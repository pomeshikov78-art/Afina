/**
 * api/config.mjs — конфигурация сервиса (только переменные окружения).
 *
 * Принцип 12-factor: никаких конфигов в коде, все пороги задаются извне.
 * Каждый параметр валидируется при старте — сервис не поднимается с
 * бессмысленной конфигурацией (fail fast, требование «Военпреда»).
 */
const int = (name, def, min, max) => {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return def;
  const v = Number(raw);
  if (!Number.isFinite(v) || !Number.isInteger(v) || v < min || v > max)
    throw new Error(`ENV ${name}=${raw}: ожидается целое в [${min}; ${max}]`);
  return v;
};
const num = (name, def, min, max) => {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return def;
  const v = Number(raw);
  if (!Number.isFinite(v) || v < min || v > max) throw new Error(`ENV ${name}=${raw}: ожидается число в [${min}; ${max}]`);
  return v;
};
const bool = (name, def) => {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return def;
  return ['1', 'true', 'yes', 'on'].includes(String(raw).toLowerCase());
};
const str = (name, def) => process.env[name] ?? def;

const CPU = Math.max(1, (globalThis.navigator?.hardwareConcurrency ?? 0) || int('ENG6_CPU_FALLBACK', 2, 1, 4096));

export const config = Object.freeze({
  http: {
    host: str('ENG6_HOST', '0.0.0.0'),
    /** 0 = эфемерный порт (назначает ОС) — используется в тестах и контейнерах */
    port: int('ENG6_PORT', 8080, 0, 65535),
    /** жёсткий потолок тела запроса — защита от OOM (Критик: «не падать с OutOfMemory») */
    maxBodyBytes: int('ENG6_MAX_BODY_BYTES', 64 * 1024 * 1024, 1024, 512 * 1024 * 1024),
    maxHeaderBytes: int('ENG6_MAX_HEADER_BYTES', 16 * 1024, 1024, 64 * 1024),
    requestTimeoutMs: int('ENG6_REQUEST_TIMEOUT_MS', 120_000, 1_000, 3_600_000),
    keepAliveTimeoutMs: int('ENG6_KEEPALIVE_MS', 65_000, 1_000, 600_000),
    corsOrigin: str('ENG6_CORS_ORIGIN', '*'),
    trustProxy: bool('ENG6_TRUST_PROXY', false),
    staticDir: str('ENG6_STATIC_DIR', new URL('../frontend', import.meta.url).pathname),
    serveUi: bool('ENG6_SERVE_UI', true),
  },
  pool: {
    /** число воркеров расчёта; 1 = «честно по одному» (требование Критика) */
    workers: int('ENG6_WORKERS', Math.max(1, CPU - 1), 1, 256),
    /** длина очереди ожидания; при переполнении — 429 Too Many Requests */
    maxQueue: int('ENG6_MAX_QUEUE', 512, 0, 100_000),
    /** лимит одновременных синхронных расчётов; сверх — 429 + рекомендация async */
    maxSyncConcurrency: int('ENG6_MAX_SYNC_CONCURRENCY', Math.max(1, CPU - 1), 1, 256),
    taskTimeoutMs: int('ENG6_TASK_TIMEOUT_MS', 300_000, 1_000, 3_600_000),
    workerRestartAfterTasks: int('ENG6_WORKER_RESTART_AFTER', 500, 0, 1_000_000),
    /**
     * Лимит кучи одного воркера, МБ. 0 = авто: totalmem / workers / 2
     * (в контейнере с лимитом 1 ГБ и двумя воркерами по 1536 МБ cgroup убивает
     * процесс целиком — обнаружено нагрузочным стендом, см. DEF-05).
     */
    workerHeapMb: int('ENG6_WORKER_HEAP_MB', 0, 0, 16384),
    resultTtlMs: int('ENG6_RESULT_TTL_MS', 3_600_000, 60_000, 86_400_000),
    maxStoredResults: int('ENG6_MAX_STORED_RESULTS', 256, 8, 100_000),
    maxResultBytes: int('ENG6_MAX_RESULT_BYTES', 64 * 1024 * 1024, 4096, 512 * 1024 * 1024),
    /**
     * Суммарный объём тел запросов, находящихся в обработке (память!).
     * Второй рубеж backpressure: очередь может быть неполной, но 100 запросов
     * по 20 МБ каждый = 2 ГБ буферов и OOM-kill процесса. При превышении —
     * 503 MEMORY_BACKPRESSURE с Retry-After (обнаружено нагрузочным стендом
     * при лимите cgroup 1 ГБ, см. docs/04_TEST_REPORT.md, DEF-04).
     */
    maxInflightBytes: int('ENG6_MAX_INFLIGHT_BYTES', 256 * 1024 * 1024, 1024 * 1024, 4 * 1024 * 1024 * 1024),
  },
  limits: {
    /** жёсткие лимиты экземпляра задачи на уровне API (дублируют core/schema.mjs,
     *  но проверяются ДО передачи в воркер — экономим очередь) */
    maxNodes: int('ENG6_LIMIT_NODES', 200_000, 2, 1_000_000),
    maxLinks: int('ENG6_LIMIT_LINKS', 800_000, 1, 5_000_000),
    maxDemands: int('ENG6_LIMIT_DEMANDS', 20_000, 1, 200_000),
  },
  rateLimit: {
    enabled: bool('ENG6_RATELIMIT_ENABLED', true),
    /** запросов в окно на одного клиента (API-ключ или IP) */
    requestsPerWindow: int('ENG6_RL_REQUESTS', 120, 1, 1_000_000),
    windowMs: int('ENG6_RL_WINDOW_MS', 60_000, 1_000, 3_600_000),
    /** отдельно — тяжёлые задачи (узлы > порога считаются за N обычных) */
    heavyNodeThreshold: int('ENG6_RL_HEAVY_NODES', 5_000, 1, 1_000_000),
    heavyWeight: num('ENG6_RL_HEAVY_WEIGHT', 5, 1, 1000),
    maxTrackedClients: int('ENG6_RL_MAX_CLIENTS', 20_000, 16, 1_000_000),
  },
  auth: {
    enabled: bool('ENG6_AUTH_ENABLED', false),
    /** ключи через запятую; в промышленной эксплуатации — из secret-хранилища */
    apiKeys: str('ENG6_API_KEYS', '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    /** требование подписи заказчика: X-Requestor (ФИО/система) — для аудита */
    requireRequestor: bool('ENG6_REQUIRE_REQUESTOR', true),
  },
  audit: {
    enabled: bool('ENG6_AUDIT_ENABLED', true),
    file: str('ENG6_AUDIT_FILE', new URL('../var/audit.ndjson', import.meta.url).pathname),
    ringSize: int('ENG6_AUDIT_RING', 2000, 16, 1_000_000),
    /** логировать тело запроса целиком (только для отладки; в бою — хэш) */
    logPayload: bool('ENG6_AUDIT_LOG_PAYLOAD', false),
  },
  metrics: {
    enabled: bool('ENG6_METRICS_ENABLED', true),
    path: str('ENG6_METRICS_PATH', '/metrics'),
    bucketsMs: str('ENG6_METRICS_BUCKETS', '10,50,100,250,500,1000,2500,5000,10000,30000,60000,120000,300000')
      .split(',')
      .map((x) => Number(x.trim()))
      .filter(Number.isFinite),
  },
  tls: {
    enabled: bool('ENG6_TLS_ENABLED', false),
    cert: str('ENG6_TLS_CERT', ''),
    key: str('ENG6_TLS_KEY', ''),
    /** NFR-02 Дополнения к ТЗ: передача только по защищённому каналу */
    requireClientCert: bool('ENG6_TLS_REQUIRE_CLIENT_CERT', false),
    ca: str('ENG6_TLS_CA', ''),
  },
  env: str('ENG6_ENV', 'development'),
  version: '1.0.0',
  startedAt: new Date().toISOString(),
});

export function assertConfig() {
  const problems = [];
  if (config.auth.enabled && config.auth.apiKeys.length === 0) problems.push('ENG6_AUTH_ENABLED=true, но ENG6_API_KEYS пуст');
  if (config.tls.enabled && (!config.tls.cert || !config.tls.key)) problems.push('ENG6_TLS_ENABLED=true, но не заданы ENG6_TLS_CERT/ENG6_TLS_KEY');
  if (config.pool.maxQueue === 0 && config.pool.maxSyncConcurrency === 0) problems.push('Нулевые лимиты очереди и синхронных расчётов — сервис не сможет принять ни одной задачи');
  if (config.env === 'production' && !config.tls.enabled) problems.push('production без TLS: нарушение NFR-02 Дополнения к ТЗ');
  return problems;
}

export default config;
