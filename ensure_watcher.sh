#!/usr/bin/env bash
# Start inbox_watcher.py in the background if it is not already running, then
# print its status. Safe to call often (from cron, a heartbeat job or a login
# script): the watcher's own pid lock keeps a single instance.
# Extra arguments (e.g. --allow-insecure-http) are passed to the watcher.
# Uses the current environment (WAKE_URL, WAKE_AUTH, AIW_*). Never echoes them.
# With AIW_REQUIRE_LIVE=1 (or --require-live) it refuses to start without credentials and exits 4,
# and the final --status call exits 4 if a running watcher is in DRY-RUN.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/inbox_watcher.py"
STATE="${AIW_STATE_DIR:-$PWD/state}"
mkdir -p "$STATE"
# Normalise AIW_REQUIRE_LIVE the way the watcher does (case-insensitive,
# whitespace ignored) and honour a --require-live argument too.
REQ="$(printf '%s' "${AIW_REQUIRE_LIVE:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
for a in "$@"; do [ "$a" = "--require-live" ] && REQ=1; done
case "$REQ" in 1|true|yes|on) export AIW_REQUIRE_LIVE=1 ;; esac
if python3 "$PY" --status >/dev/null 2>&1; then
  echo "inbox_watcher already running."
else
  if [ -z "${WAKE_URL:-}" ] || [ -z "${WAKE_AUTH:-}" ]; then
    case "${AIW_REQUIRE_LIVE:-}" in
      1|true|yes|on)
        echo "ERROR: AIW_REQUIRE_LIVE is set but WAKE_URL and/or WAKE_AUTH are missing; not starting." >&2
        exit 4 ;;
    esac
    echo "WARNING: WAKE_URL and/or WAKE_AUTH not set; the watcher will run in DRY-RUN mode."
  fi
  AIW_STATE_DIR="$STATE" nohup python3 "$PY" "$@" >>"$STATE/watcher.nohup.out" 2>&1 </dev/null &
  sleep 2
  echo "inbox_watcher started."
fi
AIW_STATE_DIR="$STATE" python3 "$PY" --status
