#!/usr/bin/env bash
# upgrade.sh — bring an existing relay up to this checkout.
#
# Deliberately narrow. It replaces the relay code, adds any settings this
# version introduced, tightens relay.env's permissions, and restarts the
# service. It does not touch nginx, TLS, the systemd unit, the system user,
# or any value already in relay.env.
#
#   git pull && sudo ./upgrade.sh
#
# That narrowness is the point. The predecessor to this project shipped as a
# single script that rewrote everything on every run, which meant an older
# copy of it would silently delete newer features. Nothing here can do that:
# the relay comes from the checkout you are standing in, and the one thing
# that would be destructive — overwriting your config — it refuses to do.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${HERE}/lib/common.sh"

DRY_RUN=0
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "Unknown argument: ${arg}" ;;
  esac
done

need_root

[[ -f "${ENV_FILE}" ]] || die \
  "No ${ENV_FILE} — this looks like a fresh machine, not an upgrade.
Run ./install.sh instead."

if [[ "${DRY_RUN}" -eq 1 ]]; then
  say "--- Dry run: nothing will be written ---"
  echo "Would install:  ${HERE}/relay/comms_watch_relay.py -> ${APP_DIR}/comms_watch_relay.py"
  echo "Current:        $(sha256sum "${APP_DIR}/comms_watch_relay.py" 2>/dev/null | cut -c1-16 || echo 'absent')"
  echo "New:            $(sha256sum "${HERE}/relay/comms_watch_relay.py" | cut -c1-16)"
  echo
  echo "Settings this version would ADD to relay.env (values you already have are never touched):"
  while IFS= read -r key; do
    grep -qE "^${key}=" "${ENV_FILE}" || echo "  + ${key}"
  done < <(grep -oE '^\s*ensure_env_default [A-Z_]+' "${HERE}/lib/common.sh" | awk '{print $2}')
  echo
  echo "Would then: chmod 600 ${ENV_FILE}; systemctl restart ${SERVICE_NAME}"
  exit 0
fi

say "--- Relay code ---"
install_relay_code "${HERE}/relay/comms_watch_relay.py"
echo "Installed $(sha256sum "${APP_DIR}/comms_watch_relay.py" | cut -c1-16) to ${APP_DIR}"

say "--- Dependencies ---"
if [[ -x "${APP_DIR}/venv/bin/pip" ]]; then
  "${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
  echo "Up to date."
else
  warn "No venv at ${APP_DIR}/venv — run ./install.sh first."
  exit 1
fi

say "--- Settings ---"
apply_env_defaults
secure_env_file
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}" 2>/dev/null || true

say "--- Restart ---"
systemctl restart "${SERVICE_NAME}"
sleep 3
systemctl is-active --quiet "${SERVICE_NAME}" \
  && echo "${SERVICE_NAME} is running." \
  || { warn "${SERVICE_NAME} did not come back up:"; journalctl -u "${SERVICE_NAME}" -n 30 --no-pager; exit 1; }

PORT="$(env_get LISTEN_PORT 8090)"
echo
echo "Health: $(curl -s --max-time 10 "http://127.0.0.1:${PORT}/health" | head -c 200 || echo '<no response>')"
echo
echo "  journalctl -u ${SERVICE_NAME} -f"
