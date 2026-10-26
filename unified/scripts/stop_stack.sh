#!/usr/bin/env bash
# unified/scripts/stop_stack.sh — остановка стека, поднятого start_stack.sh.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAR="$(dirname "$HERE")/var"
[ -f "$VAR/stack.env" ] && . "$VAR/stack.env"

for name in engine platform; do
  pid="$(cat "$VAR/$name.pid" 2>/dev/null || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.3; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    echo "$name (pid $pid): остановлен"
  else
    echo "$name: не запущен"
  fi
  rm -f "$VAR/$name.pid"
done
rm -f "$VAR/stack.env"
