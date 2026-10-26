/**
 * loadtest/k6_comms_planner.js — сценарий нагрузочного тестирования k6 (ЭТАП 5).
 *
 * Генерирует случайные графы связи 10^2…10^5 узлов и «бомбит» ими API ENG6-PLAN,
 * замеряя время отклика, долю ошибок и поведение очереди.
 *
 * Запуск:
 *   k6 run -e BASE_URL=http://localhost:8080 loadtest/k6_comms_planner.js
 *   k6 run -e PROFILE=burst  -e BASE_URL=http://eng6-api:8080 loadtest/k6_comms_planner.js
 *   k6 run -e NODES=50000 -e CONCURRENCY=100 -e DURATION=5m loadtest/k6_comms_planner.js
 *
 * Профили:
 *   smoke  — 1 VU, 30 с, 1 000 узлов          (проверка доступности)
 *   load   — 10 VU, 3 мин, 1 000…5 000 узлов  (штатная нагрузка)
 *   stress — ramp 1→200 VU, 10 мин            (поиск предела)
 *   burst  — CONCURRENCY одновременно, тяжёлые графы, ожидание 202/429 (требование Критика)
 *
 * Пороги (thresholds) соответствуют НТЗ: p95 ≤ 3 с для sync, доля 5xx = 0,
 * доля 429 ≤ 60 % (иначе сервис не справляется с заявленным RPS).
 */
import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { SharedArray } from 'k6/data';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const PROFILE = (__ENV.PROFILE || 'load').toLowerCase();
const NODES = Number(__ENV.NODES || 0);
const CONCURRENCY = Number(__ENV.CONCURRENCY || 100);
const DURATION = __ENV.DURATION || '3m';

/* ── пользовательские метрики ─────────────────────────────────────────── */
const planStatus = new Counter('eng6_plan_status');
const rejected429 = new Rate('eng6_rejected_429');
const serverErrors = new Rate('eng6_server_errors');
const syncLatency = new Trend('eng6_sync_latency_ms', true);
const asyncE2E = new Trend('eng6_async_e2e_ms', true);
const queueWait = new Trend('eng6_queue_wait_ms', true);
const computeMs = new Trend('eng6_compute_ms', true);
const graphNodes = new Trend('eng6_graph_nodes', true);

/* ── генератор топологий (детерминированный, без внешних данных) ───────── */
function mulberry32(seed) {
  let t = seed >>> 0;
  return function () {
    t = (t + 0x6d2b79f5) >>> 0;
    let x = Math.imul(t ^ (t >>> 15), 1 | t);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}

function typeForDistance(dKm) {
  if (dKm <= 25) return 'VHF';
  if (dKm <= 45) return 'UHF';
  if (dKm <= 90) return 'RRL';
  return 'SAT';
}

/**
 * Случайный граф: n узлов в квадрате areaKm × areaKm, рёбра — k ближайших
 * соседей через сеточную корзину (иначе O(n²) на 10^5 узлов не построить).
 */
function makeTopology(n, seed, opts) {
  const o = opts || {};
  const rnd = mulberry32(seed);
  const areaKm = o.areaKm || Math.max(60, Math.round(Math.sqrt(n) * 6));
  const avgDegree = o.avgDegree || 6;
  const lat0 = 55.75;
  const lng0 = 37.62;
  const nodes = new Array(n);
  const xs = new Float64Array(n);
  const ys = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const x = (rnd() - 0.5) * areaKm;
    const y = (rnd() - 0.5) * areaKm;
    xs[i] = x;
    ys[i] = y;
    const lat = lat0 + y / 111.32;
    const lng = lng0 + x / (111.32 * Math.cos((lat * Math.PI) / 180));
    const r = rnd();
    const type = i === 0 ? 'cp' : r < 0.02 ? 'cp' : r < 0.14 ? 'relay' : r < 0.2 ? 'observer' : r < 0.26 ? 'sat' : 'radio';
    nodes[i] = { id: 'n' + i, type, name: type === 'cp' ? 'КП' : 'Узел ' + i, lat: Math.round(lat * 1e6) / 1e6, lng: Math.round(lng * 1e6) / 1e6, status: 'active', parentId: i === 0 ? null : 'n0' };
  }
  const cell = Math.max(1e-6, areaKm / Math.max(8, Math.sqrt(n)));
  const buckets = {};
  const key = (cx, cy) => cx + ':' + cy;
  for (let i = 0; i < n; i++) {
    const k = key(Math.floor((xs[i] + areaKm) / cell), Math.floor((ys[i] + areaKm) / cell));
    (buckets[k] = buckets[k] || []).push(i);
  }
  const links = [];
  const seen = {};
  const k = Math.max(1, Math.min(n - 1, avgDegree));
  for (let i = 0; i < n; i++) {
    const cx = Math.floor((xs[i] + areaKm) / cell);
    const cy = Math.floor((ys[i] + areaKm) / cell);
    const cand = [];
    for (let ring = 0; ring < 4 && cand.length < k * 3; ring++) {
      for (let dx = -ring; dx <= ring; dx++) {
        for (let dy = -ring; dy <= ring; dy++) {
          if (ring > 0 && Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue;
          const arr = buckets[key(cx + dx, cy + dy)];
          if (arr) for (let q = 0; q < arr.length; q++) if (arr[q] !== i) cand.push(arr[q]);
        }
      }
    }
    cand.sort((a, b) => {
      const da = (xs[i] - xs[a]) ** 2 + (ys[i] - ys[a]) ** 2;
      const db = (xs[i] - xs[b]) ** 2 + (ys[i] - ys[b]) ** 2;
      return da - db || a - b;
    });
    for (let t = 0; t < Math.min(k, cand.length); t++) {
      const j = cand[t];
      const pk = i < j ? i + ':' + j : j + ':' + i;
      if (seen[pk]) continue;
      seen[pk] = 1;
      const dKm = Math.sqrt((xs[i] - xs[j]) ** 2 + (ys[i] - ys[j]) ** 2);
      links.push({ id: 'e' + links.length, from: nodes[i].id, to: nodes[j].id, type: typeForDistance(dKm), priority: 2 });
    }
  }
  return { nodes, links, rnd };
}

function makeRequest(n, seed) {
  const t = makeTopology(n, seed, {});
  const demands = [];
  const nDemand = Math.max(2, Math.round(n * 0.02));
  const RATES = [2.4, 4.8, 9.6, 16, 16, 32, 64, 128];
  for (let q = 0; q < nDemand; q++) {
    let a = Math.floor(t.rnd() * n);
    let b = Math.floor(t.rnd() * n);
    if (a === b) b = (b + 1) % n;
    demands.push({
      id: 'q' + q,
      from: 'n' + a,
      to: 'n' + b,
      rateKbps: RATES[Math.floor(t.rnd() * RATES.length)],
      maxDelayMs: [150, 250, 400][Math.floor(t.rnd() * 3)],
      priority: 1 + Math.floor(t.rnd() * 5),
      protected: t.rnd() < 0.1,
    });
  }
  return {
    meta: { name: 'k6 ' + PROFILE + ' n=' + n, author: 'k6-loadtest', seed },
    environment: { clutterType: 'open', terrain: { source: 'flat', amplitudeM: 0 }, weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 }, ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' } },
    topology: { nodes: t.nodes, links: t.links },
    demands,
    constraints: { budget: null, minMarginDb: 3, maxDelayMs: 400, maxHops: 24, physics: 'fast', timeLimitMs: 240000, reserveFactor: 1.3, eng6Analysis: false },
    weights: { cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 },
  };
}

/**
 * Кэш пакетов: генерация графа на 50 000 узлов занимает секунды, поэтому
 * пакеты готовятся один раз на VU (SharedArray — общий между VU, read-only).
 */
const SIZES = NODES ? [NODES] : PROFILE === 'smoke' ? [1000] : PROFILE === 'burst' ? [Number(__ENV.BURST_NODES || 10000)] : [1000, 5000, 10000];
const PAYLOADS = new SharedArray('payloads', function () {
  return SIZES.map((n, i) => ({ nodes: n, body: JSON.stringify(makeRequest(n, 4242 + i * 17)) }));
});

const HEADERS = { 'Content-Type': 'application/json', 'X-Requestor': 'k6-loadtest/Инженер', 'Accept': 'application/json' };

/* ── профили нагрузки ─────────────────────────────────────────────────── */
export const options = (() => {
  const common = {
    thresholds: {
      eng6_server_errors: ['rate==0'], // ни одной 5xx — жёсткое требование
      eng6_sync_latency_ms: PROFILE === 'burst' ? [] : ['p(95)<3000'],
      eng6_rejected_429: ['rate<0.6'],
      http_req_failed: ['rate<0.6'],
    },
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
    noConnectionReuse: false,
    insecureSkipTLSVerify: true,
  };
  if (PROFILE === 'smoke') return Object.assign(common, { vus: 1, duration: '30s' });
  if (PROFILE === 'stress')
    return Object.assign(common, {
      stages: [
        { duration: '2m', target: 20 },
        { duration: '3m', target: 80 },
        { duration: '3m', target: 200 },
        { duration: '2m', target: 0 },
      ],
    });
  if (PROFILE === 'burst') return Object.assign(common, { scenarios: { burst: { executor: 'per-vu-iterations', vus: CONCURRENCY, iterations: 1, maxDuration: '10m' } } });
  return Object.assign(common, { scenarios: { load: { executor: 'constant-arrival-rate', rate: Number(__ENV.RPS || 10), timeUnit: '1s', duration: DURATION, preAllocatedVUs: 20, maxVUs: 200 } } });
})();

/* ── сценарии ─────────────────────────────────────────────────────────── */
export function setup() {
  const meta = http.get(`${BASE}/api/v1/meta`, { headers: HEADERS });
  check(meta, { 'meta 200': (r) => r.status === 200 });
  const conf = http.get(`${BASE}/api/v1/conformance`, { headers: HEADERS });
  const okConformance = check(conf, { 'конформанс ENG6 ok': (r) => r.status === 200 && JSON.parse(r.body).ok === true });
  if (!okConformance) throw new Error('конформанс-протокол ENG6 не пройден — нагрузка бессмысленна');
  return { core: meta.status === 200 ? JSON.parse(meta.body).core : null };
}

export default function () {
  if (PROFILE === 'burst') return burstScenario();
  return mixedScenario();
}

function pickPayload() {
  return PAYLOADS[Math.floor(Math.random() * PAYLOADS.length)];
}

function mixedScenario() {
  group('health', function () {
    const h = http.get(`${BASE}/healthz`, { headers: HEADERS });
    check(h, { 'healthz 200': (r) => r.status === 200 });
  });

  const p = pickPayload();
  graphNodes.add(p.nodes);
  const useAsync = p.nodes > 5000 || Math.random() < 0.3;

  if (!useAsync) {
    group('plan/sync', function () {
      const t0 = Date.now();
      const r = http.post(`${BASE}/api/v1/plan/sync`, p.body, { headers: HEADERS, timeout: '300s' });
      syncLatency.add(Date.now() - t0);
      rejected429.add(r.status === 429);
      serverErrors.add(r.status >= 500);
      const c = Number(r.headers['X-Compute-Ms']);
      if (Number.isFinite(c)) computeMs.add(c);
      const ok = check(r, {
        'sync: 200/422/429': () => r.status === 200 || r.status === 422 || r.status === 429,
        'sync: нет 5xx': () => r.status < 500,
        'sync: есть X-Request-Id': () => !!r.headers['X-Request-Id'],
      });
      if (r.status === 200) {
        const body = JSON.parse(r.body);
        planStatus.add(1, { status: body.status });
        check(body, {
          'план содержит metrics': () => !!body.metrics,
          'статус из контракта': () => ['OPTIMAL', 'FEASIBLE', 'PARTIAL', 'INFEASIBLE'].includes(body.status),
          'координаты ≤ 6 знаков': () => (body.plan?.deployedUnits || []).every((d) => String(d.lat).split('.')[1]?.length <= 6),
        });
      }
      if (!ok) console.error(`sync HTTP ${r.status}: ${String(r.body).slice(0, 200)}`);
    });
  } else {
    group('plan/async', function () {
      const t0 = Date.now();
      const r = http.post(`${BASE}/api/v1/plan/async`, p.body, { headers: HEADERS, timeout: '60s' });
      rejected429.add(r.status === 429);
      serverErrors.add(r.status >= 500);
      if (!check(r, { 'async: 202 или 429': () => r.status === 202 || r.status === 429, 'async: Location при 202': () => r.status !== 202 || !!r.headers.Location })) return;
      if (r.status !== 202) return;
      const acc = JSON.parse(r.body);
      const url = `${BASE}${acc.statusUrl}`;
      for (let i = 0; i < 400; i++) {
        sleep(0.5);
        const st = http.get(url, { headers: Object.assign({ 'X-Result-Format': 'summary' }, HEADERS), timeout: '30s' });
        if (st.status !== 200) continue;
        const body = JSON.parse(st.body);
        if (['QUEUED', 'RUNNING'].includes(body.state)) continue;
        asyncE2E.add(Date.now() - t0);
        if (body.queueWaitMs != null) queueWait.add(body.queueWaitMs);
        if (body.computeMs != null) computeMs.add(body.computeMs);
        if (body.state === 'SUCCEEDED') planStatus.add(1, { status: body.status });
        check(body, {
          'async: задача завершилась': () => ['SUCCEEDED', 'FAILED', 'TIMEOUT'].includes(body.state),
          'async: результат содержит metrics': () => body.state !== 'SUCCEEDED' || !!body.result?.metrics,
        });
        break;
      }
    });
  }
  sleep(1);
}

/**
 * Требование Критика: CONCURRENCY одновременных тяжёлых задач.
 * Ожидание: часть 202 (приняты в очередь), часть 429 (честный отказ),
 * ни одной 5xx, сервис остаётся жив (healthz 200, readyz 200/503).
 */
function burstScenario() {
  const p = PAYLOADS[0];
  graphNodes.add(p.nodes);
  const r = http.post(`${BASE}/api/v1/plan/async`, p.body, { headers: HEADERS, timeout: '120s' });
  rejected429.add(r.status === 429);
  serverErrors.add(r.status >= 500);
  check(r, {
    'burst: только 202 или 429': () => r.status === 202 || r.status === 429,
    'burst: нет 5xx': () => r.status < 500,
    'burst: 429 содержит Retry-After': () => r.status !== 429 || !!r.headers['Retry-After'],
  });
  if (r.status === 202) {
    const acc = JSON.parse(r.body);
    const t0 = Date.now();
    for (let i = 0; i < 600; i++) {
      sleep(1);
      const st = http.get(`${BASE}${acc.statusUrl}`, { headers: Object.assign({ 'X-Result-Format': 'summary' }, HEADERS), timeout: '30s' });
      if (st.status !== 200) continue;
      const b = JSON.parse(st.body);
      if (['QUEUED', 'RUNNING'].includes(b.state)) continue;
      asyncE2E.add(Date.now() - t0);
      if (b.queueWaitMs != null) queueWait.add(b.queueWaitMs);
      if (b.computeMs != null) computeMs.add(b.computeMs);
      planStatus.add(1, { status: b.status || b.state });
      break;
    }
  }
  const h = http.get(`${BASE}/healthz`, { headers: HEADERS });
  check(h, { 'burst: сервис жив после шквала': () => h.status === 200 });
}

export function handleSummary(data) {
  return {
    stdout: textSummary(data),
    'loadtest/results/k6_summary.json': JSON.stringify(data, null, 1),
  };
}

function textSummary(data) {
  const m = data.metrics || {};
  const g = (k, f) => (m[k] && m[k].values ? f(m[k].values) : '—');
  const lines = [
    '',
    '═══ ENG6-PLAN · сводка k6 ═══',
    `профиль: ${PROFILE} · базовый URL: ${BASE} · размеры графов: ${SIZES.join(', ')}`,
    `запросов: ${g('http_reqs', (v) => v.count)} · 5xx: ${g('eng6_server_errors', (v) => (v.rate * 100).toFixed(2) + '%')} · 429: ${g('eng6_rejected_429', (v) => (v.rate * 100).toFixed(1) + '%')}`,
    `sync латентность: p95 ${g('eng6_sync_latency_ms', (v) => v['p(95)'].toFixed(0))} мс · p99 ${g('eng6_sync_latency_ms', (v) => v['p(99)'].toFixed(0))} мс`,
    `расчёт в ядре: p95 ${g('eng6_compute_ms', (v) => v['p(95)'].toFixed(0))} мс`,
    `async e2e: p95 ${g('eng6_async_e2e_ms', (v) => v['p(95)'].toFixed(0))} мс · очередь p95 ${g('eng6_queue_wait_ms', (v) => v['p(95)'].toFixed(0))} мс`,
    '',
  ];
  return lines.join('\n');
}
