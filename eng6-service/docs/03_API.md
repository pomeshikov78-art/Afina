# API сервиса ENG6-PLAN (ЭТАП 3)

Базовый URL: `https://<host>:8080/api/v1` · Формат: JSON (UTF-8) · Аутентификация: `Authorization: Bearer <key>` или `X-Api-Key: <key>`.

## 1. Общие заголовки

| Заголовок | Направление | Обязательность | Назначение |
|---|---|---|---|
| `X-Requestor` | запрос | **обязателен** при `ENG6_REQUIRE_REQUESTOR=true` | кто запросил расчёт (аудит). Кириллица — в percent-encoding: `X-Requestor: %D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2` |
| `Authorization: Bearer <key>` / `X-Api-Key` | запрос | при `ENG6_AUTH_ENABLED=true` | аутентификация |
| `X-Request-Id` | запрос/ответ | опционально | сквозная трассировка (сервер генерирует UUID при отсутствии) |
| `X-Result-Format` | запрос | опционально | `full` (по умолчанию) / `summary` / `none` — объём результата |
| `X-Compute-Ms` | ответ | всегда | время расчёта в ядре |
| `X-Queue-Wait-Ms` | ответ | всегда | время ожидания слота/воркера |
| `X-ENG6-Core-Sha256` | ответ | всегда | контрольная сумма вендорного ядра (первые 16 знаков) |
| `Retry-After` | ответ 429/503 | при отказе | рекомендованная пауза, сек |
| `Location` | ответ 202 | всегда | URL статуса задачи |

Заголовки безопасности на всех ответах: `Cache-Control: no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
CSP `default-src 'none'; connect-src 'self'; …` (NFR-02).

## 2. Эндпоинты

### 2.1 `POST /api/v1/plan/sync` — синхронный расчёт

Для быстрых задач (до ~5 000 узлов при `physics: fast`). Ответ приходит одним циклом.

**Коды:** `200` — расчёт выполнен (статус плана в теле), `400` — не JSON / нет `X-Requestor`,
`401` — нет ключа, `413` — тело больше `ENG6_MAX_BODY_BYTES`, `422` — вход не прошёл
валидацию (**тело содержит полный отчёт** об ошибках), `429` — rate limit или заняты все
sync-слоты (`SYNC_BUSY`, в теле `suggest: POST /api/v1/plan/async`), `500` — сбой ядра
(`status: ERROR`), `503` — backpressure по памяти, `504` — превышен таймаут задачи.

```jsonc
// 200
{
  "status": "PARTIAL",                    // OPTIMAL|FEASIBLE|PARTIAL|INFEASIBLE|INVALID|TIMEOUT|ERROR
  "statusReasons": [
    { "code": "DEMANDS_REJECTED", "message": "Отклонено заявок: 1 из 5", "count": 1 },
    { "code": "SPOF_PRESENT", "message": "Единых точек отказа: 3 узл., 5 лин." }
  ],
  "plan": {
    "deployedUnits": [ { "nodeId": "cp-bat", "nodeType": "cp", "unitId": "PU-001",
                         "condition": "green", "lat": 55.75, "lng": 37.62,
                         "cost": 30, "deployMin": 45, "wave": 1, "startMin": 0 } ],
    "activeLinks":  [ { "id": "l2", "from": "cp-bat", "to": "co-a", "type": "VHF",
                        "distKm": 9.9932, "marginDb": 27.05, "sensMarginDb": 38.2,
                        "capacityKbps": 32.69, "loadKbps": 16, "utilPct": 48.9,
                        "delayMs": 4.033, "availabilityPct": 99.658, "cost": 0,
                        "role": "primary",
                        "physics": { "plDb": 123.95, "rxDbm": -72.95, "snrDb": 49.07,
                                     "sensMarginDb": 38.2, "theoreticalKbps": 407.5,
                                     "los": true, "type": "VHF" } } ],
    "routes": [ { "demandId": "d1", "from": "cp-bat", "to": "co-a", "requestedKbps": 16,
                  "grantedKbps": 16, "deficitKbps": 0, "status": "ROUTED", "reason": "OK",
                  "delayMs": 68.533, "maxDelayMs": 120, "hops": 1, "marginDb": 27.05,
                  "availabilityPct": 99.658, "utilMaxPct": 48.9, "redundant": false,
                  "path": ["cp-bat","co-a"], "links": ["l2"], "backupPath": null } ],
    "schedule": { "waves": [ { "index": 1, "tStartMin": 0, "tEndMin": 45, "durationMin": 45,
                               "crewsUsed": 1, "items": [ { "id": "cp-bat", "nodeId": "cp-bat",
                               "unitId": "PU-001", "kind": "П-240И", "crew": 0,
                               "startMin": 0, "endMin": 45 } ] } ],
                  "makespanMin": 120.76, "crews": 4, "crewUtilPct": 24.2,
                  "criticalPath": [ { "id": "cp-bat", "startMin": 0, "endMin": 45 } ],
                  "referencePoint": { "lat": 55.75, "lng": 37.62 },
                  "violations": [], "jobs": [ /* … */ ], "jobsTotal": 6 },
    "rejectedLinks": [ { "id": "l8", "from": "co-a", "to": "obs-2", "type": "UHF",
                         "code": "BELOW_MARGIN", "reason": "запас по уровню ниже минимально допустимого",
                         "distKm": 14.2, "marginDb": -2.65, "capKbps": 0, "delayMs": 4.06 } ],
    "rejectedLinksHistogram": { "BELOW_MARGIN": 1 },
    "survivability": { "componentsCount": 1, "candidateComponentsCount": 1,
                       "connectivityPct": 100, "largestComponentPct": 100,
                       "spofNodes": ["cp-bat","r1","co-b"], "spofCount": 3,
                       "bridgeLinks": ["l2","l3","l4","l5","l7"], "bridgeCount": 5,
                       "minCutKbps": null, "minCutPair": null,
                       "computed": ["components","bridges","articulations"] },
    "truncation": { "links": null, "routes": null, "nodes": null },
    "redundancyAdded": 0
  },
  "metrics": {
    "objectiveValue": 0.21621, "grade": "B",
    "nodesTotal": 7, "nodesDeployed": 6,
    "linksCandidate": 8, "linksFeasible": 8, "linksActive": 5,
    "totalCost": 76, "budget": 400, "budgetUsedPct": 19,
    "demandsTotal": 5, "demandsSatisfied": 4, "demandsPartial": 0, "demandsRejected": 1,
    "demandServedPct": 94.737, "throughputKbps": 72,
    "capacityTotalKbps": 212.5, "loadTotalKbps": 88, "utilMaxPct": 81.57, "utilAvgPct": 41.41,
    "avgDelayMs": 79.367, "maxDelayMs": 143.107, "delayViolations": 0, "minAvailabilityPct": 95.053,
    "connectivityPct": 100, "componentsCount": 1, "candidateComponentsCount": 1,
    "spofNodes": 3, "bridgeLinks": 5, "minCutKbps": null,
    "avgMarginDb": 24.01, "minMarginDb": 17.81, "linksBelow10Db": 0,
    "marginHistogram": { "<0": 0, "0-3": 0, "3-10": 0, "10-20": 1, ">20": 4 },
    "redundancyAdded": 0, "scheduleMakespanMin": 120.76, "scheduleWaves": 3, "crewUtilPct": 24.2
  },
  "diagnostics": {
    "validation": { "ok": true, "errors": [], "warnings": [], "truncated": false },
    "issues": [ { "sev": "crit", "code": "UNIT_SHORTAGE", "obj": { "kind": "net" },
                  "title": "Нехватка техники: observer",
                  "text": "1 узл(ов) не могут быть развернуты — в парке нет готовых единиц типа «observer»" } ],
    "recommendations": [ { "id": "REC-SPOF", "priority": 1, "target": { "kind": "net" },
                          "title": "Устранить единые точки отказа", "text": "…" } ],
    "infeasibility": {
      "causes": [ { "code": "UNIT_SHORTAGE", "message": "…", "shortage": { "observer": 1 } } ],
      "disconnectedComponents": [],
      "minimalRelaxations": [
        { "code": "ADD_RELAY", "at": { "lat": 55.7005, "lng": 37.5961 }, "count": 1,
          "wouldConnect": [0, 1], "spanKm": 11.388, "requiredEirpDbm": -4.9,
          "nearestPair": ["cp-bat","co-c"], "reason": "…" },
        { "code": "RAISE_BUDGET", "value": 412.5, "reason": "резервирование заявки d2" },
        { "code": "ADD_CAPACITY", "linkId": "l3", "value": 7.31, "reason": "…" },
        { "code": "RELAX_MAX_DELAY_MS", "value": 312.4, "reason": "…" }
      ],
      "partialSolution": { "demandsServed": 4, "demandsTotal": 5, "throughputKbps": 72,
                           "cost": 76, "linksActivated": 5, "nodesDeployed": 6, "note": "…" }
    },
    "rejectedLinksHistogram": { "BELOW_MARGIN": 1 },
    "approximation": { "physics": "fast", "steiner": "metric_closure_mst",
                       "routing": "successive_shortest_paths_greedy",
                       "knapsack": "exact_dp", "schedule": "list_scheduling_graham" },
    "eng6Analysis": { "skipped": false, "quality": "balanced", "verdict": "DEGRADED",
                      "comparison": { "activeLinks": { "planner": 5, "eng6": 3, "delta": 2 },
                                      "grade": "C", "overall": 64.9 },
                      "ratings": { "connectivity": 100, "throughput": 100, "survivability": 10,
                                   "jamming": 80, "recon": 94.5, "redundancy": 0,
                                   "overall": 64.9, "grade": "C" },
                      "network": { "connectivityPct": 100, "componentsCount": 1, "activeLinks": 3,
                                   "avgPathLen": 1.667, "diameter": 3, "practicalTotalKbps": 1000085,
                                   "minCutKbps": 42.5, "spof": 2, "bridges": 3, "overloaded": 2 },
                      "issues": [ /* до 40 */ ], "recommendations": [ /* до 20 */ ],
                      "assumptions": [ /* модельные допущения ENG6 */ ], "models": [ /* цепочки моделей */ ] },
    "lowerBoundCost": 42.5
  },
  "timings": { "validateMs": 1.9, "fleetMs": 11.4, "physicsMs": 2.5, "graphMs": 0.5,
               "backboneMs": 0.4, "routingMs": 1.8, "improveMs": 15.7, "redundancyMs": 0.01,
               "survivabilityMs": 0.7, "scheduleMs": 1.2, "metricsMs": 1.7, "eng6Ms": 54.8,
               "totalMs": 133.4 },
  "core": { "engine": "ENG6", "engineVersion": "5.1.0",
            "engineSha256": "adb610c9eee1ca17b257cbc6087e2278fdde3eef92f3b6a360cfb51968d48b20",
            "planner": "1.0.0", "physics": "fast" },
  "requestId": "…", "taskId": "sync_…"
}
```

### 2.2 `POST /api/v1/plan/async` — постановка тяжёлой задачи

Тот же контракт тела. Ответ **до** завершения расчёта:

```jsonc
// 202 Accepted, Location: /api/v1/tasks/plan_mva3q0eq_2q2f_3
{ "taskId": "plan_mva3q0eq_2q2f_3", "state": "QUEUED",
  "statusUrl": "/api/v1/tasks/plan_mva3q0eq_2q2f_3",
  "cancelUrl": "/api/v1/tasks/plan_mva3q0eq_2q2f_3",
  "position": 3, "estimatedWaitMs": 1500,
  "inputSha256": "9f2c…", "sizes": { "nodes": 100000, "links": 352889, "demands": 2000 },
  "requestId": "…" }
```

**Коды:** `202` — принято; `429 QUEUE_FULL` (тело: `queueLength`, `maxQueue`, `busy`, `workers`);
`429 RATE_LIMITED`; `503 MEMORY_BACKPRESSURE`; `4xx` как у sync.

### 2.3 `GET /api/v1/tasks/{id}` — статус и результат

```jsonc
{ "taskId": "plan_…", "state": "SUCCEEDED",      // QUEUED|RUNNING|SUCCEEDED|FAILED|TIMEOUT|CANCELLED
  "status": "PARTIAL",                            // статус плана (при SUCCEEDED)
  "actor": "Иванов И.И.", "requestId": "…", "mode": "async",
  "inputSha256": "9f2c…", "inputBytes": 5806081,
  "sizes": { "nodes": 100000, "links": 352889, "demands": 2000 },
  "createdAt": "2026-10-16T09:12:03.412Z", "startedAt": "…", "finishedAt": "…",
  "queueWaitMs": 11.3, "computeMs": 870.7, "durationMs": 882.0, "workerId": 1,
  "resultBytes": 215953, "progress": { "phase": "queued", "percent": 0 },
  "error": null, "result": { /* полный ответ ядра */ },
  "position": 4, "estimatedWaitMs": 2000 }        // только для QUEUED/RUNNING
```

`X-Result-Format: summary` возвращает `result` в сокращении (первые 50 маршрутов/линий,
метрики целиком, диагностика усечена) — экономия трафика на крупных графах.
`404` — задача не найдена или результат удалён по TTL (`ENG6_RESULT_TTL_MS`).

### 2.4 `GET /api/v1/tasks?state=&actor=&limit=` · `DELETE /api/v1/tasks/{id}`

Список задач (без результатов) + счётчики по состояниям. `DELETE` отменяет задачу
в состоянии `QUEUED` (`409` для `RUNNING`).

### 2.5 `GET /api/v1/meta`

Контракт сервиса: версия, ядро ENG6 (`version`, `sha256`, 69 ключей), лимиты ядра и API,
состояние пула, параметры rate limit, список эндпоинтов с кодами ответов, требуемые заголовки.

### 2.6 `GET /api/v1/conformance?refresh=1`

Протокол конформанса вендора: `selfTestTotal: 33`, `selfTestFailed: []`,
`regressionTotal: 5`, `regressionFailed: []`, `ok: true`, `coreSha256`.
Кэшируется на 1 ч; `refresh=1` — принудительный пересчёт (приёмочный контроль).

### 2.7 `GET /api/v1/audit?limit=&requestId=&actor=&status=`

Журнал аудита (требование Военпреда). В production доступен только при включённой
аутентификации (`403` иначе). Запись содержит:

```jsonc
{ "ts": "2026-10-16T09:12:03.412Z", "event": "plan_finished", "mode": "async",
  "taskId": "plan_…", "actor": "Иванов И.И.", "clientId": "key:9f2c…", "requestId": "…",
  "inputSha256": "9f2c…", "inputBytes": 5806081,
  "sizes": { "nodes": 100000, "links": 352889, "demands": 2000 },
  "status": "PARTIAL", "computeMs": 870.7, "queueWaitMs": 11.3, "workerId": 1,
  "resultBytes": 215953, "service": "eng6-plan", "version": "1.0.0",
  "env": "production", "pid": 42 }
```

События: `plan_queued`, `plan_started`, `plan_finished`, `plan_failed`, `plan_rejected`,
`rate_limited`, `queue_full`, `memory_backpressure`, `sync_overloaded`, `task_cancelled`,
`internal_error`. **Тела запросов не логируются** (только SHA-256) — координаты
являются охраняемыми данными; полное логирование включается только
`ENG6_AUDIT_LOG_PAYLOAD=true` для отладки.

### 2.8 `GET /metrics` · `GET /healthz` · `GET /readyz` · `GET /`

Prometheus text exposition 0.0.4; liveness (состояние пула и память);
readiness (`503`, если воркеры не готовы или очередь переполнена); UI.

## 3. Обработка ошибок и backpressure

| Код | `error.code` | Когда | Действие клиента |
|---|---|---|---|
| 400 | `BODY_INVALID_JSON`, `EMPTY_BODY`, `REQUESTOR_REQUIRED` | мусор на входе | исправить запрос |
| 401 | `UNAUTHORIZED` | нет/неверный ключ | получить ключ |
| 403 | `ADMIN_FORBIDDEN`, `FORBIDDEN` | аудит без auth; выход за каталог статики | — |
| 404 | `NOT_FOUND`, `TASK_NOT_FOUND` | неизвестный маршрут/задача | — |
| 409 | `TASK_NOT_CANCELLABLE` | отмена выполняемой задачи | дождаться завершения |
| 413 | `PAYLOAD_TOO_LARGE` | тело > `ENG6_MAX_BODY_BYTES` | уменьшить граф |
| 422 | `LIMIT_NODES`/`LIMIT_LINKS`/`LIMIT_DEMANDS`/`BODY_NOT_OBJECT`/`TOPOLOGY_MISSING` или `status: INVALID` | превышены лимиты или вход некорректен | читать `statusReasons`/`diagnostics.validation` |
| 429 | `RATE_LIMITED`, `SYNC_BUSY`, `QUEUE_FULL` | перегрузка | **повторить по `Retry-After`**; для `SYNC_BUSY` — уйти в async |
| 500 | `INTERNAL_ERROR`, `COMPUTE_FAILED`, `WORKER_EXCEPTION`, `WORKER_OOM` | сбой | повторить; при повторе — в поддержку с `requestId` |
| 503 | `MEMORY_BACKPRESSURE`, `POOL_CLOSED` | нет памяти / остановка | повторить позже |
| 504 | `TASK_TIMEOUT` | превышен `ENG6_TASK_TIMEOUT_MS` | уменьшить граф или увеличить лимит |

**Ни один сценарий нагрузки не должен давать 5xx из-за поведения клиента** — это
проверяется фазой C нагрузочного стенда и порогом k6 `eng6_server_errors: rate==0`.

## 4. Формат входных данных

Полная спецификация — `docs/01_MATH_MODEL.md`, п. 2; исполняемая схема —
`core/schema.mjs` (лимиты, дефолты, перечисления) и `core/validate.mjs` (правила).
Открытая схема OpenAPI 3.1 — `docs/openapi.yaml`.

Импорт из CSV поддерживается UI (`parseCsv`/`csvToScenario`): секции `[nodes]`/`[links]`
или плоский список, разделители `, ; TAB`, кавычки с удвоением, BOM, русские заголовки
(`широта`, `долгота`, `тип`, `от`, `до`, `подчинение`, `инвентарный номер`).

## 5. Интеграция с внешними системами (Путь А)

Коннектор ERP/CRM → `POST /api/v1/plan/async` → опрос `GET /api/v1/tasks/{id}` →
выгрузка `plan.deployedUnits` / `plan.activeLinks` / `plan.schedule` в систему
диспетчеризации. Идемпотентность контролируется по `inputSha256`: одинаковый вход
даёт одинаковый результат (NFR-DET-01), поэтому повторная отправка безопасна,
а в аудите видна дедупликация.
