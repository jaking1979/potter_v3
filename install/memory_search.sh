#!/usr/bin/env bash
# Memory search (sourced by bootstrap.sh and update.sh): embeddings through the owner's OpenRouter key, read by
# OpenClaw from a root-only file (never written into openclaw.json), and the owner's own files indexed (preferences,
# personal rules, learnings, v2 reference) as long-lived sources. Needs: oc, q, SECRETS_DIR, and the key in $1.
potter_memory_search() {
  local key="$1" m
  [ -n "$key" ] || { warn "no OpenRouter key: memory search stays keyword-only"; return 0; }
  umask 077; printf '%s' "$key" > "$SECRETS_DIR/openrouter.key"; chmod 600 "$SECRETS_DIR/openrouter.key"; umask 022
  m="$(mktemp --suffix=.json5)"
  SECRETS_DIR="$SECRETS_DIR" python3 - "$m" <<'PY'
import json, os, sys
key_file = os.path.join(os.environ["SECRETS_DIR"], "openrouter.key")
json.dump({
    "secrets": {"providers": {"potterkeys": {"source": "file", "path": key_file, "mode": "singleValue"}}},
    "memory": {"search": {
        "provider": "openai-compatible", "model": "mongodb/voyage-4-lite",
        "remote": {"baseUrl": "https://openrouter.ai/api/v1/", "apiKey": {"source": "file", "provider": "potterkeys", "id": "value"}},
        "extraPaths": [{"path": p, "evergreen": True} for p in ("preferences", "rules", "learnings", "reference")],
    }},
}, open(sys.argv[1], "w"))
PY
  if q oc config patch --file "$m"; then
    oc memory index --force --agent potter >/dev/null 2>&1 || warn "memory index could not be rebuilt now; it rebuilds on the next search"
    echo "memory search: embeddings on, your files indexed"
  else
    warn "memory search could not be configured"
  fi
  rm -f "$m"
}
