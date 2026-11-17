#!/usr/bin/env bash
# Update a running Potter in place from the code in /opt/potter/src (or this checkout): no reinstall, no secrets
# re-entered. Installs or refreshes potter-core (v3 control + brain), Merrick's Jev decider, memory search and the
# workspace's shipped skills and rules.
#
# It never runs inside Potter's own turn: it hands itself to a separate system service, which waits a few seconds
# (so Potter's "update started" reply is delivered), stops the gateway (plugin installs and memory indexing need it
# stopped), does the work, starts the gateway, checks it, and reports to the owner on Telegram itself.
#   update.sh                      start the update in the background (what Potter runs)
#   update.sh --profile ZIP        also re-import the owner's profile from a v2 export (the zip is deleted afterwards)
#   update.sh --check              say what would change, change nothing
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
. "$HERE/lib.sh"
export PATH="$POTTER_HOME/openclaw/bin:$POTTER_HOME/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
CHECK=""; PROFILE_ZIP=""
while [ $# -gt 0 ]; do case "$1" in --check) CHECK=1;; --profile) PROFILE_ZIP="${2:-}"; shift;; esac; shift; done
[ "$(id -u)" = 0 ] || die "run as root"
SYSTEMD=""; command -v systemctl >/dev/null 2>&1 && [ -z "${POTTER_NO_DAEMON:-}" ] && SYSTEMD=1
if [ -z "$CHECK" ] && [ -n "$SYSTEMD" ] && [ -z "${POTTER_UPDATE_RUN:-}" ]; then
  unit="potter-update-$(date +%s)"
  systemd-run --unit="$unit" --collect --quiet --setenv=POTTER_UPDATE_RUN=1 --setenv=POTTER_HOME="$POTTER_HOME" \
    --setenv=POTTER_SECRETS_DIR="$SECRETS_DIR" bash "$HERE/update.sh" ${PROFILE_ZIP:+--profile "$PROFILE_ZIP"}
  echo "Update started in the background ($unit). In about 10 seconds the gateway stops for a minute or two; a"
  echo "Telegram message reports the result when it is back. Progress: journalctl -u $unit"
  exit 0
fi
notify() {   # straight to the owner's Telegram, no model involved; the token goes to curl on stdin, never argv
  [ -n "$SYSTEMD" ] || return 0
  local tok body; tok="$(cat "$SECRETS_DIR/telegram.token" 2>/dev/null)"; [ -n "$tok" ] || return 0
  body="$(mktemp)"; chmod 600 "$body"
  python3 -c 'import json,sys; json.dump({"chat_id": sys.argv[1], "text": sys.argv[2][:3900]}, open(sys.argv[3], "w"))' "$id" "$1" "$body"
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\nheader = "Content-Type: application/json"\ndata-binary = "@%s"\nsilent\nmax-time = 20\n' \
    "$tok" "$body" | curl --config - -o /dev/null || true
  rm -f "$body"
}
C="$(dirname "$(oc config file)")"; WS="$(oc config get agents.entries.potter.workspace | tr -d '"')"
id="$(oc config get channels.telegram.allowFrom | python3 -c 'import json,sys; print(json.load(sys.stdin)[0])')"
rev="$(git -C "$REPO" log -1 --format=%h 2>/dev/null || cat "$REPO/.potter-rev" 2>/dev/null || echo unknown)"
echo "Updating Potter from code $rev"
[ -n "$CHECK" ] && { echo "check only: would refresh potter-core, Merrick's Jev decider, memory search, shipped skills${PROFILE_ZIP:+, your profile}; restart the gateway"; exit 0; }
LOG=()
step() { echo "$1"; LOG+=("$1"); }
if [ -n "$SYSTEMD" ]; then
  trap 'systemctl start potter-gateway; notify "Potter update FAILED at: $(tail -n1 <<<"${LOG[*]:-start}"). The gateway was restarted on the old setup. Details: journalctl -u potter-update-*"' ERR
  sleep 8                                                     # let Potter's "update started" reply go out
  systemctl stop potter-gateway
fi

# 1. potter-core plugin
d="$(mktemp -d)"; cp -r "$REPO/overlay/plugins/potter-core/"{index.js,lib,openclaw.plugin.json,package.json} "$d/"
install_plugin "$d" potter-core --force; rm -rf "$d"
p="$(mktemp --suffix=.json5)"
python3 - "$id" "$C" "$WS" "$SECRETS_DIR" "$p" <<'PY'
import json, os, sys
owner, state, ws, secrets, out = sys.argv[1:6]
cfg = {"mode": os.environ.get("POTTER_CONTROL_MODE", "enforce"), "agents": ["potter"], "ownerIds": [owner],
       "standingGrants": [], "stateDir": os.path.join(state, "potter-core"),
       "jevEnvFile": os.path.join(secrets, "jev.env"), "policyPath": os.path.join(ws, "state", "model-policy.json"),
       "governor": {"enabled": True, "jev": True, "timeoutMs": 2500}, "fastPath": {"enabled": True, "merrick": "merrick"}}
json.dump({"plugins": {"entries": {"potter-core": {"enabled": True, "hooks": {"allowConversationAccess": True}}}}}, open(out, "w"))
json.dump(cfg, open(out + ".cfg", "w"))
PY
q oc config patch --file "$p"
# keep standing grants the owner already gave ("take over X")
python3 - "$p.cfg" "$(oc config get plugins.entries.potter-core.config 2>/dev/null || echo '{}')" <<'PY'
import json, sys
new = json.load(open(sys.argv[1]))
try:
    old = json.loads(sys.argv[2])
except ValueError:
    old = {}
new["standingGrants"] = old.get("standingGrants") or []
new["mode"] = old.get("mode", new["mode"])
json.dump({"plugins": {"entries": {"potter-core": {"config": new}}}}, open(sys.argv[1], "w"))
PY
q oc config patch --file "$p.cfg" --replace-path plugins.entries.potter-core.config; rm -f "$p" "$p.cfg"
a="$(mktemp --suffix=.json5)"
oc plugins list --json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
ids = sorted({p["id"] for p in d["plugins"] if p.get("origin") == "bundled" or p.get("enabled")})
json.dump({"plugins": {"allow": ids}}, open(sys.argv[1], "w"))' "$a"
q oc config patch --file "$a" --replace-path plugins.allow; rm -f "$a"
step "potter-core: installed and configured"

# 1b. model policy: replaced only if it is still a shipped one (an edited policy is the owner's and is kept)
OLD_SHIPPED="$(sed -n 's/^OLD_SHIPPED_POLICIES="\(.*\)"$/\1/p' "$HERE/bootstrap.sh")"
P="$WS/state/model-policy.json"
if [ -f "$P" ] && ! cmp -s "$P" "$REPO/overlay/jev/policy.json"; then
  h="$(sha256sum "$P" | cut -c1-64)"
  if { [ -f "$WS/state/model-policy.shipped.json" ] && cmp -s "$P" "$WS/state/model-policy.shipped.json"; } || \
     case " $OLD_SHIPPED " in *" $h "*) true;; *) false;; esac; then
    cp "$REPO/overlay/jev/policy.json" "$P"; cp "$REPO/overlay/jev/policy.json" "$WS/state/model-policy.shipped.json"
    m="$(mktemp --suffix=.json5)"
    python3 "$HERE/apply_models.py" "$P" --patch "$m" --agents potter$(oc agents list 2>/dev/null | grep -q merrick && echo ,merrick) \
      && q oc config patch --file "$m" --replace-path agents.defaults.models
    rm -f "$m"; step "model policy: updated to the shipped version"
  else
    step "model policy: yours was edited, kept as it is (the new default is overlay/jev/policy.json)"
  fi
fi

# 2. Jev for Merrick
mkdir -p "$POTTER_HOME/jev"; cp "$REPO/overlay/jev/"{client.js,policy.js,telemetry.js,index.js,merrick-decide.mjs} "$POTTER_HOME/jev/"
if [ -f /usr/local/bin/merrick ] && ! grep -q MERRICK_DECIDE_CMD /usr/local/bin/merrick; then
  sed -i "/^exec /i export MERRICK_DECIDE_CMD=\"node $POTTER_HOME/jev/merrick-decide.mjs\"\nexport POTTER_JEV_ENV=$SECRETS_DIR/jev.env\nexport POTTER_MODEL_POLICY=$WS/state/model-policy.json" /usr/local/bin/merrick
fi
[ -d "$POTTER_HOME/merrick" ] && { rm -rf "$POTTER_HOME/merrick"; cp -r "$REPO/overlay/merrick" "$POTTER_HOME/merrick"; rm -f "$POTTER_HOME/merrick/testing.py"; }
mkdir -p "$POTTER_HOME/tools"; cp "$REPO/overlay/tools/"*.py "$POTTER_HOME/tools/"
step "merrick: Jev decider wired"

# 2b. memory search, with the key already saved for Jev (no key is re-entered)
. "$HERE/memory_search.sh"
LOG+=("$(potter_memory_search "$(sed -n 's/^OPENROUTER_API_KEY=//p' "$SECRETS_DIR/jev.env" 2>/dev/null)" | tail -n1)")

# 3. shipped skills and global rules (personal rules and the index's personal rows are not touched)
for s in "$REPO/overlay/workspace/skills/"*/; do n="$(basename "$s")"; mkdir -p "$WS/skills/$n"; cp -r "$s". "$WS/skills/$n/"; done
cp "$REPO/overlay/workspace/rules/global/"*.md "$WS/rules/global/"
step "skills and global rules: refreshed"
if [ -n "$PROFILE_ZIP" ] && [ -f "$PROFILE_ZIP" ]; then
  python3 "$HERE/profile_from_v2.py" apply "$PROFILE_ZIP" --workspace "$WS" --state-dir /etc/potter > /etc/potter/profile-report.json \
    && step "profile: re-imported from your v2 export ($(python3 -c 'import json;print(len(json.load(open("/etc/potter/profile-report.json"))["active_files"]))') personal files, including USER.md)" \
    || step "profile: could not be re-imported (see /etc/potter/profile-report.json)"
  shred -u "$PROFILE_ZIP" 2>/dev/null || rm -f "$PROFILE_ZIP"
fi
echo "$rev" > "$POTTER_HOME/.potter-rev"

# 4. start the gateway again and check it
if [ -n "$SYSTEMD" ]; then
  systemctl start potter-gateway
  for _ in $(seq 1 60); do oc health >/dev/null 2>&1 && break; sleep 2; done
  trap - ERR
  hooks="$(oc plugins inspect potter-core --runtime --json 2>/dev/null | grep -o '"name": *"[a-z_]*"' | wc -l)"
  vh="$("$HERE/verify_host.sh" 2>/dev/null || true)"
  health="$(grep -c '^PASS' <<<"$vh" || true)/$(grep -c -E '^(PASS|FAIL)' <<<"$vh" || true)"
  fails="$(grep '^FAIL' <<<"$vh" | sed 's/^FAIL  /failed: /' || true)"
  notify "Potter updated to $rev.
$(printf '%s\n' "${LOG[@]}")
potter-core hooks: $hooks. Health checks passed: $health.${fails:+
$fails}"
  echo "done: health $health"
fi
