#!/usr/bin/env bash
# Boxer setup script — installs system deps, configures libvirt, creates
# directories/config, generates SSH keys, installs the Python package, and
# registers systemd services.
#
# Run as root:  sudo bash scripts/setup.sh
# Or:           sudo bash scripts/setup.sh --user alice
# Dry-run:      sudo bash scripts/setup.sh --dry-run
# Force-unlock: sudo bash scripts/setup.sh --force-unlock

set -eEuo pipefail

# ── constants ──────────────────────────────────────────────────────────────────

readonly LOCK_FILE="/tmp/boxer-setup.lock"
readonly DEFAULT_LOGFILE="/var/log/boxer-setup.log"

# ── colour helpers (only when stdout is a terminal) ───────────────────────────

if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; NC=''
fi

# ── structured logging ─────────────────────────────────────────────────────────

LOGFILE=""          # resolved after root-check (may need mktemp fallback)
_LOG_READY=0        # set to 1 once tee is running

_ts()   { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
info()  { echo -e "${CYAN}[$(_ts)] [INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[$(_ts)] [OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[$(_ts)] [WARN]${NC}  $*"; }
die()   { echo -e "${RED}[$(_ts)] [ERROR]${NC} $*" >&2; exit 1; }

# ── state / rollback tracking ──────────────────────────────────────────────────

COMPLETED_STEPS=()
_SETUP_FAILED=0     # set to 1 by ERR trap; cleanup gates rollback on this

_record_step() { COMPLETED_STEPS+=("$1"); }

# ── temp-file registry ─────────────────────────────────────────────────────────

_TMPFILES=()
_mktmp() {
    # Usage: _mktmp [mktemp-args…]  → prints the path; registers for cleanup
    local t
    t=$(mktemp "$@")
    _TMPFILES+=("$t")
    printf '%s' "$t"
}

# ── lock file fd ───────────────────────────────────────────────────────────────

_LOCK_FD=          # set once flock opens the fd

# ── cleanup / trap ─────────────────────────────────────────────────────────────

cleanup() {
    local exit_code=$?

    # Remove registered temp files
    local f
    for f in "${_TMPFILES[@]+"${_TMPFILES[@]}"}"; do
        rm -f -- "$f"
    done

    # Release lock fd
    if [[ -n "${_LOCK_FD}" ]]; then
        flock -u "${_LOCK_FD}" 2>/dev/null || true
        eval "exec ${_LOCK_FD}>&-" 2>/dev/null || true
    fi

    # On failure: print completed steps and inverse hints
    if [[ "${_SETUP_FAILED}" -eq 1 ]]; then
        echo "" >&2
        echo -e "${RED}[$(_ts)] [ERROR]${NC} Setup failed. Completed steps so far:" >&2
        local step
        for step in "${COMPLETED_STEPS[@]+"${COMPLETED_STEPS[@]}"}"; do
            echo "  - ${step}" >&2
        done
        echo "" >&2
        echo "  Rollback hints (run manually if needed):" >&2
        echo "    systemctl disable --now boxerd 2>/dev/null" >&2
        echo "    rm -f /etc/systemd/system/boxerd.service" >&2
        echo "    groupdel boxer-admin 2>/dev/null" >&2
        echo "    rm -f /usr/local/bin/{boxerd,boxer,boxer-mcp,boxer-notifier}" >&2
    fi

    if [[ -n "${LOGFILE}" ]]; then
        echo "" >&2
        echo "Full log: ${LOGFILE}" >&2
    fi

    return "${exit_code}"
}

_on_err() {
    _SETUP_FAILED=1
}

trap '_on_err' ERR
trap 'cleanup' EXIT INT TERM

# ── safe in-place sed helper ───────────────────────────────────────────────────

_sed_inplace() {
    local file="$1" expr="$2"
    local tmp mode owner group
    mode=$(stat -c '%a' "${file}")
    owner=$(stat -c '%u' "${file}")
    group=$(stat -c '%g' "${file}")
    tmp=$(_mktmp -- "${file}.XXXXXX")
    if sed "${expr}" "${file}" > "${tmp}"; then
        chmod "${mode}" "${tmp}"
        chown "${owner}:${group}" "${tmp}"
        mv -f "${tmp}" "${file}"
    else
        rm -f "${tmp}"
        return 1
    fi
}

# ── dry-run command runner ─────────────────────────────────────────────────────

DRY_RUN=0

run_cmd() {
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# ── argument parsing ───────────────────────────────────────────────────────────

INSTALL_USER="${SUDO_USER:-}"
FORCE_UNLOCK=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            [[ $# -ge 2 ]] || die "--user requires an argument."
            INSTALL_USER="$2"; shift 2 ;;
        --user=*)
            INSTALL_USER="${1#--user=}"; shift ;;
        --dry-run|-n)
            DRY_RUN=1; shift ;;
        --force-unlock)
            FORCE_UNLOCK=1; shift ;;
        --help|-h)
            cat <<'EOF'
Usage: sudo bash scripts/setup.sh [OPTIONS]

Options:
  --user USERNAME   Add this user to boxer/libvirt groups.
                    Defaults to SUDO_USER (the user who ran sudo).
  --dry-run, -n     Print state-mutating commands without executing them.
  --force-unlock    Remove a stale /tmp/boxer-setup.lock and continue.
  --help, -h        Show this help and exit.
EOF
            exit 0 ;;
        *) die "Unknown argument: $1. Use --help for usage." ;;
    esac
done

# ── preflight checks ───────────────────────────────────────────────────────────

[[ "$(id -u)" -eq 0 ]] || die "This script must be run as root (use sudo)."
[[ "$(uname -s)" == "Linux" ]] || die "Boxer only supports Linux."

# ── resolve REPO_DIR once at startup ──────────────────────────────────────────

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_DIR

# ── set up logging ─────────────────────────────────────────────────────────────

if [[ "${DRY_RUN}" -eq 0 ]]; then
    if touch "${DEFAULT_LOGFILE}" 2>/dev/null; then
        LOGFILE="${DEFAULT_LOGFILE}"
    else
        LOGFILE=$(_mktmp -t boxer-setup-XXXXXX.log)
        warn "Cannot write to ${DEFAULT_LOGFILE}; logging to ${LOGFILE}"
    fi
    # Redirect all stdout/stderr through tee into the log
    exec > >(tee -a "${LOGFILE}") 2>&1
    _LOG_READY=1
else
    LOGFILE=$(_mktmp -t boxer-setup-dryrun-XXXXXX.log)
    exec > >(tee -a "${LOGFILE}") 2>&1
    _LOG_READY=1
fi

# ── exclusive lock ─────────────────────────────────────────────────────────────

if [[ "${FORCE_UNLOCK}" -eq 1 ]]; then
    rm -f -- "${LOCK_FILE}"
    info "Stale lock removed (--force-unlock)."
fi

# Open the lock file on a free fd (fd 9)
exec 9>"${LOCK_FILE}"
_LOCK_FD=9

if ! flock -n 9; then
    die "Another instance of setup.sh is running (lock: ${LOCK_FILE}). Use --force-unlock to override."
fi

# ── dependency checks ──────────────────────────────────────────────────────────

_check_deps() {
    local tool missing=()
    local tools=(virsh ssh-keygen systemctl apt-get python3 flock getent groupadd usermod sed mktemp install stat gpgv)
    for tool in "${tools[@]}"; do
        command -v "${tool}" &>/dev/null || missing+=("${tool}")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required tools: ${missing[*]}.  The apt install step should supply them; re-run after step 1 completes manually."
    fi
}

# Note: some tools (virsh, etc.) are installed by the apt step; we defer the
# full check until after packages have been installed.  We do a minimal early
# check for the tools required before that step.
_check_early_deps() {
    local tool missing=()
    local tools=(apt-get python3 flock sed mktemp install stat)
    for tool in "${tools[@]}"; do
        command -v "${tool}" &>/dev/null || missing+=("${tool}")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing tools required before apt step: ${missing[*]}."
    fi
}

_check_early_deps

# ── distro warning ─────────────────────────────────────────────────────────────

if ! grep -qi 'debian\|ubuntu\|mint\|pop\|elementary' /etc/os-release 2>/dev/null; then
    warn "This script is designed for Debian/Ubuntu-based systems."
    warn "Proceeding anyway — apt commands may fail on other distros."
fi

# ── path constants (captured once) ────────────────────────────────────────────

readonly INSTALL_DIR="/opt/boxer"
readonly VENV_DIR="${INSTALL_DIR}/venv"
readonly CONFIG_DIR="/etc/boxer"
readonly STATE_DIR="/var/lib/boxer"
readonly SSH_KEY_PATH="${CONFIG_DIR}/boxer_id_ed25519"
readonly CONFIG_GROUP="libvirt"

if [[ -z "${INSTALL_USER}" ]]; then
    warn "Could not determine the target user (SUDO_USER is unset)."
    warn "Groups will not be assigned. Re-run with: sudo bash scripts/setup.sh --user YOUR_USERNAME"
fi

info "Boxer repo:  ${REPO_DIR}"
info "Install dir: ${INSTALL_DIR}"
info "Config dir:  ${CONFIG_DIR}"
info "State dir:   ${STATE_DIR}"
[[ -n "${INSTALL_USER}" ]] && info "Target user: ${INSTALL_USER}"
[[ "${DRY_RUN}" -eq 1 ]]   && warn "DRY-RUN mode — no system changes will be made."
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
    # PGP signature verification of image checksum manifests (gpgv)
    gnupg
    # desktop notifications (boxer-notifier)
    libnotify-bin
    # screenshot conversion (optional, PIL is fallback)
    imagemagick
)

run_cmd apt-get update -qq
run_cmd env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PKGS[@]}"
ok "System packages installed."
_record_step "apt-packages"

# Full dependency check now that packages should be present
if [[ "${DRY_RUN}" -eq 0 ]]; then
    _check_deps
fi

# ── 2. libvirt service ─────────────────────────────────────────────────────────

info "Enabling libvirtd…"
run_cmd systemctl enable --now libvirtd

# Ensure the default NAT network is running (idempotent: ignore already-exists errors)
if [[ "${DRY_RUN}" -eq 0 ]]; then
    if ! virsh -c qemu:///system net-list --all | grep -q ' default '; then
        virsh -c qemu:///system net-define /usr/share/libvirt/networks/default.xml 2>/dev/null || true
    fi
    virsh -c qemu:///system net-start default    2>/dev/null || true
    virsh -c qemu:///system net-autostart default 2>/dev/null || true
else
    echo "[DRY-RUN] virsh -c qemu:///system net-define/start/autostart default"
fi
ok "libvirtd running."
_record_step "libvirtd-enabled"

# ── 3. groups ──────────────────────────────────────────────────────────────────

info "Creating boxer-admin group…"
if getent group boxer-admin &>/dev/null; then
    ok "boxer-admin group already exists — skipped."
else
    run_cmd groupadd --system boxer-admin
    ok "boxer-admin group created."
fi
_record_step "group-boxer-admin"

if [[ -n "${INSTALL_USER}" ]]; then
    info "Adding ${INSTALL_USER} to groups: libvirt kvm boxer-admin"
    _add_user_groups=(libvirt kvm boxer-admin)
    _g=""
    for _g in "${_add_user_groups[@]}"; do
        if getent group "${_g}" &>/dev/null; then
            if id -nG "${INSTALL_USER}" 2>/dev/null | grep -qw "${_g}"; then
                ok "${INSTALL_USER} already in group ${_g} — skipped."
            else
                run_cmd usermod -aG "${_g}" "${INSTALL_USER}" || warn "Could not add ${INSTALL_USER} to ${_g}."
            fi
        else
            warn "Group ${_g} does not exist — skipping."
        fi
    done
    ok "Groups assigned. A logout/login is needed for them to take effect."
    _record_step "user-groups-${INSTALL_USER}"
fi

# ── 4. directories ─────────────────────────────────────────────────────────────

info "Creating directories…"
run_cmd install -d -m 0755 "${CONFIG_DIR}"
run_cmd install -d -m 0755 "${CONFIG_DIR}/keyrings"
run_cmd install -d -m 0750 -o root -g libvirt "${STATE_DIR}"
run_cmd install -d -m 0750 -o root -g libvirt "${INSTALL_DIR}"
ok "Directories created."
_record_step "directories"

# ── 5. config files ────────────────────────────────────────────────────────────

info "Installing config files to ${CONFIG_DIR}…"

_install_config() {
    local src="$1" dst="$2" mode="${3:-0640}" owner="${4:-root}" group="${5:-${CONFIG_GROUP}}"
    if [[ ! -f "${dst}" ]]; then
        local tmp
        tmp=$(_mktmp -- "${dst}.XXXXXX")
        cp -- "${src}" "${tmp}"
        chmod "${mode}" "${tmp}"
        chown "${owner}:${group}" "${tmp}"
        run_cmd mv -f "${tmp}" "${dst}"
        ok "Copied $(basename "${dst}")"
    else
        ok "$(basename "${dst}") already present — skipped."
    fi
    run_cmd chown "${owner}:${group}" "${dst}"
    run_cmd chmod "${mode}" "${dst}"
}

if [[ "${DRY_RUN}" -eq 0 ]]; then
    _install_config "${REPO_DIR}/config/boxer.yaml"  "${CONFIG_DIR}/boxer.yaml"
    _install_config "${REPO_DIR}/config/images.yaml" "${CONFIG_DIR}/images.yaml"
else
    echo "[DRY-RUN] install config: boxer.yaml → ${CONFIG_DIR}/boxer.yaml"
    echo "[DRY-RUN] install config: images.yaml → ${CONFIG_DIR}/images.yaml"
fi
_record_step "config-files"

# ── 5b. trusted PGP keyrings ────────────────────────────────────────────────────

info "Installing trusted image keyrings to ${CONFIG_DIR}/keyrings…"
if [[ "${DRY_RUN}" -eq 0 ]]; then
    _kr_found=0
    shopt -s nullglob
    for _kr in "${REPO_DIR}"/config/keyrings/*.gpg "${REPO_DIR}"/config/keyrings/*.kbx; do
        _kr_found=1
        _install_config "${_kr}" "${CONFIG_DIR}/keyrings/$(basename "${_kr}")" 0644 root root
    done
    shopt -u nullglob
    if [[ "${_kr_found}" -eq 0 ]]; then
        warn "No keyrings bundled — image signatures will verify by checksum only."
        warn "See ${CONFIG_DIR}/keyrings (config/keyrings/README.md) to enable fail-closed PGP verification."
    fi
else
    echo "[DRY-RUN] install trusted keyrings → ${CONFIG_DIR}/keyrings/"
fi
_record_step "keyrings"

# ── 6. SSH keypair for VM access ───────────────────────────────────────────────

info "Checking Boxer SSH keypair…"
if [[ ! -f "${SSH_KEY_PATH}" ]]; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "[DRY-RUN] ssh-keygen -t ed25519 -N '' -C boxer-vm-access -f ${SSH_KEY_PATH} -q"
        echo "[DRY-RUN] chmod 0600 ${SSH_KEY_PATH}"
        echo "[DRY-RUN] chmod 0644 ${SSH_KEY_PATH}.pub"
        echo "[DRY-RUN] inject boxer_ssh_pubkey into ${CONFIG_DIR}/boxer.yaml"
    else
        run_cmd ssh-keygen -t ed25519 -N "" -C "boxer-vm-access" -f "${SSH_KEY_PATH}" -q
        run_cmd chmod 0600 "${SSH_KEY_PATH}"
        run_cmd chmod 0644 "${SSH_KEY_PATH}.pub"
        _PUBKEY=$(cat "${SSH_KEY_PATH}.pub")

        # Inject public key into boxer.yaml — use _sed_inplace for atomicity
        if grep -q '^# boxer_ssh_pubkey' "${CONFIG_DIR}/boxer.yaml"; then
            _sed_inplace "${CONFIG_DIR}/boxer.yaml" \
                "s|^# boxer_ssh_pubkey:.*|boxer_ssh_pubkey: \"${_PUBKEY}\"|"
        elif ! grep -q '^boxer_ssh_pubkey' "${CONFIG_DIR}/boxer.yaml"; then
            _tmp_yaml=$(_mktmp -- "${CONFIG_DIR}/boxer.yaml.XXXXXX")
            { cat "${CONFIG_DIR}/boxer.yaml"; printf 'boxer_ssh_pubkey: "%s"\n' "${_PUBKEY}"; } \
                > "${_tmp_yaml}"
            chmod 0640 "${_tmp_yaml}"
            chown "root:${CONFIG_GROUP}" "${_tmp_yaml}"
            mv -f "${_tmp_yaml}" "${CONFIG_DIR}/boxer.yaml"
        fi

        if grep -q '^# boxer_ssh_privkey_path' "${CONFIG_DIR}/boxer.yaml"; then
            _sed_inplace "${CONFIG_DIR}/boxer.yaml" \
                "s|^# boxer_ssh_privkey_path:.*|boxer_ssh_privkey_path: ${SSH_KEY_PATH}|"
        elif ! grep -q '^boxer_ssh_privkey_path' "${CONFIG_DIR}/boxer.yaml"; then
            _tmp_yaml2=$(_mktmp -- "${CONFIG_DIR}/boxer.yaml.XXXXXX")
            { cat "${CONFIG_DIR}/boxer.yaml"; printf 'boxer_ssh_privkey_path: %s\n' "${SSH_KEY_PATH}"; } \
                > "${_tmp_yaml2}"
            chmod 0640 "${_tmp_yaml2}"
            chown "root:${CONFIG_GROUP}" "${_tmp_yaml2}"
            mv -f "${_tmp_yaml2}" "${CONFIG_DIR}/boxer.yaml"
        fi
        ok "SSH keypair generated at ${SSH_KEY_PATH}"
    fi
else
    ok "SSH keypair already exists at ${SSH_KEY_PATH} — skipped."
fi
if [[ "${DRY_RUN}" -eq 0 ]]; then
    chown root:root "${SSH_KEY_PATH}" 2>/dev/null || true
    chmod 0600 "${SSH_KEY_PATH}" 2>/dev/null || true
    chown root:root "${SSH_KEY_PATH}.pub" 2>/dev/null || true
    chmod 0644 "${SSH_KEY_PATH}.pub" 2>/dev/null || true
    chown "root:${CONFIG_GROUP}" "${CONFIG_DIR}/boxer.yaml"
    chmod 0640 "${CONFIG_DIR}/boxer.yaml"
fi
_record_step "ssh-keypair"

# ── 7. Python venv + package install ──────────────────────────────────────────

info "Creating Python virtual environment at ${VENV_DIR}…"
run_cmd python3 -m venv "${VENV_DIR}"

info "Installing Boxer Python package…"
run_cmd "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
run_cmd "${VENV_DIR}/bin/pip" install --quiet "${REPO_DIR}"
_record_step "python-venv"

# Create convenience symlinks for the four entry points (ln -sf is idempotent)
for cmd in boxerd boxer boxer-mcp boxer-notifier; do
    run_cmd ln -sf "${VENV_DIR}/bin/${cmd}" "/usr/local/bin/${cmd}"
done
ok "Boxer installed. Entry points: boxerd, boxer, boxer-mcp, boxer-notifier"
_record_step "entry-point-symlinks"

# ── 8. systemd services ────────────────────────────────────────────────────────

info "Installing systemd service: boxerd (system)…"

BOXERD_SERVICE="${REPO_DIR}/systemd/boxerd.service"
DEST_SERVICE="/etc/systemd/system/boxerd.service"

# Write via temp file for atomicity
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "[DRY-RUN] patch + install ${BOXERD_SERVICE} → ${DEST_SERVICE}"
    echo "[DRY-RUN] systemctl daemon-reload && systemctl enable boxerd"
else
    _tmp_svc=$(_mktmp -- "${DEST_SERVICE}.XXXXXX")
    sed "s|ExecStart=.*|ExecStart=${VENV_DIR}/bin/boxerd|" "${BOXERD_SERVICE}" > "${_tmp_svc}"
    mv -f "${_tmp_svc}" "${DEST_SERVICE}"
    run_cmd systemctl daemon-reload
    run_cmd systemctl enable boxerd
fi
ok "boxerd.service installed and enabled."
_record_step "boxerd-service"

if [[ -n "${INSTALL_USER}" ]]; then
    info "Installing systemd user service: boxer-notifier…"
    _USER_SYSTEMD_DIR="/home/${INSTALL_USER}/.config/systemd/user"
    run_cmd install -d -m 0755 -o "${INSTALL_USER}" "${_USER_SYSTEMD_DIR}"

    _NOTIFIER_SERVICE="${REPO_DIR}/systemd/boxer-notifier.service"
    _DEST_NOTIFIER="${_USER_SYSTEMD_DIR}/boxer-notifier.service"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "[DRY-RUN] patch + install ${_NOTIFIER_SERVICE} → ${_DEST_NOTIFIER}"
        echo "[DRY-RUN] su -l ${INSTALL_USER} -c 'systemctl --user daemon-reload && systemctl --user enable boxer-notifier'"
    else
        _tmp_ntfy=$(_mktmp -- "${_DEST_NOTIFIER}.XXXXXX")
        sed "s|ExecStart=.*|ExecStart=${VENV_DIR}/bin/boxer-notifier|" "${_NOTIFIER_SERVICE}" > "${_tmp_ntfy}"
        mv -f "${_tmp_ntfy}" "${_DEST_NOTIFIER}"
        chown "${INSTALL_USER}" "${_DEST_NOTIFIER}"
        su -l "${INSTALL_USER}" -c "systemctl --user daemon-reload && systemctl --user enable boxer-notifier" 2>/dev/null \
            || warn "Could not enable boxer-notifier user service (no active user session?). Run: systemctl --user enable boxer-notifier"
    fi
    ok "boxer-notifier.service installed for user ${INSTALL_USER}."
    _record_step "notifier-service-${INSTALL_USER}"
fi

# ── 9. MCP registration hint ──────────────────────────────────────────────────

info "Generating Claude Code MCP registration command…"
_MCP_CMD="claude mcp add --transport stdio --scope local boxer-vm -- ${VENV_DIR}/bin/boxer-mcp"
echo ""
echo "  Add Boxer to Claude Code with:"
echo ""
echo "    ${_MCP_CMD}"
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
if [[ -n "${INSTALL_USER}" ]]; then
    echo "  NOTE: Log out and back in as ${INSTALL_USER} for group membership"
    echo "        (libvirt, kvm, boxer-admin) to take effect."
    echo ""
fi
echo "  Config:  ${CONFIG_DIR}/boxer.yaml"
echo "  State:   ${STATE_DIR}"
echo "  SSH key: ${SSH_KEY_PATH}"
echo "  Log:     ${LOGFILE}"
echo ""
echo "  Emergency VM cleanup (if boxerd is unavailable):"
echo "    virsh -c qemu:///system list --all | grep 'Boxer--'"
echo "    virsh -c qemu:///system destroy  Boxer--<name>"
echo "    virsh -c qemu:///system undefine Boxer--<name> --remove-all-storage"
echo ""
