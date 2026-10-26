#!/usr/bin/env bash
# unified/scripts/start_stack.sh — локальный подъём совмещённого стека (без Docker).
#
#   ENGINE_PORT=8080 PLATFORM_PORT=8470 ./start_stack.sh
#
# Поднимает: engine (ENG6-PLAN, Node) + platform («Афина», Python).
# PID и URL пишутся в unified/var/stack.env; остановка — stop_stack.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIFIED="$(dirname "$HERE")"
REPO="$(dirname "$UNIFIED")"
VAR="$UNIFIED/var"
mkdir -p "$VAR"

ENGINE_PORT="${ENGINE_PORT:-8080}"
PLATFORM_PORT="${PLATFORM_PORT:-8470}"
ENGINE_URL="http://127.0.0.1:$ENGINE_PORT"
PLATFORM_URL="http://127.0.0.1:$PLATFORM_PORT"

command -v node >/dev/null || { echo "нужен node >= 20.11"; exit 2; }
command -v python3 >/dev/null || { echo "нужен python3 >= 3.11"; exit 2; }

# --- engine (линия B) ---
if [ -z "${ENG6_NO_START:-}" ]; then
  (cd "$REPO/eng6-service" && \
   ENG6_PORT="$ENGINE_PORT" ENG6_HOST=127.0.0.1 ENG6_SERVE_UI=true ENG6_LOG_LEVEL=info \
   nohup node api/main.mjs > "$VAR/engine.log" 2>&1 & echo $! > "$VAR/engine.pid")
  echo "engine: pid $(cat "$VAR/engine.pid") → $ENGINE_URL (лог: unified/var/engine.log)"
fi

# --- platform (линия A) ---
if [ -z "${AFINA_NO_START:-}" ]; then
  (cd "$REPO/afina/src" && \
   AFINA_ENG6_URL="$ENGINE_URL" \
   nohup python3 -m afina.server.app --db "$VAR/afina.db" --port "$PLATFORM_PORT" \
     --requirements "$REPO/afina/data/Афина_Реестр_требований_ПриложениеА.json" \
     --tz "$REPO/afina/Афина_Техническое_задание.md" > "$VAR/platform.log" 2>&1 & echo $! > "$VAR/platform.pid")
  echo "platform: pid $(cat "$VAR/platform.pid") → $PLATFORM_URL (лог: unified/var/platform.log)"
fi

# --- ожидание готовности ---
wait_ok() { # url, имя, сек
  local url="$1" name="$2" deadline=$((SECONDS + ${3:-60}))
  while [ $SECONDS -lt $deadline ]; do
    if python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('$url',timeout=3).status==200 else 1)" 2>/dev/null; then
      echo "$name: готов"; return 0
    fi
    sleep 0.5
  done
  echo "$name: НЕ поднялся за ${3:-60} с — см. лог"; return 1
}
RC=0
[ -z "${ENG6_NO_START:-}" ] && { wait_ok "$ENGINE_URL/healthz" engine 60 || RC=1; }
[ -z "${AFINA_NO_START:-}" ] && { wait_ok "$PLATFORM_URL/healthz" platform 60 || RC=1; }

cat > "$VAR/stack.env" <<EOF
ENGINE_PID=$(cat "$VAR/engine.pid" 2>/dev/null || true)
PLATFORM_PID=$(cat "$VAR/platform.pid" 2>/dev/null || true)
ENGINE_URL=$ENGINE_URL
PLATFORM_URL=$PLATFORM_URL
AFINA_ENG6_URL=$ENGINE_URL
EOF
echo "stack.env: $VAR/stack.env"
exit $RC
