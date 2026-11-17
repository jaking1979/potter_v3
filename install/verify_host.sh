#!/usr/bin/env bash
# Post-install checks. Prints PASS/FAIL per line; exit status is non-zero if any check fails.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"
set +e
fails=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then printf 'PASS  %s\n' "$name"; else printf 'FAIL  %s\n' "$name"; fails=$((fails+1)); fi; }
cfg() { oc config get "$1" 2>/dev/null; }

check "OpenClaw installed"                         oc --version
check "config validates"                           oc config validate
check "gateway healthy"                            oc health
check "Telegram locked to one allowed user"        bash -c "[ \"\$(openclaw ${OC_PROFILE_FLAG[*]:-} config get channels.telegram.dmPolicy | tr -d '\"')\" = allowlist ]"
check "Telegram groups disabled"                   bash -c "[ \"\$(openclaw ${OC_PROFILE_FLAG[*]:-} config get channels.telegram.groupPolicy | tr -d '\"')\" = disabled ]"
check "full toolset enabled"                       bash -c "openclaw ${OC_PROFILE_FLAG[*]:-} config get tools.profile | grep -q full"
check "settings page answers (when run as a service)" bash -c "! command -v systemctl >/dev/null || ! systemctl is-enabled potter-settings.service >/dev/null 2>&1 || curl -fsS -m 5 http://127.0.0.1:8789/ >/dev/null"
check "telemetry off"                              bash -c "openclaw ${OC_PROFILE_FLAG[*]:-} config get telemetry.enabled | grep -q false"
check "bot token file is root-only (600)"          bash -c "[ \"\$(stat -c %a '$SECRETS_DIR/telegram.token')\" = 600 ]"
check "secrets dir is root-only (700)"             bash -c "[ \"\$(stat -c %a '$SECRETS_DIR')\" = 700 ]"
check "no model or bot key inside openclaw.json"   bash -c "! grep -q -E 'sk-or-|[0-9]{6,}:[A-Za-z0-9_-]{20,}' \"\$(openclaw ${OC_PROFILE_FLAG[*]:-} config file)\""
check "openclaw.json is root-only (600)"           bash -c "[ \"\$(stat -c %a \"\$(openclaw ${OC_PROFILE_FLAG[*]:-} config file)\")\" = 600 ]"
check "Morning Brief job scheduled"                bash -c "openclaw ${OC_PROFILE_FLAG[*]:-} cron list 2>/dev/null | grep -q potter-morning-brief"
check "morning-brief skill present"                bash -c "openclaw ${OC_PROFILE_FLAG[*]:-} skills list 2>/dev/null | grep -q morning-brief"
check "security audit: 0 critical findings"       bash -c "openclaw ${OC_PROFILE_FLAG[*]:-} security audit 2>&1 | grep -q 'Summary: 0 critical'"
echo
[ "$fails" = 0 ] && { echo "All checks passed."; exit 0; } || { echo "$fails check(s) failed."; exit 1; }
