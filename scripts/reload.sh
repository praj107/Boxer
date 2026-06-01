#!/usr/bin/env bash
# Hot-reload Boxer: reinstall the Python package from the repo and restart the daemon.
# Run as root:  sudo bash scripts/reload.sh
# Or from repo: sudo bash scripts/reload.sh [--check] [--restart-mcp]
#
# --check       : print what would change without making any changes (dry-run).
# --restart-mcp : also terminate boxer-mcp stdio children. This closes active
#                 MCP client sessions; restart the client after using it.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="/opt/boxer/venv"
SERVICE="boxerd"
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"

CHECK_ONLY=0
RESTART_MCP=0
for arg in "$@"; do
    case "${arg}" in
        --check)
            CHECK_ONLY=1
            ;;
        --restart-mcp|--kill-mcp)
            RESTART_MCP=1
            ;;
        -h|--help)
            sed -n '1,8p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: ${arg}" >&2
            echo "usage: sudo bash scripts/reload.sh [--check] [--restart-mcp]" >&2
            exit 2
            ;;
    esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "ERROR: run as root (sudo bash scripts/reload.sh)" >&2; exit 1; }

echo "[reload] Boxer repo:    ${REPO_DIR}"
echo "[reload] Install venv:  ${VENV_DIR}"

# Show what version is currently installed vs what the repo has.
INSTALLED=$("${VENV_DIR}/bin/pip" show boxer 2>/dev/null | awk '/^Version:/{print $2}') || INSTALLED="(none)"
REPO_VER=$(python3 -c "import tomllib; d=tomllib.load(open('${REPO_DIR}/pyproject.toml','rb')); print(d['project']['version'])" 2>/dev/null || echo "(unknown)")
echo "[reload] Installed: ${INSTALLED}  Repo: ${REPO_VER}"

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
    echo "[reload] --check mode: no changes made."
    exit 0
fi

echo "[reload] Reinstalling package…"
"${VENV_DIR}/bin/pip" install --quiet --force-reinstall "${REPO_DIR}"

if [[ -f "${REPO_DIR}/systemd/${SERVICE}.service" ]]; then
    echo "[reload] Installing systemd unit…"
    tmp_service=$(mktemp --tmpdir "$(basename "${SERVICE_FILE}").XXXXXX")
    sed "s|ExecStart=.*|ExecStart=${VENV_DIR}/bin/boxerd|" \
        "${REPO_DIR}/systemd/${SERVICE}.service" > "${tmp_service}"
    chmod 0644 "${tmp_service}"
    mv -f "${tmp_service}" "${SERVICE_FILE}"
    systemctl daemon-reload
fi

STATE_DIR=$("${VENV_DIR}/bin/python" - <<'PY'
from boxer.config import get_config
print(get_config().state_dir)
PY
)
QEMU_GROUP=$("${VENV_DIR}/bin/python" - <<'PY'
from boxer.config import get_config
print(get_config().qemu_group)
PY
)
if getent group "${QEMU_GROUP}" >/dev/null; then
    echo "[reload] Repairing VM artifact permissions for group ${QEMU_GROUP}…"
    install -d -m 0750 -o root -g "${QEMU_GROUP}" "${STATE_DIR}"
    for dir in "${STATE_DIR}/projects" "${STATE_DIR}/images" "${STATE_DIR}/isos"; do
        [[ -e "${dir}" ]] || continue
        chgrp -R "${QEMU_GROUP}" "${dir}" 2>/dev/null || true
        find "${dir}" -type d -exec chmod 0750 {} +
    done
    if [[ -e "${STATE_DIR}/projects" ]]; then
        find "${STATE_DIR}/projects" -type f -exec chmod 0660 {} +
    fi
    for dir in "${STATE_DIR}/images" "${STATE_DIR}/isos"; do
        [[ -e "${dir}" ]] || continue
        find "${dir}" -type f -exec chmod 0440 {} +
    done
else
    echo "[reload] WARNING: qemu_group '${QEMU_GROUP}' does not exist; VM artifact permissions not repaired."
fi

# boxer-mcp is a per-client stdio server. Killing it from this script closes the
# active MCP transport, and clients such as Codex may not reconnect until the
# session is restarted. Leave it running by default; opt in when you are ready to
# restart MCP clients and want the next session to load the reinstalled package.
MCP_PIDS=$(pgrep -f "${VENV_DIR}/bin/boxer-mcp" 2>/dev/null || true)
if [[ -n "${MCP_PIDS}" && "${RESTART_MCP}" -eq 1 ]]; then
    echo "[reload] Terminating boxer-mcp PIDs: ${MCP_PIDS}"
    kill ${MCP_PIDS} 2>/dev/null || true
elif [[ -n "${MCP_PIDS}" ]]; then
    echo "[reload] boxer-mcp still running: ${MCP_PIDS}"
    echo "[reload] Existing MCP sessions keep their loaded code. Restart the MCP client,"
    echo "[reload] or rerun with --restart-mcp after ending active sessions."
fi

echo "[reload] Restarting ${SERVICE}…"
systemctl restart "${SERVICE}"

# Wait for the service to come up
for i in $(seq 1 10); do
    if systemctl is-active --quiet "${SERVICE}"; then
        echo "[reload] ${SERVICE} is active."
        systemctl status "${SERVICE}" --no-pager -l | tail -6
        exit 0
    fi
    sleep 1
done

echo "[reload] ERROR: ${SERVICE} did not come up within 10s." >&2
journalctl -u "${SERVICE}" --no-pager -n 20 >&2
exit 1
