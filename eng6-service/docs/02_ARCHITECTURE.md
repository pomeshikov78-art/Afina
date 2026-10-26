# Архитектура сервиса ENG6-PLAN

## 1. Слои и границы

```
┌────────────────────────────────────────────────────────────────────────────┐
│ КЛИЕНТЫ                                                                    │
│  • frontend/index.html — картографический планировщик (Дополнение к ТЗ №1) │
│  • ERP/CRM/система диспетчеризации — коннектор по REST (Путь А)            │
│  • k6 / Locust / нагрузочный стенд                                         │
└───────────────▲────────────────────────────────────────────────────────────┘
                │ HTTPS (TLS, NFR-02) · JSON · X-Requestor · X-Api-Key
┌───────────────┴────────────────────────────────────────────────────────────┐
│ ЭТАП 3 · API-LAYER                                     api/                │
│  server.mjs   маршрутизация, CORS/CSP, security-заголовки                   │
│  http.mjs     чтение тела с лимитом, предпроверка, идентификаторы          │
│  config.mjs   12-factor конфигурация + fail-fast валидация                 │
│  auth         Bearer/X-Api-Key, обязательный X-Requestor                    │
│  ratelimit    token bucket (429) + Semaphore для /plan/sync (429)          │
│  pool         пул worker_threads, ограниченная очередь (429), таймауты     │
│  tasks        хранилище состояний задач (QUEUED→RUNNING→SUCCEEDED/…)       │
│  audit        NDJSON-журнал + кольцевой буфер (требование Военпреда)        │
│  metrics      Prometheus text exposition (0 внешних зависимостей)           │
└───────────────▲────────────────────────────────────────────────────────────┘
                │ postMessage(сырой JSON)  ·  без БД, без сети
┌───────────────┴────────────────────────────────────────────────────────────┐
│ ЭТАП 2 · ВЫЧИСЛИТЕЛЬНОЕ ЯДРО (чистая функция)          core/               │
│                                                                            │
│  planner.mjs  plan(request) → response        ← единственный вход/выход    │
│   ├ schema.mjs    контракт, лимиты, веса, целевая функция                  │
│   ├ validate.mjs  строгая валидация + канонический SHA-256                 │
│   ├ scenario.mjs  нормализация среды, сборка сценария ENG6                 │
│   ├ physics.mjs   физический слой: режимы fast / full (ENG6.calcLink)      │
│   ├ graph/        CSR, куча, Дейкстра, RCSP, Tarjan, Крускал, Штейнер,     │
│   │               Диниц, min-cut, непересекающиеся пути                    │
│   ├ route.mjs     многопродуктовая маршрутизация (остаточная ёмкость)      │
│   ├ knapsack.mjs  0/1-рюкзак (точное ДП / жадность + swap)                 │
│   ├ schedule.mjs  списковое расписание (Graham), волны, критический путь   │
│   └ eng6.mjs      загрузчик вендора в изолированном V8-контексте           │
│        └ vendor/eng6_core.js  — ENG6 v5.1.0, sha256 adb610c9…, 677 168 зн. │
└────────────────────────────────────────────────────────────────────────────┘
```

**Правило зависимостей:** стрелки идут только сверху вниз. Ядро не знает ни о
HTTP, ни о воркерах, ни о конфигурации сервиса; API не знает математики.
Единственная внешняя сущность ядра — вендор ENG6 (математика, не сервис).

## 2. Ключевые решения (ADR)

### ADR-001 · Вендор ENG6 как единственный источник истины
**Контекст.** В изделии `comms_planner_v8.0_headless.html` обнаружено полное
нативное ядро ENG6 (69 ключей, 151 модель `MLIB`, встроенные `selfTest` 33/33
и `runRegression` 5/5, конформанс 326 050 проверок с оригиналом v5.1).
**Решение.** Ядро извлечено побайтово (`core/vendor/eng6_core.js`), исполняется
в изолированном V8-контексте и используется без правок; sha256 зафиксирован в
`core/vendor/VENDOR_SHA256.txt` и проверяется тестом.
**Альтернативы.** Переписать на Go/Rust → два источника истины, риск расхождения
моделей, необходимость повторной валидации 326 050 проверок.
**Последствия.** + ноль дрейфа математики, + перекрёстная проверка плана тем же
ядром, − привязка к Node.js, − 677 КБ вендора в образе.

### ADR-002 · CSR + типизированные массивы вместо объектного графа
**Решение.** Единственное представление графа — `CsrGraph` (offset/to/edgeId/w/cap/twin).
**Обоснование.** На 10⁵ узлов / 3,5·10⁵ рёбер объектный граф дал бы ≈ 7·10⁵
мелких объектов (деградация GC), тогда как CSR — 24·m + 4·n байт непрерывной
памяти. Замер: 10⁵ узлов — RSS 434 МБ, Дейкстра с ранним выходом 2 мс,
Tarjan 72 мс. Массив `twin[]` обязателен: без него Tarjan на мультиграфе
ошибочно считает параллельные рёбра мостами.
**Расширение.** При необходимости кратного ускорения графовый подслой выносится
в Rust/N-API аддон без изменения контракта `plan()` (граница — `core/graph/index.mjs`).

### ADR-003 · Tiered fidelity: `fast` зеркалит ветвь `balanced`, а не `fast` оригинала
**Контекст.** `ENG6.calcLink` в режиме `quality='fast'` считает УКВ-пролёт чистым
Hata (147 дБ на 10 км / 150 МГц), а в `balanced` — `logDistance(n_clutter)`
(108 дБ) либо `min(Hata_open, two-ray)` в дальней зоне. Расхождение до 40 дБ.
**Решение.** Режим `fast` планировщика повторяет **`balanced`**-ветвь без профиля
рельефа, добавляя `fastMarginPenaltyDb` (6 дБ) как оценку дифракции.
**Проверено.** На плоском рельефе расхождение `fast`/`full` < 0,01 дБ по запасу,
ёмкости и задержке; на синтетическом рельефе (120 м) `fast` оптимистичнее — это
задокументированная область применимости, контролируемая перекрёстной проверкой
ENG6 (вердикты `CONSISTENT` / `DEGRADED` / `REJECTED_BY_ENG6`).

### ADR-004 · Backpressure в три рубежа
1. **rate limit** на клиента (token bucket, тяжёлые графы тарифицируются с весом) → `429 + Retry-After`;
2. **семафор** одновременных синхронных расчётов → `429 SYNC_BUSY` + подсказка `suggest: async`;
3. **очередь** ограниченной длины → `429 QUEUE_FULL`;
4. **память «в полёте»** (`maxInflightBytes`, по `Content-Length` до чтения тела) → `503 MEMORY_BACKPRESSURE`.
Пункт 4 добавлен после того, как нагрузочный стенд убил сервис 100 запросами
по 2,3 МБ (DEF-04): очередь при этом не была переполнена.

### ADR-005 · Ответ 202 до начала расчёта
`POST /plan/async` отправляет `202 + Location` **до** постановки задачи в пул,
но постановка выполняется в том же обработчике сразу после ответа. Обнаруженная
ошибка порядка (`ERR_HTTP_HEADERS_SENT` прерывал постановку) устранена — DEF-06;
жизненный цикл задачи покрыт тестом.

### ADR-006 · In-memory хранилище задач с интерфейсом замены
`TaskStore` хранит метаданные + результат (TTL 1 ч, LRU ≤ 256 записей),
**не хранит тело запроса** (только SHA-256). Замена на Redis/PostgreSQL —
реализация того же интерфейса (`create/get/update/setResult/list/cancel/evict`).

### ADR-007 · Передача в воркер сырой строкой
`structured clone` глубокого объекта на 10⁵ узлов стоил сотен миллисекунд и
удваивал пиковую память. Решение: в воркер уходит JSON-строка, `JSON.parse`
выполняется в воркере; результат возвращается строкой так же.

### ADR-008 · UI без внешних зависимостей
Single-file HTML + Canvas2D + синтетическая геоподложка (детерминированный
value-noise, кэш слоя рельефа, рендер с шагом 4 px). Причины: NFR-02 (запрет
внешних ресурсов и кэширования координат), работа в изолированном контуре,
предпросмотр без сети. Cytoscape.js/D3.js не используются осознанно;
WebGL не потребовался: счётчик FPS в UI подтверждает ≥ 30 FPS при 1000 знаков.

## 3. Модель данных (укрупнённо)

```
request
├ meta            {name, author, requestId, seed}
├ environment     {clutterType, weather, ionosphere, terrain(synth|flat), miscLossDb}
├ topology
│  ├ nodes[]      {id, type, name, lat(6), lng(6), status, parentId, unitId,
│  │               txPowerDbm, txGainDbi, rxGainDbi, rxSensDbm, antHeightM,
│  │               freqMhz, feederLossDb, noiseFigureDb, mobility,
│  │               deployTimeMin, costDeploy, children[](узел связи)}
│  └ links[]      {id, from, to, type(VHF|UHF|HF|RRL|SAT|FOC), freqMhz, bwKhz,
│                  mod, fec, priority, isBackup, cost}
├ fleet[]         {unitId, kind, nodeType, condition(green|yellow|red),
│                  costDeploy, deployTimeMin, powerW, note}
├ demands[]       {id, from, to, rateKbps, maxDelayMs, maxHops, priority(1=высший),
│                  minAvailabilityPct, protected}
├ constraints     {budget, minMarginDb, maxDelayMs, maxHops, reserveFactor,
│                  maxSimultaneousDeploy, crewSpeedKmh, requireRedundancy,
│                  allowYellowUnits, allowRedUnits, physics, quality, timeLimitMs,
│                  frameKbit, hopProcessingMs, maxUtilizationPct, backbone,
│                  upgradeFactor, upgradeCostRatio, crossCheck*}
└ weights         {cost, delay, survivability, margin}
```

Соответствие БД Дополнения к ТЗ (FR-INT-01): `map_object` ↔ `objects/nodes`,
`node_group` ↔ узел связи (`children[]`), `link_line` ↔ `links`,
`CommAsset` ↔ `fleet[unitId]`, `geo_position` ↔ `lat/lng` (6 знаков).

## 4. Потоки выполнения

**Синхронный расчёт.** `POST /plan/sync` → auth → rate limit → лимит тела →
предпроверка размеров → семафор → задача в пул → воркер: `plan()` → ответ 200
(или 422/500 по статусу ядра) → аудит → метрики.

**Асинхронный расчёт.** `POST /plan/async` → те же рубежи → `202 + Location` →
постановка в очередь → воркер → `TaskStore.setResult` → клиент опрашивает
`GET /tasks/{id}` (поддержка `X-Result-Format: summary`).

**Изменение топологии в UI (FR-INT-02).** drag&drop/перемещение/линия →
дебаунс 900 мс → `buildRequest()` → `POST /plan/sync` → `applyPlanToState()` →
перерисовка оверлеев (зоны покрытия, цвет линий по запасу, маршруты, резервные пути).

## 5. Отказоустойчивость

| Отказ | Реакция |
|---|---|
| Превышен `timeLimitMs` в ядре | `status: TIMEOUT` + частичный план |
| Исключение в математике | `status: ERROR` + `CORE_EXCEPTION`, аудит, другие задачи не затронуты |
| Завис/превысил таймаут воркер | `worker.terminate()` + пересоздание, задача → `TIMEOUT`, метрика `eng6_worker_restarts_total{reason="timeout"}` |
| OOM воркера | `WORKER_EXIT`, задача → `FAILED`, воркер пересоздаётся, сервис жив |
| Переполнение очереди | `429 QUEUE_FULL` + `Retry-After` |
| Переполнение памяти | `503 MEMORY_BACKPRESSURE` + `Retry-After` |
| Падение диска аудита | счётчик `writeErrors`, расчёты не останавливаются |
| Недоступность API для UI | автоматический переход на встроенный демо-результат, тост с инструкцией |

## 5.1. Интеграция с учётной системой (Путь А)

`integrations/erp_connector.mjs` — эталонный коннектор «ERP/CRM/АСУ → ядро →
система диспетчеризации» без участия человека и без картографического интерфейса.

```
ERP/CRM/АСУ ──(JSON | CSV | {sites, assets, circuits, orders})──▶ АДАПТЕР ИСТОЧНИКА
                                                                     │  normalizeRequest()
                                                                     ▼
                                                          пакет ENG6-PLAN (WGS-84, 6 знаков)
                                                                     │  REST + повторы
                                                                     │  429/503 → Retry-After
                                                                     ▼
                                                       POST /plan/sync  |  /plan/async + опрос
                                                                     │
                                                          ответ ядра (план, метрики,
                                                          послабления, перекрёстная проверка)
                                                                     │  toDispatchPayload()
                                                                     ▼
СИСТЕМА ДИСПЕТЧЕРИЗАЦИИ ◀── плоские таблицы: map_object, link_line, link_rejected,
                            route, deployment_wave, schedule_summary, survivability,
                            actions_required, issues, recommendations, metrics
```

Особенности:
* три формата источника: готовый запрос ENG6-PLAN, «сырая» модель ERP
  (`sites/assets/circuits/orders` с маппингом типов и состояний) и CSV
  (секции `[nodes]/[links]`, разделители `, ; TAB`, русские заголовки);
* режим `auto`: мелкие задачи — `sync`, крупные (порог `--autoNodes`) — `async`
  с опросом состояния;
* повторы с экспоненциальной паузой и учётом `Retry-After` на 429/503;
* коды возврата: `0` — план построен, `2` — `INFEASIBLE`/`TIMEOUT`, `3` — сбой интеграции;
* если источник не передал рельеф, используется **плоская** модель с
  предупреждением: синтетический рельеф создал бы вымышленные возвышенности и
  фиктивные дифракционные потери до 40 дБ (см. DEF-12);
* ключ `--dump-request=<файл>` выгружает собранный пакет для контроля интеграции.

## 6. Безопасность

* TLS обязателен в `production` (fail-fast при старте); mTLS — `ENG6_TLS_REQUIRE_CLIENT_CERT`;
* CSP `default-src 'none'`, `X-Frame-Options: DENY`, `no-store`, `nosniff`, `no-referrer`;
* API-ключи из окружения/secret-хранилища; обязательный `X-Requestor` (percent-encoded UTF-8);
* аудит: SHA-256 канонического JSON входа, **без** тел запросов (координаты — охраняемые данные);
* журнал аудита в production недоступен без включённой аутентификации;
* ядро: V8-контекст с tripwire на 12 браузерных/сетевых API, `indexedDB = undefined`,
  запрет `eval`/`Function` через `codeGeneration`, отсутствие импортов сети/БД (проверяется тестом);
* k8s: `runAsNonRoot`, `readOnlyRootFilesystem`, `capabilities: drop ALL`,
  `seccompProfile: RuntimeDefault`, NetworkPolicy с egress только на DNS.
