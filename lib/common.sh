#!/usr/bin/env bash
# Shared helpers for install.sh and upgrade.sh.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aprs-relay}"
SERVICE_USER="${SERVICE_USER:-aprs-relay}"
SERVICE_NAME="${SERVICE_NAME:-aprs-relay}"
ENV_FILE="${APP_DIR}/relay.env"

# These three names look like leftovers from when this only served ham radio
# feeds, and they are. They are deliberately unchanged: an existing install
# is recognised by exactly these paths, and renaming them would orphan every
# relay already deployed. The project outgrew its name; that is cheaper to
# explain than to fix.

say()  { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

need_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run as root: sudo $0"
}

need_debian() {
  command -v apt-get >/dev/null 2>&1 || die \
    "This installer targets Debian/Ubuntu (it uses apt and systemd). On another
distro, read install.sh — it is short — and do the equivalent by hand. The
relay itself is plain Python and has no distro-specific code."
}

# Read one KEY= line without sourcing the file, so a value containing shell
# metacharacters cannot execute anything.
env_get() {
  local key="$1" default="${2:-}" val=""
  if [[ -f "${ENV_FILE}" ]]; then
    val="$(grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2-)"
  fi
  printf '%s' "${val:-$default}"
}

# Append KEY=VALUE only when the key is absent. This is what makes an upgrade
# safe: a setting introduced in a later version appears with its default, and
# a value you have already chosen is never touched.
ensure_env_default() {
  local key="$1" value="$2"; shift 2
  [[ -f "${ENV_FILE}" ]] || return 0
  grep -qE "^${key}=" "${ENV_FILE}" && return 0
  { echo ""; for c in "$@"; do echo "# ${c}"; done; echo "${key}=${value}"; } \
    >> "${ENV_FILE}"
  echo "  relay.env: added ${key}=${value}"
}

gen_token() {
  openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# Every setting the relay understands, with its default. upgrade.sh walks
# this so a new release's settings land in an existing relay.env without
# anyone editing anything. Keep it in sync with relay.env.example.
apply_env_defaults() {
  ensure_env_default APRS_ENABLED true \
    "Run the APRS-IS feed (needs CALLSIGN). False if you only want grid data."
  ensure_env_default NETLOGGER_ENABLED true "Poll NetLogger for active nets"
  ensure_env_default REPEATERS_ENABLED true "Run the daily analog-repeater sweep"
  ensure_env_default RADIUS_KM 100 "APRS-IS filter radius, km"
  ensure_env_default ALLOWED_ORIGIN "*" "Browser origin allowed to read these feeds"
  ensure_env_default RECENTER_MIN_DELTA_KM 2 "Minimum move before the APRS filter re-centres"
  ensure_env_default RECENTER_COOLDOWN_SECONDS 20 "Cooldown between re-centres"
  ensure_env_default STATION_MAX_AGE_SECONDS 3600 "How long a heard station stays listed"
  ensure_env_default NETLOGGER_POLL_SECONDS 60 "NetLogger poll interval (60 is a floor in code)"
  ensure_env_default REPEATER_DEFAULT_RADIUS_KM 160 "Analog repeater search radius, km"
  ensure_env_default REPEATER_SWEEP_INTERVAL_HOURS 24 "How often the repeater sweep refreshes"

  ensure_env_default GRIDWATCH_ENABLED true \
    "Serve the Grid Watch feeds — KEV, DOE 202(c), NERC"
  ensure_env_default KEV_POLL_HOURS 1 "How often the CISA KEV catalog is refreshed"
  ensure_env_default DOE_POLL_HOURS 6 "How often the DOE 202(c) register is refreshed"
  ensure_env_default NERC_POLL_HOURS 6 "How often NERC is refreshed"

  ensure_env_default ISOALERTS_ENABLED true "Poll ISO/RTO operational alerts"
  ensure_env_default ISOALERTS_POLL_MINUTES 10 "How often ISO alerts are refreshed"
  ensure_env_default PJM_API_KEY "" \
    "PJM Data Miner 2 key — free for non-members; see relay.env.example for how"
  ensure_env_default PJM_API_KEY_HEADER "Ocp-Apim-Subscription-Key" \
    "Header the PJM key is sent in; undocumented by PJM, this is the likely default"
  ensure_env_default ISONE_API_USER "" "ISO-NE web services username (free account)"
  ensure_env_default ISONE_API_PASSWORD "" "ISO-NE web services password"

  ensure_env_default DIGEST_TOKEN "$(gen_token)" \
    "Bearer token required to POST a briefing digest (reads stay open)"
  ensure_env_default DIGEST_MAX 400 "How many daily digests to keep"

  ensure_env_default BRIEFMAIL_ENABLED false "Read the daily brief out of a mailbox"
  ensure_env_default BRIEFMAIL_MAILDIR "" "Local maildir path — no credentials needed"
  ensure_env_default BRIEFMAIL_IMAP_HOST "" "IMAP server holding the briefing mailbox"
  ensure_env_default BRIEFMAIL_IMAP_PORT 993 "IMAP port"
  ensure_env_default BRIEFMAIL_IMAP_SSL true "Connect to IMAP over TLS"
  ensure_env_default BRIEFMAIL_IMAP_USER "" "IMAP username"
  ensure_env_default BRIEFMAIL_IMAP_PASSWORD "" "IMAP password"
  ensure_env_default BRIEFMAIL_IMAP_FOLDER INBOX "IMAP folder to read"
  ensure_env_default BRIEFMAIL_ARCHIVE_FOLDER "" "Where a message goes once read"
  ensure_env_default BRIEFMAIL_POLL_MINUTES 10 "How often the mailbox is checked"
  ensure_env_default BRIEFMAIL_SUBJECT_MATCH "Grid Watch Digest" \
    "Only messages whose Subject contains this are considered"
  ensure_env_default BRIEFMAIL_FROM_ALLOW "" \
    "Optional sender allowlist (convenience only — From is forgeable)"
  ensure_env_default BRIEFMAIL_SECRET "$(gen_token)" \
    "Shared secret the brief's structured block must carry as its token field"
  ensure_env_default BRIEFMAIL_BACKFILL 20 \
    "When nothing is unread, re-examine this many recent messages"
  ensure_env_default BRIEFMAIL_KEEP_TEXT true "Store the brief's full text too"

  ensure_env_default LOG_LEVEL INFO "Log verbosity"
}

# relay.env is the only file here that holds secrets — a mailbox password,
# among others. Default permissions leave it world-readable. Applied on every
# run so an install predating this line is tightened by the next upgrade.
secure_env_file() {
  [[ -f "${ENV_FILE}" ]] || return 0
  chmod 600 "${ENV_FILE}"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}" 2>/dev/null || true
}

install_relay_code() {
  local src="$1"
  [[ -f "${src}" ]] || die "Relay source not found at ${src} — run this from a checkout of the repo."
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "${src}" \
    || die "${src} is not valid Python. Refusing to install it."
  install -m 644 "${src}" "${APP_DIR}/comms_watch_relay.py"
  install -m 644 "$(dirname "${src}")/requirements.txt" "${APP_DIR}/requirements.txt"
}
