#!/usr/bin/env bash
# Update a running Potter in place from the code in /opt/potter/src (or this checkout): no reinstall, no secrets
# re-entered. Installs or refreshes potter-core (v3 control + brain), Merrick's Jev decider and the workspace's
# shipped skills and rules, then restarts the gateway a few seconds AFTER it has printed its report, so Potter can
# run this itself and still answer.
#   update.sh            apply
#   update.sh --check    say what would change, change nothing
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
. "$HERE/lib.sh"
export PATH="$POTTER_HOME/openclaw/bin:$POTTER_HOME/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
CHECK=""; [ "${1:-}" = --check ] && CHECK=1
[ "$(id -u)" = 0 ] || die "run as root"
C="$(dirname "$(oc config file)")"; WS="$(oc config get agents.entries.potter.workspace | tr -d '"')"
id="$(oc config get channels.telegram.allowFrom | python3 -c 'import json,sys; print(json.load(sys.stdin)[0])')"
rev="$(git -C "$REPO" log -1 --format=%h 2>/dev/null || cat "$REPO/.potter-rev" 2>/dev/null || echo unknown)"
echo "Updating Potter from code $rev"
[ -n "$CHECK" ] && { echo "check only: would refresh potter-core, Merrick's Jev decider, shipped skills; restart the gateway"; exit 0; }

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
echo "potter-core: installed and configured"

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
    rm -f "$m"; echo "model policy: updated to the shipped version"
  else
    echo "model policy: yours was edited, kept as it is (the new default is overlay/jev/policy.json)"
  fi
fi

# 2. Jev for Merrick
mkdir -p "$POTTER_HOME/jev"; cp "$REPO/overlay/jev/"{client.js,policy.js,telemetry.js,index.js,merrick-decide.mjs} "$POTTER_HOME/jev/"
if [ -f /usr/local/bin/merrick ] && ! grep -q MERRICK_DECIDE_CMD /usr/local/bin/merrick; then
  sed -i "/^exec /i export MERRICK_DECIDE_CMD=\"node $POTTER_HOME/jev/merrick-decide.mjs\"\nexport POTTER_JEV_ENV=$SECRETS_DIR/jev.env\nexport POTTER_MODEL_POLICY=$WS/state/model-policy.json" /usr/local/bin/merrick
fi
[ -d "$POTTER_HOME/merrick" ] && { rm -rf "$POTTER_HOME/merrick"; cp -r "$REPO/overlay/merrick" "$POTTER_HOME/merrick"; rm -f "$POTTER_HOME/merrick/testing.py"; }
mkdir -p "$POTTER_HOME/tools"; cp "$REPO/overlay/tools/"*.py "$POTTER_HOME/tools/"
echo "merrick: Jev decider wired"

# 3. shipped skills and global rules (personal rules and the index's personal rows are not touched)
for s in "$REPO/overlay/workspace/skills/"*/; do n="$(basename "$s")"; mkdir -p "$WS/skills/$n"; cp -r "$s". "$WS/skills/$n/"; done
cp "$REPO/overlay/workspace/rules/global/"*.md "$WS/rules/global/"
echo "skills and global rules: refreshed"
echo "$rev" > "$POTTER_HOME/.potter-rev"

# 4. restart after this report has been delivered
if command -v systemctl >/dev/null 2>&1 && [ -z "${POTTER_NO_DAEMON:-}" ]; then
  nohup bash -c 'sleep 8; systemctl restart potter-gateway' >/dev/null 2>&1 &
  echo "The gateway restarts in about 8 seconds to load the new code. Then: openclaw plugins inspect potter-core --runtime"
fi
