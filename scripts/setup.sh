#!/usr/bin/env bash
# Boxer setup script — installs system deps, configures libvirt, creates
# directories/config, generates SSH keys, installs the Python package, and
# registers systemd services.
#
# Run as root:  sudo bash scripts/setup.sh
# Or:           sudo bash scripts/setup.sh --user alice

set -euo pipefail

# ── helpers ────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[boxer]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
die()   { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── argument parsing ────────────────────────────────────────────────────────────

INSTALL_USER="${SUDO_USER:-}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) INSTALL_USER="$2"; shift 2 ;;
        --user=*) INSTALL_USER="${1#--user=}"; shift ;;
        --help|-h)
            echo "Usage: sudo bash scripts/setup.sh [--user USERNAME]"
            echo ""
            echo "  --user USERNAME   Add this user to boxer/libvirt groups."
            echo "                    Defaults to SUDO_USER (the user who ran sudo)."
            exit 0
            ;;
        *) die "Unknown argument: $1. Use --help for usage." ;;
    esac
done

# ── preflight checks ───────────────────────────────────────────────────────────

[[ "$(id -u)" -eq 0 ]] || die "This script must be run as root (use sudo)."

[[ "$(uname -s)" == "Linux" ]] || die "Boxer only supports Linux."

if ! grep -qi 'debian\|ubuntu\|mint\|pop\|elementary' /etc/os-release 2>/dev/null; then
    warn "This script is designed for Debian/Ubuntu-based systems."
    warn "Proceeding anyway — apt commands may fail on other distros."
fi

if [[ -z "$INSTALL_USER" ]]; then
    warn "Could not determine the target user (SUDO_USER is unset)."
    warn "Groups will not be assigned. Re-run with: sudo bash scripts/setup.sh --user YOUR_USERNAME"
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/boxer"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_DIR="/etc/boxer"
STATE_DIR="/var/lib/boxer"
SSH_KEY_PATH="$CONFIG_DIR/boxer_id_ed25519"

info "Boxer repo:  $REPO_DIR"
info "Install dir: $INSTALL_DIR"
info "Config dir:  $CONFIG_DIR"
info "State dir:   $STATE_DIR"
[[ -n "$INSTALL_USER" ]] && info "Target user: $INSTALL_USER"
echo ""

# ── 1. system packages ─────────────────────────────────────────────────────────

info "Installing system packages…"

PKGS=(
    # KVM / libvirt
    qemu-kvm
    libvirt-daemon-system
    libvirt-clients
    libvirt-dev
    bridge-utils
    virtinst
    qemu-utils
    cloud-image-utils
    # Python build
    python3
    python3-dev
    python3-venv
    python3-pip
    pkg-config
    # cloud-init ISO generation
    genisoimage
    # desktop notifications (boxer-notifier)
    libnotify-bin
    # screenshot conversion (optional, PIL is fallback)
    imagemagick
)

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PKGS[@]}"
ok "System packages installed."

# ── 2. libvirt service ─────────────────────────────────────────────────────────

info "Enabling libvirtd…"
systemctl enable --now libvirtd
# Ensure the default NAT network is running
if ! virsh -c qemu:///system net-list --all | grep -q ' default '; then
    virsh -c qemu:///system net-define /usr/share/libvirt/networks/default.xml 2>/dev/null || true
fi
virsh -c qemu:///system net-start default    2>/dev/null || true
virsh -c qemu:///system net-autostart default 2>/dev/null || true
ok "libvirtd running."

# ── 3. groups ──────────────────────────────────────────────────────────────────

info "Creating boxer-admin group…"
getent group boxer-admin &>/dev/null || groupadd --system boxer-admin
ok "boxer-admin group ready."

if [[ -n "$INSTALL_USER" ]]; then
    info "Adding $INSTALL_USER to groups: libvirt kvm boxer-admin"
    usermod -aG libvirt    "$INSTALL_USER" 2>/dev/null || warn "Could not add $INSTALL_USER to libvirt (group may not exist)."
    usermod -aG kvm        "$INSTALL_USER" 2>/dev/null || warn "Could not add $INSTALL_USER to kvm."
    usermod -aG boxer-admin "$INSTALL_USER"
    ok "Groups assigned. A logout/login is needed for them to take effect."
fi

# ── 4. directories ─────────────────────────────────────────────────────────────

info "Creating directories…"
install -d -m 0755 "$CONFIG_DIR"
install -d -m 0750 -o root -g libvirt "$STATE_DIR"
install -d -m 0750 -o root -g libvirt "$INSTALL_DIR"
ok "Directories created."

# ── 5. config files ────────────────────────────────────────────────────────────

info "Installing config files to $CONFIG_DIR…"

if [[ ! -f "$CONFIG_DIR/boxer.yaml" ]]; then
    cp "$REPO_DIR/config/boxer.yaml" "$CONFIG_DIR/boxer.yaml"
    chmod 0640 "$CONFIG_DIR/boxer.yaml"
    ok "Copied boxer.yaml"
else
    ok "boxer.yaml already present — skipped."
fi

if [[ ! -f "$CONFIG_DIR/images.yaml" ]]; then
    cp "$REPO_DIR/config/images.yaml" "$CONFIG_DIR/images.yaml"
    chmod 0640 "$CONFIG_DIR/images.yaml"
    ok "Copied images.yaml"
else
    ok "images.yaml already present — skipped."
fi

# ── 6. SSH keypair for VM access ───────────────────────────────────────────────

info "Checking Boxer SSH keypair…"
if [[ ! -f "$SSH_KEY_PATH" ]]; then
    ssh-keygen -t ed25519 -N "" -C "boxer-vm-access" -f "$SSH_KEY_PATH" -q
    chmod 0600 "$SSH_KEY_PATH"
    chmod 0644 "${SSH_KEY_PATH}.pub"
    PUBKEY=$(cat "${SSH_KEY_PATH}.pub")
    # Inject the public key into boxer.yaml
    if grep -q '^# boxer_ssh_pubkey' "$CONFIG_DIR/boxer.yaml"; then
        # Uncomment and set the value
        sed -i "s|^# boxer_ssh_pubkey:.*|boxer_ssh_pubkey: \"$PUBKEY\"|" "$CONFIG_DIR/boxer.yaml"
    elif ! grep -q '^boxer_ssh_pubkey' "$CONFIG_DIR/boxer.yaml"; then
        echo "boxer_ssh_pubkey: \"$PUBKEY\"" >> "$CONFIG_DIR/boxer.yaml"
    fi
    if grep -q '^# boxer_ssh_privkey_path' "$CONFIG_DIR/boxer.yaml"; then
        sed -i "s|^# boxer_ssh_privkey_path:.*|boxer_ssh_privkey_path: $SSH_KEY_PATH|" "$CONFIG_DIR/boxer.yaml"
    elif ! grep -q '^boxer_ssh_privkey_path' "$CONFIG_DIR/boxer.yaml"; then
        echo "boxer_ssh_privkey_path: $SSH_KEY_PATH" >> "$CONFIG_DIR/boxer.yaml"
    fi
    ok "SSH keypair generated at $SSH_KEY_PATH"
else
    ok "SSH keypair already exists at $SSH_KEY_PATH — skipped."
fi

# ── 7. Python venv + package install ──────────────────────────────────────────

info "Creating Python virtual environment at $VENV_DIR…"
python3 -m venv "$VENV_DIR"

info "Installing Boxer Python package…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$REPO_DIR"

# Create convenience symlinks for the four entry points
for cmd in boxerd boxer boxer-mcp boxer-notifier; do
    ln -sf "$VENV_DIR/bin/$cmd" "/usr/local/bin/$cmd"
done
ok "Boxer installed. Entry points: boxerd, boxer, boxer-mcp, boxer-notifier"

# ── 8. systemd services ────────────────────────────────────────────────────────

info "Installing systemd service: boxerd (system)…"
# Patch ExecStart to use the venv python
BOXERD_SERVICE="$REPO_DIR/systemd/boxerd.service"
DEST_SERVICE="/etc/systemd/system/boxerd.service"
sed "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/boxerd|" "$BOXERD_SERVICE" > "$DEST_SERVICE"
systemctl daemon-reload
systemctl enable boxerd
ok "boxerd.service installed and enabled."

if [[ -n "$INSTALL_USER" ]]; then
    info "Installing systemd user service: boxer-notifier…"
    USER_SYSTEMD_DIR="/home/$INSTALL_USER/.config/systemd/user"
    install -d -m 0755 -o "$INSTALL_USER" "$USER_SYSTEMD_DIR"
    NOTIFIER_SERVICE="$REPO_DIR/systemd/boxer-notifier.service"
    DEST_NOTIFIER="$USER_SYSTEMD_DIR/boxer-notifier.service"
    sed "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/boxer-notifier|" "$NOTIFIER_SERVICE" > "$DEST_NOTIFIER"
    chown "$INSTALL_USER" "$DEST_NOTIFIER"
    # Enable as the target user
    su -l "$INSTALL_USER" -c "systemctl --user daemon-reload && systemctl --user enable boxer-notifier" 2>/dev/null \
        || warn "Could not enable boxer-notifier user service (no active user session?). Run: systemctl --user enable boxer-notifier"
    ok "boxer-notifier.service installed for user $INSTALL_USER."
fi

# ── 9. MCP registration hint ──────────────────────────────────────────────────

info "Generating Claude Code MCP registration command…"
MCP_CMD="claude mcp add --transport stdio --scope local boxer-vm -- $VENV_DIR/bin/boxer-mcp"
echo ""
echo "  Add Boxer to Claude Code with:"
echo ""
echo "    $MCP_CMD"
echo ""

# ── summary ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Boxer setup complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo ""
echo "  Start the daemon:     sudo systemctl start boxerd"
echo "  Check daemon logs:    journalctl -u boxerd -f"
echo "  List VMs:             boxer ls"
echo "  Host resource status: boxer status"
echo ""
if [[ -n "$INSTALL_USER" ]]; then
    echo "  NOTE: Log out and back in as $INSTALL_USER for group membership"
    echo "        (libvirt, kvm, boxer-admin) to take effect."
    echo ""
fi
echo "  Config:  $CONFIG_DIR/boxer.yaml"
echo "  State:   $STATE_DIR"
echo "  SSH key: $SSH_KEY_PATH"
echo ""
echo "  Emergency VM cleanup (if boxerd is unavailable):"
echo "    virsh -c qemu:///system list --all | grep 'Boxer--'"
echo "    virsh -c qemu:///system destroy  Boxer--<name>"
echo "    virsh -c qemu:///system undefine Boxer--<name> --remove-all-storage"
echo ""
