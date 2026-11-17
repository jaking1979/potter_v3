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
    local out; out="$(oc memory index --force --agent potter 2>&1)" && \
      out="$(oc memory status --agent potter 2>&1)"
    if grep -q "Vector search: paused\|Index identity:" <<<"$out" || ! grep -q "Indexed:" <<<"$out"; then
      # say exactly what stopped it (never the key: only OpenClaw's own message lines)
      echo "memory search: configured, but the index did not rebuild: $(grep -v -i -E "deprecat|^\[secrets\]" <<<"$out" | grep -i -E "error|fail|index identity|paused|refused|401|402|403|404" | head -2 | sed 's/sk-[A-Za-z0-9_-]*/(key)/g' | tr '\n' ' ')"
    else
      echo "memory search: embeddings on, $(grep -o 'Indexed: [^·]*' <<<"$out" | head -1)"
    fi
  else
    warn "memory search could not be configured"
  fi
  rm -f "$m"
}
