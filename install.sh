#!/usr/bin/env bash
# install.sh — provision the relay on a fresh Debian/Ubuntu box.
#
#   sudo ./install.sh                  interactive, with nginx + Let's Encrypt
#   sudo ./install.sh --no-tls         skip nginx and certbot entirely
#   sudo ./install.sh --dry-run        print what would happen, change nothing
#
# Non-interactive (for CI or config management): set any of CALLSIGN, GRID,
# DOMAIN, PUBLIC_PORT, CERTBOT_EMAIL in the environment and pass
# --non-interactive; anything unset takes its default and anything required
# but missing is an error rather than a prompt.
#
# For an existing install, use ./upgrade.sh. This script will tell you so
# rather than reprovisioning over the top of one.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${HERE}/lib/common.sh"

WITH_TLS=1
DRY_RUN=0
INTERACTIVE=1
for arg in "$@"; do
  case "${arg}" in
    --no-tls)          WITH_TLS=0 ;;
    --dry-run)         DRY_RUN=1 ;;
    --non-interactive) INTERACTIVE=0 ;;
    -h|--help)         sed -n '2,15p' "$0"; exit 0 ;;
    *) die "Unknown argument: ${arg}" ;;
  esac
done

need_root
need_debian

if [[ -f "${ENV_FILE}" && -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
  die "Found an existing install at ${APP_DIR}.
Use ./upgrade.sh — it replaces the code and adds new settings without
touching your config, nginx, or certificates."
fi

ask() {  # ask <var> <prompt> <default>
  local var="$1" prompt="$2" default="${3:-}" cur="${!1:-}" ans=""
  [[ -n "${cur}" ]] && { printf -v "${var}" '%s' "${cur}"; return; }
  if [[ "${INTERACTIVE}" -eq 0 ]]; then
    printf -v "${var}" '%s' "${default}"; return
  fi
  read -rp "${prompt}${default:+ [${default}]}: " ans || true
  printf -v "${var}" '%s' "${ans:-$default}"
}

say "=== Grid Watch / Comms Watch relay ==="
cat <<'INTRO'
One process serving two dashboards. You can run either half or both.

  Ham feeds     APRS-IS positions, NetLogger active nets, analog repeaters.
                Needs an amateur callsign (receive-only; it never transmits).
  Grid feeds    CISA KEV + EPSS, DOE 202(c) orders, NERC, ISO/RTO alerts,
                and a store for a daily grid briefing. Needs nothing.

Leave the callsign blank if you only want the grid feeds.
INTRO

ask CALLSIGN     "Amateur callsign (blank for grid feeds only)" ""
CALLSIGN="$(printf '%s' "${CALLSIGN}" | tr '[:lower:]' '[:upper:]')"
if [[ -n "${CALLSIGN}" ]]; then
  ask GRID "Maidenhead grid square, e.g. EM73" ""
  [[ -n "${GRID}" ]] || die "A grid square is needed to centre the APRS filter."
fi

if [[ "${WITH_TLS}" -eq 1 ]]; then
  ask DOMAIN "Domain name this relay will answer on" ""
  [[ -n "${DOMAIN}" ]] || die "A domain is needed for a Let's Encrypt certificate. Use --no-tls to skip."
  ask PUBLIC_PORT   "Public HTTPS port" "443"
  ask CERTBOT_EMAIL "Email for Let's Encrypt (blank to register without one)" ""
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  say "--- Dry run ---"
  DRY_PKGS="python3-venv curl openssl ca-certificates"
  [[ "${WITH_TLS}" -eq 1 ]] && DRY_PKGS="${DRY_PKGS} nginx certbot python3-certbot-nginx"
  cat <<EOF
apt-get install  ${DRY_PKGS}
useradd          ${SERVICE_USER} (system user, no login)
mkdir            ${APP_DIR}
install          relay/comms_watch_relay.py -> ${APP_DIR}/
python3 -m venv  ${APP_DIR}/venv ; pip install -r requirements.txt
write            ${ENV_FILE}  (chmod 600)  callsign=${CALLSIGN:-<none>} grid=${GRID:-<none>}
write            /etc/systemd/system/${SERVICE_NAME}.service
systemctl        enable --now ${SERVICE_NAME}
EOF
  [[ "${WITH_TLS}" -eq 1 ]] && cat <<EOF
write            /etc/nginx/sites-available/${DOMAIN}.conf -> proxy to 127.0.0.1:8090
certbot          --nginx -d ${DOMAIN}${CERTBOT_EMAIL:+ -m ${CERTBOT_EMAIL}}
$( [[ "${PUBLIC_PORT}" != "443" ]] && echo "sed              move nginx listen 443 -> ${PUBLIC_PORT}" )
EOF
  exit 0
fi

say "--- Packages ---"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
PKGS=(python3-venv curl openssl ca-certificates)
[[ "${WITH_TLS}" -eq 1 ]] && PKGS+=(nginx certbot python3-certbot-nginx)
apt-get install -y "${PKGS[@]}"

say "--- Service user and directory ---"
id -u "${SERVICE_USER}" >/dev/null 2>&1 || \
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
mkdir -p "${APP_DIR}"

say "--- Relay code ---"
install_relay_code "${HERE}/relay/comms_watch_relay.py"

say "--- Python environment ---"
[[ -d "${APP_DIR}/venv" ]] || python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install -q --upgrade pip
"${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

say "--- Configuration ---"
# Start from the documented example so every setting arrives with its
# comment, then overwrite only the answers given above.
install -m 600 "${HERE}/relay.env.example" "${ENV_FILE}"
set_env() {  # set_env KEY VALUE — replace in place, the key already exists
  local k="$1" v="$2"
  grep -qE "^${k}=" "${ENV_FILE}" \
    && sed -i "s|^${k}=.*|${k}=${v}|" "${ENV_FILE}" \
    || echo "${k}=${v}" >> "${ENV_FILE}"
}
set_env CALLSIGN "${CALLSIGN}"
set_env GRID "${GRID:-}"
set_env APRS_ENABLED "$( [[ -n "${CALLSIGN}" ]] && echo true || echo false )"
set_env NETLOGGER_ENABLED "$( [[ -n "${CALLSIGN}" ]] && echo true || echo false )"
set_env REPEATERS_ENABLED "$( [[ -n "${CALLSIGN}" ]] && echo true || echo false )"
set_env DIGEST_TOKEN "$(gen_token)"
set_env BRIEFMAIL_SECRET "$(gen_token)"
apply_env_defaults
secure_env_file
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
echo "Wrote ${ENV_FILE} (mode 600)."

say "--- systemd ---"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNITEOF
[Unit]
Description=Relay for comms-watch.html and grid-watch.html
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/comms_watch_relay.py
Restart=always
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
sleep 3
systemctl is-active --quiet "${SERVICE_NAME}" || {
  warn "${SERVICE_NAME} did not start:"; journalctl -u "${SERVICE_NAME}" -n 30 --no-pager; exit 1; }

if [[ "${WITH_TLS}" -eq 1 ]]; then
  say "--- nginx ---"
  LISTEN_PORT_VAL="$(env_get LISTEN_PORT 8090)"
  NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}.conf"
  cat > "${NGINX_CONF}" <<NGINXEOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${LISTEN_PORT_VAL};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINXEOF
  ln -sf "${NGINX_CONF}" "/etc/nginx/sites-enabled/${DOMAIN}.conf"
  nginx -t
  systemctl reload nginx

  say "--- TLS certificate ---"
  CERTBOT_ARGS=(--nginx -d "${DOMAIN}" --redirect -n --agree-tos)
  if [[ -n "${CERTBOT_EMAIL:-}" ]]; then CERTBOT_ARGS+=(-m "${CERTBOT_EMAIL}")
  else CERTBOT_ARGS+=(--register-unsafely-without-email); fi
  certbot "${CERTBOT_ARGS[@]}"

  if [[ "${PUBLIC_PORT}" != "443" ]]; then
    sed -i -e "s/listen 443 ssl;/listen ${PUBLIC_PORT} ssl;/" \
           -e "s/listen \[::\]:443 ssl;/listen [::]:${PUBLIC_PORT} ssl;/" "${NGINX_CONF}"
    nginx -t
    systemctl reload nginx
  fi
  printf 'DOMAIN=%s\nPUBLIC_PORT=%s\n' "${DOMAIN}" "${PUBLIC_PORT}" > "${APP_DIR}/.install-meta"
fi

say "=== Done ==="
PORT="$(env_get LISTEN_PORT 8090)"
echo "Local health: $(curl -s --max-time 10 "http://127.0.0.1:${PORT}/health" | head -c 160 || echo '<no response>')"
echo
if [[ "${WITH_TLS}" -eq 1 ]]; then
  echo "Relay base URL for a dashboard's Setup tab:"
  echo "    https://${DOMAIN}:${PUBLIC_PORT}"
  echo
  echo "If that does not answer from outside, check these two before anything else:"
  echo "  1. Your hosting provider's own firewall or security group — separate"
  echo "     from this box's ufw/iptables and invisible from this shell. TCP"
  echo "     ${PUBLIC_PORT} has to be allowed there too."
  echo "  2. Anything else already bound to ${PUBLIC_PORT}. A VPN parked on 443"
  echo "     is the classic case, and from outside it looks like a TLS failure,"
  echo "     not like a port conflict."
else
  echo "Listening on 127.0.0.1:${PORT}. Put your own TLS terminator in front of it."
fi
echo
echo "Tokens (also in ${ENV_FILE}, mode 600):"
echo "  DIGEST_TOKEN      $(env_get DIGEST_TOKEN)"
echo "  BRIEFMAIL_SECRET  $(env_get BRIEFMAIL_SECRET)"
echo
echo "  systemctl status ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
