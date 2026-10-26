/**
 * tests/smoke_api.mjs — сквозная проверка API (ЭТАП 3) без внешних инструментов:
 * поднимает сервер на случайном порту и прогоняет все эндпоинты, включая
 * поведение под нагрузкой (429/202/очередь).
 */
process.env.ENG6_PORT = '0';
process.env.ENG6_AUTH_ENABLED = 'false';
process.env.ENG6_AUDIT_FILE = new URL('../var/audit-smoke.ndjson', import.meta.url).pathname;
process.env.ENG6_WORKERS = '2';
process.env.ENG6_MAX_QUEUE = '8';
process.env.ENG6_MAX_SYNC_CONCURRENCY = '1';
process.env.ENG6_RL_REQUESTS = '40';
process.env.ENG6_RL_WINDOW_MS = '60000';

const { ApiServer } = await import('../api/server.mjs');
const { battalionScenario, randomTopology, fleetFor } = await import('../scripts/gen_topology.mjs');

const server = new ApiServer();
const info = await server.listen();
const base = `http://127.0.0.1:${info.port}`;
const H = { 'Content-Type': 'application/json', 'X-Requestor': 'smoke-test/' + encodeURIComponent('Иванов И.И.') };

const j = async (method, path, body, headers = {}, opts = {}) => {
  const payloadOut = opts.raw !== undefined ? opts.raw : body === undefined ? undefined : JSON.stringify(body);
  const r = await fetch(base + path, { method, headers: { ...H, ...headers }, body: payloadOut });
  let payload = null;
  const text = await r.text();
  try {
    payload = JSON.parse(text);
  } catch {
    payload = text; // текст (например /metrics) — без обрезки
  }
  return { status: r.status, headers: Object.fromEntries(r.headers.entries()), body: payload };
};

const log = (...a) => console.log(...a);

log('base', base);
let r = await j('GET', '/healthz');
log('healthz', r.status, JSON.stringify(r.body).slice(0, 160));
r = await j('GET', '/readyz');
log('readyz', r.status);
r = await j('GET', '/api/v1/meta');
log('meta', r.status, 'core', r.body.core?.version, r.body.core?.sha256?.slice(0, 12), 'endpoints', r.body.endpoints?.length);
r = await j('GET', '/api/v1/conformance');
log('conformance', r.status, 'ok=', r.body.ok, 'selfTest', r.body.selfTestTotal, 'regression', r.body.regressionTotal);

r = await j('POST', '/api/v1/plan/sync', battalionScenario());
log('plan/sync', r.status, '→ status', r.body.status, 'compute', r.headers['x-compute-ms'], 'ms; metrics.demands', r.body.metrics?.demandsTotal, 'active', r.body.plan?.activeLinksTotal);

r = await j('POST', '/api/v1/plan/sync', { topology: { nodes: [{ id: 'a', type: 'cp', lat: 999, lng: 0 }], links: [] } });
log('plan/sync invalid', r.status, JSON.stringify(r.body).slice(0, 220));

r = await j('POST', '/api/v1/plan/sync', undefined, {}, { raw: '{"broken json' });
log('plan/sync bad json', r.status, JSON.stringify(r.body).slice(0, 160));

// async + опрос
r = await j('POST', '/api/v1/plan/async', randomTopology({ n: 800, seed: 3, areaKm: 150 }));
log('plan/async', r.status, 'taskId', r.body.taskId, 'location', r.headers.location);
const taskId = r.body.taskId;
let finalTask = null;
for (let i = 0; i < 100; i++) {
  const t = await j('GET', `/api/v1/tasks/${taskId}`);
  if (['SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED'].includes(t.body.state)) {
    finalTask = t.body;
    log('  task', t.body.state, 'status', t.body.status, 'compute', t.body.computeMs, 'wait', t.body.queueWaitMs, 'resultBytes', t.body.resultBytes, 'error', JSON.stringify(t.body.error));
    if (t.body.result) log('  result.status', t.body.result.status, 'metrics.grade', t.body.result.metrics?.grade, 'active', t.body.result.plan?.activeLinksTotal);
    break;
  }
  await new Promise((res) => setTimeout(res, 200));
}
r = await j('GET', `/api/v1/tasks/${taskId}`, undefined, { 'X-Result-Format': 'summary' });
log('task summary', r.status, 'metrics.grade', r.body.result?.metrics?.grade, 'routes shown', r.body.result?.plan?.routes?.length, 'of', r.body.result?.plan?.routesTotal);
r = await j('GET', '/api/v1/tasks?limit=5');
log('tasks list', r.status, 'byState', JSON.stringify(r.body.byState));
r = await j('GET', '/api/v1/tasks/nonexistent');
log('task 404', r.status, r.body.error?.code);

// перегрузка синхронного канала: 1 слот → остальные 429
const heavy = randomTopology({ n: 400, seed: 9, areaKm: 100 });
const burst = await Promise.all([j('POST', '/api/v1/plan/sync', heavy), j('POST', '/api/v1/plan/sync', heavy), j('POST', '/api/v1/plan/sync', heavy)]);
log('sync burst (1 слот):', burst.map((x) => x.status).join(','), burst.find((x) => x.status === 429)?.body?.error?.code);

// переполнение очереди async (maxQueue=8, workers=2)
const big = randomTopology({ n: 6000, seed: 11, areaKm: 400 });
const codes = [];
for (let i = 0; i < 12; i++) codes.push((await j('POST', '/api/v1/plan/async', big)).status);
log('async flood:', codes.join(','));

r = await j('GET', '/api/v1/audit?limit=5');
log('audit', r.status, 'items', r.body.total, 'stats', JSON.stringify(r.body.stats));
r = await j('GET', '/metrics');
const mtext = String(r.body);
log('metrics', r.status, 'lines', mtext.split('\n').length, '| plan_total:', mtext.includes('eng6_plan_total'), '| queue_length:', mtext.includes('eng6_queue_length'), '| histogram:', mtext.includes('eng6_plan_duration_ms_bucket'));
log('metrics sample:', mtext.split('\n').filter((l) => l.startsWith('eng6_plan_total') || l.startsWith('eng6_workers_busy') || l.startsWith('eng6_queue_rejected')).slice(0, 8).join(' | '));
r = await j('GET', '/');
log('UI /', r.status, typeof r.body === 'string' ? r.body.slice(0, 60).replace(/\n/g, ' ') : 'html bytes');

await server.close();
log('closed');
