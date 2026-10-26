# ЭКСПЛУАТАЦИЯ сервиса ENG6-PLAN

## 1. Требования к окружению

| Параметр | Минимум | Рекомендуется | Обоснование |
|---|---|---|---|
| Node.js | 20.11 | 20 LTS | `worker_threads`, `vm.createContext` с `codeGeneration` |
| CPU | 2 vCPU | 4–8 vCPU | пул воркеров = CPU − 1 |
| RAM | 2 ГБ | 8 ГБ | ~434 МБ на расчёт 10⁵ узлов + ~150 МБ на воркер |
| Диск | 1 ГБ | 10 ГБ SSD | журнал аудита (NDJSON) + результаты задач |
| Сеть | — | TLS 1.2+ | NFR-02: передача только по защищённому каналу |
| ОС | Linux x64 | Linux x64 (container) | Docker/k8s |

**Внешние зависимости отсутствуют**: `dependencies: {}` в `package.json`.
Вендор ENG6, Prometheus-клиент и HTTP-слой реализованы средствами Node.

## 2. Быстрый старт

```bash
# 1. Запуск без контейнера
node api/main.mjs                       # http://localhost:8080
curl -s localhost:8080/api/v1/meta | head -c 400

# 2. Расчёт (обязателен заголовок X-Requestor, кириллица — percent-encoding)
curl -s -X POST localhost:8080/api/v1/plan/sync \
  -H 'Content-Type: application/json' \
  -H "X-Requestor: $(printf 'Иванов И.И.' | jq -sRr @uri)" \
  --data @examples/battalion.json | jq '.status, .metrics'

# 3. Тесты и конформанс
npm test                                # 125 тестов (unit + conformance + jsdom-стенд UI)
npm run conformance                     # протокол ENG6 (33 + 5)

# 4. Нагрузочный стенд
npm run bench                           # quick-профиль (~1 мин)
npm run bench:full                      # full: ядро до 10⁵ узлов + API + burst

# 5. Пересборка UI (с новым встроенным расчётом ядра)
node scripts/build_frontend.mjs
```

## 3. Контейнерное развертывание

```bash
# сборка (тесты и конформанс выполняются ВНУТРИ сборки — непройденный
# конформанс ENG6 означает, что образ не соберётся)
docker build -f deploy/Dockerfile -t eng6-plan:1.0.0 .

# запуск: read-only корень, non-root, сброшенные capabilities
docker run -d --name eng6-plan -p 8080:8080 \
  --read-only --tmpfs /app/var:size=256M \
  -e ENG6_ENV=production -e ENG6_API_KEYS="$KEY1,$KEY2" \
  -e ENG6_TLS_ENABLED=true -e ENG6_TLS_CERT=/app/certs/tls.crt -e ENG6_TLS_KEY=/app/certs/tls.key \
  -v "$PWD/certs:/app/certs:ro" \
  --security-opt no-new-privileges --cap-drop ALL \
  --memory 6g --cpus 4 \
  eng6-plan:1.0.0

# полный стек: сервис + Prometheus + Grafana
echo "ENG6_API_KEYS=$(openssl rand -hex 32)" > deploy/.env
docker compose -f deploy/docker-compose.yml up -d --build
#  UI          http://localhost:8080
#  Prometheus  http://localhost:9090  (цель eng6-plan настроена)
#  Grafana     http://localhost:3000  (дашборд «ENG6-PLAN» подхватывается)
```

## 4. Kubernetes

```bash
kubectl create namespace eng6
kubectl -n eng6 create secret generic eng6-plan-secrets \
  --from-literal=ENG6_API_KEYS="$(openssl rand -hex 32)" \
  --from-literal=GRAFANA_PASSWORD="$(openssl rand -hex 16)" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/k8s/eng6-plan.yaml
kubectl -n eng6 rollout status deploy/eng6-plan
```

Манифест содержит: Deployment (2 реплики, `readOnlyRootFilesystem`,
`runAsNonRoot`, `capabilities: drop ALL`, `seccompProfile: RuntimeDefault`,
PVC для аудита), Service, **HPA по CPU и по длине очереди расчёта**
(`eng6_queue_length`, требует prometheus-adapter), PodDisruptionBudget,
NetworkPolicy (egress только DNS — ядро headless), ServiceMonitor, Ingress с TLS.

## 5. Конфигурация (переменные окружения)

### 5.1 Пул расчёта и лимиты

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ENG6_WORKERS` | CPU − 1 | число воркеров; `1` = «честно по одному» |
| `ENG6_MAX_QUEUE` | 512 | длина очереди; переполнение → `429 QUEUE_FULL` |
| `ENG6_MAX_SYNC_CONCURRENCY` | CPU − 1 | sync-слоты; исчерпание → `429 SYNC_BUSY` |
| `ENG6_TASK_TIMEOUT_MS` | 300000 | таймаут задачи → `terminate` воркера и пересоздание |
| `ENG6_WORKER_HEAP_MB` | авто | лимит кучи воркера; авто = `totalmem/workers/2`, 192…1536 МБ |
| `ENG6_WORKER_RESTART_AFTER` | 500 | перезапуск воркера после N задач (гигиена кучи) |
| `ENG6_MAX_INFLIGHT_BYTES` | 256 МБ | память тел «в полёте»; превышение → `503 MEMORY_BACKPRESSURE` |
| `ENG6_RESULT_TTL_MS` / `ENG6_MAX_STORED_RESULTS` | 1 ч / 256 | хранение результатов (LRU) |
| `ENG6_MAX_BODY_BYTES` | 64 МБ | предел тела запроса → `413` |
| `ENG6_LIMIT_NODES` / `_LINKS` / `_DEMANDS` | 200 000 / 800 000 / 20 000 | лимиты экземпляра → `422` |

### 5.2 Безопасность и аудит

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ENG6_ENV` | development | `production` включает fail-fast проверки |
| `ENG6_AUTH_ENABLED` / `ENG6_API_KEYS` | false / — | Bearer или `X-Api-Key` |
| `ENG6_REQUIRE_REQUESTOR` | true | обязательный `X-Requestor` (аудит) |
| `ENG6_TLS_ENABLED` / `_CERT` / `_KEY` / `_CA` / `_REQUIRE_CLIENT_CERT` | false | TLS и mTLS |
| `ENG6_AUDIT_ENABLED` / `_FILE` / `_RING` / `_LOG_PAYLOAD` | true / `var/audit.ndjson` / 2000 / false | журнал аудита |
| `ENG6_CORS_ORIGIN` | `*` | для UI на другом хосте |
| `ENG6_TRUST_PROXY` | false | учитывать `X-Forwarded-For` в rate limit |

### 5.3 Rate limit и метрики

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ENG6_RATELIMIT_ENABLED` | true | ограничитель частоты |
| `ENG6_RL_REQUESTS` / `_WINDOW_MS` | 120 / 60000 | запросов в окне |
| `ENG6_RL_HEAVY_NODES` / `_HEAVY_WEIGHT` | 5000 / 5 | тяжёлые графы тарифицируются с весом |
| `ENG6_METRICS_ENABLED` / `_PATH` / `_BUCKETS` | true / `/metrics` / 10…300000 мс | Prometheus |

### 5.4 Отладка

| Переменная | Эффект |
|---|---|
| `ENG6_STRICT=1` | в ответе `ERROR` появляется стек исключения |
| `LOAD_VERBOSE=1` | нагрузочный стенд печатает stdout/stderr сервиса |

> **Важно.** `ENG6_ENV=production` без TLS не запускается: `assertConfig()` возвращает
> нарушение NFR-02, и `listen()` бросает исключение до открытия порта.

## 5.5. Коннектор к учётной системе (Путь А)

```bash
# JSON-экспорт ERP (модель {sites, assets, circuits, orders})
node integrations/erp_connector.mjs \
  --from=examples/erp_export.json \
  --api=http://eng6-api:8080/api/v1 \
  --requestor="Диспетчер АСУ Соколов П.П." \
  --key="$ENG6_API_KEY" \
  --out=var/erp_result.json --mode=auto

# CSV-выгрузка (секции [nodes]/[links] либо плоский лист)
node integrations/erp_connector.mjs --from=examples/erp_sites.csv --out=var/erp_csv.json

# контроль собранного пакета без расчёта
node integrations/erp_connector.mjs --from=examples/erp_export.json --dump-request=var/req.json
```

| Ключ | Назначение |
|---|---|
| `--from` | источник: JSON-запрос ENG6-PLAN, ERP-модель или CSV |
| `--api` | базовый URL API (или `ENG6_API`) |
| `--key` | API-ключ (или `ENG6_API_KEY`) |
| `--requestor` | кто инициировал расчёт — попадает в журнал аудита |
| `--mode` | `sync` / `async` / `auto` (по числу узлов) |
| `--autoNodes` | порог перехода на async, по умолчанию 3 000 |
| `--retries`, `--pollMs`, `--pollMaxMs` | повторы и опрос задачи |
| `--out` | файл результата для системы диспетчеризации |
| `--dump-request` | выгрузить собранный пакет и завершиться (контроль интеграции) |

Коды возврата: `0` — план построен, `2` — план невыполним (`INFEASIBLE`/`TIMEOUT`),
`3` — ошибка интеграции (ядро недоступно, некорректный источник).

## 6. Мониторинг

### 6.1 Ключевые метрики

| Метрика | Тип | Порог / алерт |
|---|---|---|
| `eng6_plan_duration_ms_bucket` | histogram | p95 > 5 с → `Eng6PlanLatencyHigh` |
| `eng6_queue_length` | gauge | > 32 в течение 5 мин → `Eng6QueueGrowing` |
| `eng6_queue_rejected_total` | counter | > 0,5/с → `Eng6QueueRejectsHigh` |
| `eng6_workers_busy` / `_total` | gauge | утилизация > 0,95 (10 мин) → `Eng6WorkersSaturated` |
| `eng6_worker_restarts_total` | counter | > 5 за 10 мин → `Eng6WorkerRestarts` (OOM/таймауты) |
| `eng6_plan_total{status="ERROR"}` | counter | > 0,02/с → `Eng6ComputeErrors` |
| `eng6_memory_rejected_total` | counter | рост → увеличить RAM или снизить `ENG6_MAX_BODY_BYTES` |
| `eng6_process_memory_rss_bytes` | gauge | > 5 ГБ → `Eng6MemoryHigh` |
| `eng6_validation_failed_total` | counter | > 1/с (5 мин) → `Eng6ValidationFlood` (сбой интеграции/фаззинг) |
| `eng6_plan_graph_nodes_bucket` | histogram | контроль распределения размеров графов |

Записываемые правила (`deploy/prometheus/rules/eng6-plan.yml`):
`eng6:plan_duration_ms:p95/p99`, `eng6:plan_rate_rps`, `eng6:http_error_rate`,
`eng6:worker_utilization`.

### 6.2 Дашборд Grafana

`deploy/grafana/dashboards/eng6-plan.json` — 22 панели в 4 группах:
состояние сервиса (up, расчётов/с, очередь, воркеры, RSS, 429/с);
производительность ядра (квантили времени, очередь и ожидание, размеры графов,
статусы расчётов, коды HTTP); ресурсы и надёжность (память, перезапуски, ошибки,
размеры запросов/результатов); сборка ядра (`eng6_build_info` + sha256 через `/api/v1/meta`).

### 6.3 Аудит и расследование

```bash
# последние 50 записей
curl -s -H "Authorization: Bearer $KEY" localhost:8080/api/v1/audit?limit=50 | jq

# найти все запуски конкретного заказчика
curl -s -H "Authorization: Bearer $KEY" \
  "localhost:8080/api/v1/audit?actor=%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2" | jq '.items[] | {ts,status,computeMs}'

# воспроизвести расчёт по хэшу входа (тела в журнале нет — только SHA-256)
jq 'select(.inputSha256=="9f2c…")' var/audit.ndjson
```

Журнал — append-only NDJSON: пригоден для `filebeat`/`vector`/`fluentbit`,
ротация — средствами ОС или sidecar. При отказе диска расчёты не
останавливаются, растёт счётчик `writeErrors` (виден в `GET /api/v1/audit → stats`).

## 7. Регламентные операции

| Операция | Периодичность | Команда / действие |
|---|---|---|
| Конформанс ядра ENG6 | при каждой сборке + ежесуточно | `GET /api/v1/conformance?refresh=1` → `ok: true` |
| Проверка sha256 вендора | при каждом изменении `core/vendor/` | `sha256sum core/vendor/eng6_core.js` = `VENDOR_SHA256.txt` |
| Ротация API-ключей | 90 суток | обновить `ENG6_API_KEYS`, перезапустить (rolling) |
| Ротация журнала аудита | по размеру (1 ГБ) | `logrotate` + `copytruncate` |
| Проверка readiness | постоянно | `GET /readyz` (K8s-проба настроена) |
| Нагрузочный прогон | ежемесячно / перед релизом | `npm run bench:full`, сверка с `docs/04_TEST_REPORT.md` |
| Обновление дашборда | при изменении метрик | `deploy/grafana/dashboards/eng6-plan.json` |

## 8. Штатные нештатные ситуации

| Симптом | Вероятная причина | Действие |
|---|---|---|
| Все запросы `429 SYNC_BUSY` | тяжёлые графы в sync-режиме при малом числе слотов | перевести интеграцию на `/plan/async`; увеличить `ENG6_MAX_SYNC_CONCURRENCY` и CPU |
| `429 QUEUE_FULL` | поток задач выше производительности пула | масштабировать реплики (HPA по `eng6_queue_length`), снизить `ENG6_RL_REQUESTS` для проблемного клиента |
| `503 MEMORY_BACKPRESSURE` | много крупных пакетов одновременно | поднять RAM/лимит, уменьшить `ENG6_MAX_BODY_BYTES`, дробить графы |
| `504 TASK_TIMEOUT` | граф крупнее расчётного, мал `ENG6_TASK_TIMEOUT_MS` | увеличить таймаут, задать `constraints.timeLimitMs`, снизить число заявок |
| Рост `eng6_worker_restarts_total{reason="timeout"}` | задачи систематически не успевают | см. выше; проверить `timings.routingMs` в ответе |
| `status: ERROR` + `CORE_EXCEPTION` | дефект данных или ядра | включить `ENG6_STRICT=1`, найти запись в аудите по `requestId`, воспроизвести по `inputSha256` |
| `eng6Analysis.verdict: REJECTED_BY_ENG6` | план построен в `physics: fast` на пересечённом рельефе | перевести задачу на `physics: full` либо `terrain.source: flat` |
| UI: «ядро: нет связи» | сервис не запущен / другой адрес | проверить поле «Ядро» в UI, `GET /healthz`; UI автоматически переходит на встроенный демо-результат |

## 9. Резервное копирование и восстановление

* **Журнал аудита** (`/app/var/audit.ndjson` или PVC `eng6-plan-audit`) — единственные
  данные, требующие хранения; бэкап ежесуточно, хранение ≥ 1 года (требование аудита).
* **Результаты задач** не персистентны by design: TTL 1 ч, LRU ≤ 256 записей.
  Для долгосрочного хранения заказчик выгружает результат и кладёт в свою БД
  (FR-INT-01: Frontend и сервис — «витрина», источник истины — БД заказчика).
* **Конфигурация** — в `ConfigMap`/`Secret`; восстановление = повторное применение манифестов.
* **Образ** — в приватном реестре с тегом по версии и sha256 вендора в аннотации.

## 10. Обновление версии

1. Обновить `core/vendor/eng6_core.js` → пересчитать `VENDOR_SHA256.txt`.
2. `npm run test:conformance` — **обязательно** 33/33 selfTest и 5/5 регрессия.
3. `npm test` — все 125 тестов.
4. `npm run bench` → сверка с `docs/04_TEST_REPORT.md` (регрессия производительности > 20 % = блокирующий дефект).
5. `node scripts/build_frontend.mjs` — пересборка UI.
6. Сборка образа (тесты выполняются внутри), прогон в staging, canary 10 % → 100 %.
