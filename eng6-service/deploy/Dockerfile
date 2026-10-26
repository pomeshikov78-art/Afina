# ═══════════════════════════════════════════════════════════════════════════
# ENG6-PLAN — промышленный образ сервиса планирования систем связи
#
# Multi-stage: тесты и конформанс выполняются НА ЭТАПЕ СБОРКИ — образ с
# непройденным конформанс-протоколом ENG6 физически не собирается.
#
#   docker build -f deploy/Dockerfile -t eng6-plan:1.0.0 .
#   docker run --rm -p 8080:8080 --read-only --tmpfs /app/var \
#     -e ENG6_ENV=production -e ENG6_TLS_ENABLED=true ... eng6-plan:1.0.0
# ═══════════════════════════════════════════════════════════════════════════

# ── Стадия 1: зависимости и проверки (тяжёлый образ, в результат не идёт) ──
FROM node:20-bookworm-slim AS build

WORKDIR /app
# Node-зависимостей нет (package.json: dependencies = {}), поэтому npm ci
# не требуется; копия package.json нужна только для метаданных образа
COPY package.json ./
COPY core ./core
COPY api ./api
COPY scripts ./scripts
COPY tests ./tests
COPY loadtest ./loadtest
COPY frontend ./frontend

# Конформанс вендорного ядра ENG6 (33 эталонные проверки + 5 регрессионных
# сценариев) и unit-тесты — барьер сборки.
# Тесты исполнения UI в jsdom автоматически пропускаются: jsdom — единственная
# dev-зависимость, в образ она не устанавливается (принцип zero-dependency).
RUN node --test tests/unit tests/conformance

# Сборка single-file UI с встроенным результатом расчёта ядра
RUN node scripts/build_frontend.mjs

# ── Стадия 2: runtime ────────────────────────────────────────────────────
FROM node:20-bookworm-slim AS runtime

# tini — корректная обработка SIGTERM/SIGCHLD (graceful shutdown, зомби)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь (требование эксплуатации, NFR-SEC)
RUN groupadd --system --gid 1001 eng6 && useradd --system --uid 1001 --gid eng6 --home-dir /app --shell /usr/sbin/nologin eng6

WORKDIR /app
ENV NODE_ENV=production \
    ENG6_ENV=production \
    ENG6_HOST=0.0.0.0 \
    ENG6_PORT=8080 \
    ENG6_AUDIT_FILE=/app/var/audit.ndjson \
    ENG6_STATIC_DIR=/app/frontend \
    # ядро не должно расти в памяти бесконечно: воркер перезапускается пулом
    ENG6_WORKER_RESTART_AFTER=200 \
    UV_THREADPOOL_SIZE=4

COPY --from=build --chown=eng6:eng6 /app/package.json ./
COPY --from=build --chown=eng6:eng6 /app/core ./core
COPY --from=build --chown=eng6:eng6 /app/api ./api
COPY --from=build --chown=eng6:eng6 /app/scripts ./scripts
COPY --from=build --chown=eng6:eng6 /app/frontend/index.html ./frontend/index.html
COPY --from=build --chown=eng6:eng6 /app/loadtest ./loadtest

# Каталог журнала аудита (в production монтируется том, либо --tmpfs /app/var)
RUN mkdir -p /app/var && chown -R eng6:eng6 /app/var

USER eng6
EXPOSE 8080

# Liveness: процесс жив. Readiness: воркеры готовы и очередь не переполнена.
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.ENG6_PORT||8080)+'/readyz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "--max-old-space-size=1536", "api/main.mjs"]
