# Shared helpers for the Potter installer. Sourced, not executed.
set -euo pipefail

POTTER_HOME="${POTTER_HOME:-/opt/potter}"
SECRETS_DIR="${POTTER_SECRETS_DIR:-/etc/potter/secrets}"
OC_PROFILE_FLAG=()
[ -n "${POTTER_OPENCLAW_PROFILE:-}" ] && OC_PROFILE_FLAG=(--profile "$POTTER_OPENCLAW_PROFILE")

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mxx %s\033[0m\n' "$*" >&2; exit 1; }
oc()   { openclaw "${OC_PROFILE_FLAG[@]}" "$@"; }

# q CMD...  - run quietly; on failure show the command's output so an install never dies silently.
q() { local out; if ! out="$("$@" 2>&1)"; then printf '%s\n' "$out" >&2; return 1; fi; }
# install_plugin SOURCE ID [extra flags]  - install and confirm the plugin really landed.
install_plugin() {
  local src="$1" id="$2"; shift 2
  local out; out="$(openclaw "${OC_PROFILE_FLAG[@]}" plugins install "$src" --accept-capabilities "$@" 2>&1)" || true
  printf '%s' "$out" | grep -q "Installed plugin: $id" || { printf '%s\n' "$out" >&2; die "plugin $id did not install"; }
  q oc plugins enable "$id"
}

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

# ask VAR "Prompt" [default] [secret]  - whiptail box when a terminal is available, plain prompt otherwise.
# A value already present in the environment (POTTER_<VAR>) is used as-is, so the installer is scriptable.
ask() {
  local var="$1" prompt="$2" default="${3:-}" secret="${4:-}" val=""
  local pre="POTTER_${var}"
  if [ -n "${!pre:-}" ]; then printf -v "$var" '%s' "${!pre}"; return; fi
  if [ -t 0 ] && command -v whiptail >/dev/null 2>&1; then
    if [ "$secret" = secret ]; then
      val=$(whiptail --title "Potter setup" --passwordbox "$prompt" 11 70 3>&1 1>&2 2>&3) || die "setup cancelled"
    else
      val=$(whiptail --title "Potter setup" --inputbox "$prompt" 11 70 "$default" 3>&1 1>&2 2>&3) || die "setup cancelled"
    fi
  elif [ -t 0 ]; then
    if [ "$secret" = secret ]; then read -r -s -p "$prompt: " val; echo
    else read -r -p "$prompt [$default]: " val; fi
  else
    die "no value for $pre and no terminal to ask"
  fi
  printf -v "$var" '%s' "${val:-$default}"
}

# render_overlay IN OUT  - replace @@NAME@@ placeholders from the environment, refuse leftovers.
render_overlay() {
  python3 - "$1" "$2" <<'PY'
import os, re, sys
src = open(sys.argv[1], encoding="utf-8").read()
def sub(m):
    key = m.group(1)
    if key not in os.environ:
        sys.exit(f"unfilled placeholder @@{key}@@")
    return os.environ[key]
open(sys.argv[2], "w", encoding="utf-8").write(re.sub(r"@@([A-Z_]+)@@", sub, src))
PY
}

# collect_inputs - the setup form. Fills OWNER_NAME, TIMEZONE, OWNER_TELEGRAM_ID, TELEGRAM_TOKEN, OPENROUTER_KEY, BRIEF_TIME.
collect_inputs() {
  ask OWNER_NAME        "Your first name"                                   "" 
  ask TIMEZONE          "Your time zone (IANA, e.g. America/New_York)"      "America/New_York"
  ask OWNER_TELEGRAM_ID "Your numeric Telegram user ID"                     ""
  ask TELEGRAM_TOKEN    "Telegram bot token from @BotFather"                "" secret
  ask OPENROUTER_KEY    "Model API key (OpenRouter, with a spend cap set)"  "" secret
  ask BRIEF_TIME        "Morning Brief time, 24h HH:MM (weekdays)"          "08:30"
  COMPOSIO_MCP_URL="${POTTER_COMPOSIO_MCP_URL:-}"; AGENTMAIL_API_KEY="${POTTER_AGENTMAIL_API_KEY:-}"; TAILSCALE_AUTHKEY="${POTTER_TAILSCALE_AUTHKEY:-}"
  OPENAI_VOICE_KEY="${POTTER_OPENAI_VOICE_KEY:-}"; EXCLUDED_IDS="${POTTER_EXCLUDED_IDS:-}"; WORK_CALENDAR="${POTTER_WORK_CALENDAR:-}"; OLLAMA_URL="${POTTER_OLLAMA_URL:-}"; AGENTMAIL_INBOX="${POTTER_AGENTMAIL_INBOX:-}"; OWNER_EMAILS="${POTTER_OWNER_EMAILS:-}"; CO_OWNER_EMAILS="${POTTER_CO_OWNER_EMAILS:-}"; HA_URL="${POTTER_HA_URL:-}"; HA_TOKEN="${POTTER_HA_TOKEN:-}"
  [[ "$OWNER_TELEGRAM_ID" =~ ^[0-9]{4,15}$ ]] || die "Telegram user ID must be a number"
  [[ "$BRIEF_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "brief time must be HH:MM"
  python3 -c "import zoneinfo,sys; zoneinfo.ZoneInfo(sys.argv[1])" "$TIMEZONE" || die "unknown time zone"
}
