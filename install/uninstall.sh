#!/usr/bin/env bash
# Removes Potter from this machine: stops the gateway, deletes OpenClaw state and Potter's files.
# It never touches anything outside /opt/potter, /etc/potter and OpenClaw's own state directory.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"
[ "${1:-}" = "--yes" ] || { read -r -p "This deletes Potter, its memory and its settings from this machine. Type DELETE to continue: " a; [ "$a" = DELETE ] || die "cancelled"; }
command -v openclaw >/dev/null 2>&1 && oc uninstall --yes --all >/dev/null 2>&1 || true
rm -rf "$POTTER_HOME" "$SECRETS_DIR" /usr/local/bin/openclaw /usr/local/bin/merrick /usr/local/bin/potter-kg-curate
say "Potter removed. (Node, if installed by Potter, lived under $POTTER_HOME and is gone too.)"
