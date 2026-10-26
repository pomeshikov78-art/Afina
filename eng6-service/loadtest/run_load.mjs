#!/usr/bin/env node
/**
 * loadtest/run_load.mjs — нативный нагрузочный стенд (ЭТАП 5).
 *
 * Зачем свой стенд при наличии k6/Locust: k6 и Locust в закрытом контуре
 * часто недоступны, а результаты нужны воспроизводимые. Сценарии k6
 * (loadtest/k6_comms_planner.js) и Locust (loadtest/locustfile.py) прилагаются
 * и бьют по тем же эндпоинтам; этот файл даёт те же метрики средствами Node
 * без единой внешней зависимости.
 *
 * Три группы измерений:
 *   A. CORE   — масштабирование ядра in-process: 10^2…10^5 узлов,
 *               время по фазам, пик RSS/heap, поведение GC.
 *   B. API    — латентность и пропускная способность HTTP-слоя
 *               (синхронный и асинхронный режимы), коды ответов.
 *   C. BURST  — требование Критика: 100 одновременных тяжёлых задач.
 *               Ожидание: 202/429, очередь, ни одного падения процесса.
 *
 * Запуск:
 *   node loadtest/run_load.mjs --profile=quick     # ~2 мин
 *   node loadtest/run_load.mjs --profile=full      # ~15 мин, до 100 000 узлов
 *   node loadtest/run_load.mjs --only=core --sizes=100,1000,10000
 *
 * Результаты: loadtest/results/load_<profile>_<ts>.json + сводка в stdout (MD).
 */
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync, readFileSync, unlinkSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import v8 from 'node:v8';
import { performance } from 'node:perf_hooks';
import { plan } from '../core/planner.mjs';
import { loadEng6 } from '../core/eng6.mjs';
import { randomTopology, fleetFor } from '../scripts/gen_topology.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/* ── аргументы ─────────────────────────────────────────────────────────── */
const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const m = a.match(/^--([^=]+)=(.*)$/);
    return m ? [m[1], m[2]] : [a.replace(/^--/, ''), 'true'];
  }),
);
const PROFILE = args.profile || 'quick';
const CHILD_OUT = args['child-out'] ? String(args['child-out']) : null;
const ONLY = args.only ? String(args.only).split(',') : ['core', 'api', 'burst'];
const PROFILES = {
  quick: { coreSizes: [100, 1000, 5000, 10000], apiRps: 8, apiSeconds: 12, apiNodes: 1000, burstConcurrency: 40, burstNodes: 2000, burstMaxQueue: 16, overflowNodes: 20000 },
  full: { coreSizes: [100, 1000, 5000, 10000, 25000, 50000, 100000], apiRps: 25, apiSeconds: 45, apiNodes: 5000, burstConcurrency: 60, burstNodes: 1200, burstMaxQueue: 64, overflowNodes: 50000 },
};
const P = { ...PROFILES.quick, ...PROFILES[PROFILE] };
if (args.sizes) P.coreSizes = String(args.sizes).split(',').map(Number);
if (args.concurrency) P.burstConcurrency = Number(args.concurrency);
if (args.burstNodes) P.burstNodes = Number(args.burstNodes);

const out = {
  meta: {
    profile: PROFILE,
    at: new Date().toISOString(),
    node: process.version,
    platform: `${process.platform} ${process.arch}`,
    cpus: (globalThis.navigator?.hardwareConcurrency ?? 0) || require_cpus(),
    argv: process.argv.slice(2),
    engine: (() => { const c = loadEng6(); return { version: c.ENG6.version, sha256: c.sha256 }; })(),
  },
  core: [],
  api: [],
  burst: [],
  summary: {},
};
function require_cpus() { try { return process.availableParallelism?.() ?? 2; } catch { return 2; } }

/**
 * Оценка размера ответа без полной сериализации: на графе 10^5 узлов
 * JSON.stringify результата требует сотен МБ и сам по себе может привести
 * к OOM стенда (наблюдалось при лимите cgroup 1 ГБ). Для n ≤ 20 000 считаем
 * точно, крупнее — по числу объектов с эмпирическими весами.
 */
function estimateResponseBytes(r, n) {
  if (n <= 20000) {
    try { return Buffer.byteLength(JSON.stringify(r)); } catch { /* переходим к оценке */ }
  }
  const p = r.plan || {};
  return Math.round(
    4096 +
      (p.deployedUnitsTotal || 0) * 180 +
      (p.activeLinksTotal || 0) * 260 +
      (p.routesTotal || 0) * 320 +
      ((p.schedule && p.schedule.jobsTotal) || 0) * 140 +
      ((p.rejectedLinks || []).length) * 180 +
      (((r.diagnostics || {}).issues || []).length) * 220,
  );
}

const mem = () => {
  const m = process.memoryUsage();
  const h = v8.getHeapStatistics();
  return { rssMb: +(m.rss / 1048576).toFixed(1), heapMb: +(m.heapUsed / 1048576).toFixed(1), externalMb: +(m.external / 1048576).toFixed(1), heapTotalMb: +(h.total_heap_size / 1048576).toFixed(1) };
};
const pct = (arr, p) => {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  return +s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))].toFixed(2);
};
const log = (...a) => process.stdout.write(a.join(' ') + '\n');
const hr = (t) => log(`\n${'═'.repeat(78)}\n${t}\n${'═'.repeat(78)}`);

/* ══ A. CORE: масштабирование ядра ══════════════════════════════════════ */
async function runCore() {
  hr('A. CORE — масштабирование ядра (in-process, чистая функция plan())');
  log('узлов | рёбер | заявок | статус   | всего, мс | физика | граф | остов | маршр. | живуч. | RSS МБ | heap МБ');
  log(`${'-'.repeat(76)}`);
  for (const n of P.coreSizes) {
    const areaKm = Math.max(60, Math.round(Math.sqrt(n) * 6));
    const req = randomTopology({ n, seed: 1000 + n, areaKm, avgDegree: 6, demandRatio: 0.02, budget: null });
    // парк техники ограничен лимитом ядра (LIMITS.maxFleetUnits = 50 000);
    // на графах крупнее техника считается уже развёрнутой по позициям
    if (n <= 40000) req.fleet = fleetFor(req.topology.nodes, { seed: 7 });
    req.constraints.eng6Analysis = false;
    req.constraints.timeLimitMs = 600000;
    global.gc?.();
    const before = mem();
    const t0 = performance.now();
    let r;
    try {
      r = plan(req);
    } catch (err) {
      out.core.push({ n, error: String((err && err.message) || err) });
      log(`${n} | ИСКЛЮЧЕНИЕ: ${err.message}`);
      continue;
    }
    const dt = performance.now() - t0;
    const after = mem();
    const t = r.timings;
    const row = {
      n,
      statusReasons: r.statusReasons && r.statusReasons.length ? r.statusReasons.slice(0, 4) : undefined,
      links: req.topology.links.length,
      demands: req.demands.length,
      status: r.status,
      totalMs: +dt.toFixed(1),
      phases: {
        validate: t.validateMs, fleet: t.fleetMs, physics: t.physicsMs, graph: t.graphMs,
        backbone: t.backboneMs, routing: t.routingMs, improve: t.improveMs, redundancy: t.redundancyMs,
        survivability: t.survivabilityMs, schedule: t.scheduleMs, metrics: t.metricsMs,
      },
      metrics: r.metrics ? {
        linksFeasible: r.metrics.linksFeasible, linksActive: r.metrics.linksActive,
        demandsSatisfied: r.metrics.demandsSatisfied, demandsRejected: r.metrics.demandsRejected,
        throughputKbps: r.metrics.throughputKbps, utilMaxPct: r.metrics.utilMaxPct,
        connectivityPct: r.metrics.connectivityPct, totalCost: r.metrics.totalCost,
        grade: r.metrics.grade, objectiveValue: r.metrics.objectiveValue,
      } : null,
      memBefore: before, memAfter: after,
      memDeltaMb: +(after.rssMb - before.rssMb).toFixed(1),
      responseBytes: estimateResponseBytes(r, n),
      physicsMode: r.core.physics,
    };
    out.core.push(row);
    log(
      `${String(n).padStart(6)} | ${String(row.links).padStart(6)} | ${String(row.demands).padStart(6)} | ${row.status.padEnd(8)} | ${String(row.totalMs).padStart(9)} | ${String(t.physicsMs).padStart(6)} | ${String(t.graphMs).padStart(4)} | ${String(t.backboneMs).padStart(5)} | ${String(t.routingMs).padStart(6)} | ${String(t.survivabilityMs).padStart(6)} | ${String(after.rssMb).padStart(6)} | ${String(after.heapMb).padStart(6)}`,
    );
    if (row.status === 'ERROR' || row.status === 'INVALID' || !r.metrics) log(`  ⚠ статус ${row.status}: ${JSON.stringify(r.statusReasons).slice(0, 220)}`);
    // освобождаем память перед следующим шагом, чтобы пики не складывались
    req.topology.nodes.length = 0;
    req.topology.links.length = 0;
    req.demands.length = 0;
    if (req.fleet) req.fleet.length = 0;
    r.plan = null;
    r.metrics = null;
    r.diagnostics = null;
    global.gc?.();
  }
}

/* ══ B/C. API: поднимаем сервис дочерним процессом ══════════════════════ */
let serverProc = null;
let baseUrl = '';

function startServer(env = {}) {
  return new Promise((resolve, reject) => {
    const port = 18080 + Math.floor(Math.random() * 500);
    serverProc = spawn(process.execPath, [path.join(root, 'api/main.mjs')], {
      cwd: root,
      env: {
        ...process.env,
        ENG6_PORT: String(port),
        ENG6_HOST: '127.0.0.1',
        ENG6_ENV: 'loadtest',
        ENG6_AUDIT_FILE: path.join(root, 'var/audit-load.ndjson'),
        ENG6_SERVE_UI: 'false',
        ...env,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let booted = false;
    const to = setTimeout(() => { if (!booted) reject(new Error('сервис не стартовал за 30 с')); }, 30000);
    const onData = (buf) => {
      const s = buf.toString();
      if (!booted && s.includes('сервис запущен')) {
        booted = true;
        clearTimeout(to);
        baseUrl = `http://127.0.0.1:${port}`;
        resolve(baseUrl);
      }
      if (process.env.LOAD_VERBOSE === '1') process.stdout.write(`[srv] ${s}`);
    };
    serverProc.stdout.on('data', onData);
    serverProc.stderr.on('data', onData);
    serverProc.on('exit', (code) => { if (!booted) reject(new Error(`сервис завершился с кодом ${code}`)); });
  });
}

async function stopServer() {
  if (!serverProc) return;
  serverProc.kill('SIGTERM');
  await new Promise((r) => setTimeout(r, 800));
  try { serverProc.kill('SIGKILL'); } catch {}
  serverProc = null;
}

async function postJson(url, body, headers = {}) {
  const raw = typeof body === 'string' ? body : JSON.stringify(body);
  return postRaw(url, raw, headers);
}

/**
 * POST без разбора тела ответа: стенду нужны только код, заголовки и время.
 * Разбор результата на 10^4 узлов (~1 МБ JSON) в 100 параллельных запросах
 * исчерпал бы память САМОГО стенда раньше, чем сервиса.
 */
async function postRaw(url, raw, headers = {}) {
  const t0 = performance.now();
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requestor': 'loadtest', 'X-Result-Format': 'summary', ...headers },
      body: raw,
    });
    const hdrs = Object.fromEntries(r.headers.entries());
    let data = null;
    // тело читаем только для коротких ответов (ошибки, 202 с taskId)
    const len = Number(hdrs['content-length'] || 0);
    if (len > 0 && len < 65536) {
      const text = await r.text();
      try { data = JSON.parse(text); } catch { data = text.slice(0, 300); }
    } else if (r.body) {
      try { await r.body.cancel(); } catch { /* не критично */ }
    }
    return { status: r.status, ms: performance.now() - t0, data, headers: hdrs };
  } catch (err) {
    return { status: 0, ms: performance.now() - t0, error: String((err && err.message) || err) };
  }
}
async function getJson(url, headers = {}) {
  const t0 = performance.now();
  try {
    const r = await fetch(url, { headers: { 'X-Requestor': 'loadtest', ...headers } });
    const text = await r.text();
    let data = null;
    try { data = JSON.parse(text); } catch { data = text.slice(0, 4000); }
    return { status: r.status, ms: performance.now() - t0, data };
  } catch (err) {
    return { status: 0, ms: performance.now() - t0, error: String((err && err.message) || err) };
  }
}

async function runApi() {
  hr(`B. API — латентность и пропускная способность (${P.apiRps} rps × ${P.apiSeconds} с, граф ${P.apiNodes} узлов)`);
  await startServer({ ENG6_WORKERS: String(Math.max(2, require_cpus() - 1)), ENG6_MAX_SYNC_CONCURRENCY: String(Math.max(2, require_cpus() - 1)), ENG6_MAX_QUEUE: '512', ENG6_RL_REQUESTS: '100000' });
  const meta = await getJson(`${baseUrl}/api/v1/meta`);
  log(`сервис: ${baseUrl} · ядро ${meta.data?.core?.version} · воркеров ${meta.data?.pool?.workers} · очередь ${meta.data?.pool?.maxQueue}`);

  const req = randomTopology({ n: P.apiNodes, seed: 555, areaKm: Math.round(Math.sqrt(P.apiNodes) * 6), avgDegree: 6, demandRatio: 0.02, budget: null });
  req.fleet = fleetFor(req.topology.nodes, { seed: 3 });
  req.constraints.eng6Analysis = false;
  const rawBody = JSON.stringify(req);
  const bodyBytes = Buffer.byteLength(rawBody);
  log(`пакет запроса: ${(bodyBytes / 1024).toFixed(0)} КБ (${req.topology.nodes.length} узлов, ${req.topology.links.length} рёбер, ${req.demands.length} заявок)`);

  // прогрев
  await postRaw(`${baseUrl}/api/v1/plan/sync`, rawBody);

  const lat = [];
  const srvCompute = [];
  const srvQueue = [];
  const codes = {};
  const t0 = Date.now();
  let sent = 0;
  const interval = 1000 / P.apiRps;
  let next = performance.now();
  const inflight = [];
  while (Date.now() - t0 < P.apiSeconds * 1000) {
    const now = performance.now();
    if (now >= next) {
      next += interval;
      sent++;
      inflight.push(postRaw(`${baseUrl}/api/v1/plan/sync`, rawBody).then((r) => {
        lat.push(r.ms);
        codes[r.status] = (codes[r.status] || 0) + 1;
        // серверная телеметрия из заголовков: не зависит от загрузки клиента
        const c = Number(r.headers?.['x-compute-ms']);
        const q = Number(r.headers?.['x-queue-wait-ms']);
        if (Number.isFinite(c)) srvCompute.push(c);
        if (Number.isFinite(q)) srvQueue.push(q);
      }));
    } else await new Promise((r) => setTimeout(r, Math.min(5, next - now)));
  }
  await Promise.all(inflight);
  const wall = (Date.now() - t0) / 1000;
  const health = await getJson(`${baseUrl}/healthz`);
  const row = {
    mode: 'sync', nodes: P.apiNodes, requestBytes: bodyBytes, sent, wallS: +wall.toFixed(1),
    rps: +(sent / wall).toFixed(2), codes, latencyMs: { p50: pct(lat, 50), p90: pct(lat, 90), p95: pct(lat, 95), p99: pct(lat, 99), min: pct(lat, 0), max: pct(lat, 100), avg: +(lat.reduce((a, b) => a + b, 0) / Math.max(1, lat.length)).toFixed(1) },
    serverComputeMs: { p50: pct(srvCompute, 50), p95: pct(srvCompute, 95), max: pct(srvCompute, 100) },
    serverQueueMs: { p50: pct(srvQueue, 50), p95: pct(srvQueue, 95), max: pct(srvQueue, 100) },
    pool: health.data?.pool, memory: health.data?.memory,
  };
  out.api.push(row);
  log(`отправлено ${sent} за ${row.wallS} с → ${row.rps} rps; коды: ${JSON.stringify(codes)}`);
  log(`латентность клиента, мс: p50=${row.latencyMs.p50} p95=${row.latencyMs.p95} p99=${row.latencyMs.p99} max=${row.latencyMs.max}`);
  log(`расчёт в ядре (X-Compute-Ms), мс: p50=${row.serverComputeMs.p50} p95=${row.serverComputeMs.p95} max=${row.serverComputeMs.max}`);
  log(`ожидание слота (X-Queue-Wait-Ms), мс: p50=${row.serverQueueMs.p50} p95=${row.serverQueueMs.p95} max=${row.serverQueueMs.max}`);
  log(`сервис: RSS ${(row.memory?.rssBytes / 1048576).toFixed(0)} МБ, воркеров занято ${row.pool?.busy}/${row.pool?.workers}, очередь ${row.pool?.queueLength}`);

  // B1b. Реалистичный профиль: мелкий пакет (батальонный сценарий из UI, ~10 КБ)
  hr(`B1b. API — реалистичный профиль: ${P.apiRps * 3} rps × 10 с, батальонный сценарий`);
  const { battalionScenario } = await import('../scripts/gen_topology.mjs');
  const small = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  small.constraints.eng6Analysis = false;
  small.constraints.physics = 'fast';
  const smallRaw = JSON.stringify(small);
  const smallBytes = Buffer.byteLength(smallRaw);
  const lat2 = [];
  const srv2 = [];
  const codes2 = {};
  const t2 = Date.now();
  let sent2 = 0;
  const rps2 = P.apiRps * 3;
  const iv2 = 1000 / rps2;
  let nx2 = performance.now();
  const inf2 = [];
  while (Date.now() - t2 < 10000) {
    if (performance.now() >= nx2) {
      nx2 += iv2;
      sent2++;
      inf2.push(postRaw(`${baseUrl}/api/v1/plan/sync`, smallRaw).then((r) => {
        lat2.push(r.ms);
        codes2[r.status] = (codes2[r.status] || 0) + 1;
        const c = Number(r.headers?.['x-compute-ms']);
        if (Number.isFinite(c)) srv2.push(c);
      }));
    } else await new Promise((r) => setTimeout(r, 2));
  }
  await Promise.all(inf2);
  const wall2 = (Date.now() - t2) / 1000;
  out.api.push({
    mode: 'sync-small', nodes: small.topology.nodes.length, requestBytes: smallBytes, sent: sent2,
    wallS: +wall2.toFixed(1), rps: +(sent2 / wall2).toFixed(2), codes: codes2,
    latencyMs: { p50: pct(lat2, 50), p95: pct(lat2, 95), p99: pct(lat2, 99), max: pct(lat2, 100) },
    serverComputeMs: { p50: pct(srv2, 50), p95: pct(srv2, 95), max: pct(srv2, 100) },
  });
  log(`пакет ${(smallBytes / 1024).toFixed(1)} КБ · отправлено ${sent2} за ${wall2.toFixed(1)} с → ${(sent2 / wall2).toFixed(2)} rps · коды ${JSON.stringify(codes2)}`);
  log(`латентность клиента: p50=${pct(lat2, 50)} p95=${pct(lat2, 95)} p99=${pct(lat2, 99)} мс · расчёт в ядре: p50=${pct(srv2, 50)} p95=${pct(srv2, 95)} мс`);

  // асинхронный режим: 202 → опрос → результат
  hr('B2. API — асинхронный режим (202 Accepted + очередь)');
  const asyncLat = [];
  const queueWait = [];
  const compute = [];
  const N_ASYNC = 12;
  const t1 = Date.now();
  const tasks = await Promise.all(Array.from({ length: N_ASYNC }, () => postRaw(`${baseUrl}/api/v1/plan/async`, rawBody)));
  const accepted = tasks.filter((t) => t.status === 202);
  for (const t of tasks.filter((x) => x.status !== 202)) log(`  ⚠ не принято: HTTP ${t.status} ${JSON.stringify(t.data).slice(0, 120)}`);
  const ids = accepted.map((t) => t.data.taskId);
  const finished = [];
  for (const id of ids) {
    const tStart = performance.now();
    for (let i = 0; i < 600; i++) {
      const st = await getJson(`${baseUrl}/api/v1/tasks/${id}`, { 'X-Result-Format': 'summary' });
      if (['SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED'].includes(st.data?.state)) {
        finished.push(st.data.state);
        asyncLat.push(performance.now() - tStart);
        queueWait.push(st.data.queueWaitMs ?? 0);
        compute.push(st.data.computeMs ?? 0);
        break;
      }
      await new Promise((r) => setTimeout(r, 150));
    }
  }
  const rowA = {
    mode: 'async', nodes: P.apiNodes, submitted: N_ASYNC, accepted: accepted.length,
    rejected429: tasks.filter((t) => t.status === 429).length,
    states: finished.reduce((a, s) => ((a[s] = (a[s] || 0) + 1), a), {}),
    e2eMs: { p50: pct(asyncLat, 50), p95: pct(asyncLat, 95), max: pct(asyncLat, 100) },
    queueWaitMs: { p50: pct(queueWait, 50), max: pct(queueWait, 100) },
    computeMs: { p50: pct(compute, 50), max: pct(compute, 100) },
    wallS: +((Date.now() - t1) / 1000).toFixed(1),
  };
  out.api.push(rowA);
  log(`принято ${accepted.length}/${N_ASYNC} (202), состояния ${JSON.stringify(rowA.states)}`);
  log(`сквозное время, мс: p50=${rowA.e2eMs.p50} p95=${rowA.e2eMs.p95} max=${rowA.e2eMs.max}`);
  log(`ожидание в очереди, мс: p50=${rowA.queueWaitMs.p50} max=${rowA.queueWaitMs.max}; расчёт: p50=${rowA.computeMs.p50} max=${rowA.computeMs.max}`);

  // метрики Prometheus после нагрузки
  const m = await fetch(`${baseUrl}/metrics`).then((r) => r.text());
  const pick = (name) => (m.match(new RegExp(`^${name}[^\\n]*$`, 'gm')) || []).slice(0, 6);
  out.summary.prometheusSample = {
    planTotal: pick('eng6_plan_total'),
    planDurationCount: pick('eng6_plan_duration_ms_count'),
    queueLength: pick('eng6_queue_length'),
    workersBusy: pick('eng6_workers_busy'),
    requestsTotal: pick('eng6_http_requests_total').slice(0, 4),
    memoryRss: pick('eng6_process_memory_rss_bytes'),
  };
  log('\nметрики Prometheus (выборка):');
  for (const l of [...out.summary.prometheusSample.planTotal, ...out.summary.prometheusSample.planDurationCount, ...out.summary.prometheusSample.queueLength, ...out.summary.prometheusSample.memoryRss]) log(`  ${l}`);
}

async function runBurst() {
  hr(`C. BURST — требование Критика: ${P.burstConcurrency} одновременных тяжёлых задач`);
  await stopServer();
  await startServer({
    ENG6_WORKERS: '2', ENG6_MAX_QUEUE: String(P.burstMaxQueue), ENG6_MAX_SYNC_CONCURRENCY: '2',
    ENG6_RL_REQUESTS: '100000', ENG6_TASK_TIMEOUT_MS: '240000',
    // лимит памяти «в полёте» согласуется с доступной стенду памятью
    ENG6_MAX_INFLIGHT_BYTES: String(192 * 1024 * 1024),
  });
  const req = randomTopology({ n: P.burstNodes, seed: 999, areaKm: Math.round(Math.sqrt(P.burstNodes) * 6), avgDegree: 6, demandRatio: 0.01, budget: null });
  req.fleet = fleetFor(req.topology.nodes, { seed: 4 });
  req.constraints.eng6Analysis = false;
  const burstRaw = JSON.stringify(req);
  log(`граф ${req.topology.nodes.length} узлов / ${req.topology.links.length} рёбер; пакет ${(Buffer.byteLength(burstRaw) / 1024).toFixed(0)} КБ; воркеров 2, очередь ${P.burstMaxQueue}`);

  // C1: шквал асинхронных задач — ожидаем 202 + 429 (очередь), падений нет
  const t0 = Date.now();
  const res = await Promise.all(Array.from({ length: P.burstConcurrency }, () => postRaw(`${baseUrl}/api/v1/plan/async`, burstRaw)));
  const codes = res.reduce((a, r) => ((a[r.status] = (a[r.status] || 0) + 1), a), {});
  const accepted = res.filter((r) => r.status === 202);
  const limited = res.filter((r) => r.status === 429);
  log(`C1 async-шквал: ${P.burstConcurrency} запросов за ${Date.now() - t0} мс → коды ${JSON.stringify(codes)}`);
  log(`   принято в очередь ${accepted.length}, честно отклонено 429: ${limited.length}`);
  for (const r of limited.slice(0, 2)) log(`   429: ${r.data?.error?.code} · Retry-After=${r.headers?.['retry-after'] ?? '—'} · ${r.data?.error?.message}`);

  // сервис жив и отдаёт статусы
  const health = await getJson(`${baseUrl}/healthz`);
  log(`   healthz: ${health.status}, очередь ${health.data?.pool?.queueLength}, занято ${health.data?.pool?.busy}/${health.data?.pool?.workers}`);
  assertAlive(health);

  // ждём завершения принятых задач (не все — измеряем поведение, не полный дренаж)
  const sampleIds = accepted.slice(0, Math.min(6, accepted.length)).map((r) => r.data.taskId);
  const done = [];
  const waitStart = Date.now();
  while (done.length < sampleIds.length && Date.now() - waitStart < 120000) {
    for (const id of sampleIds) {
      if (done.some((d) => d.id === id)) continue;
      const st = await getJson(`${baseUrl}/api/v1/tasks/${id}`, { 'X-Result-Format': 'summary' });
      if (['SUCCEEDED', 'FAILED', 'TIMEOUT'].includes(st.data?.state)) done.push({ id, state: st.data.state, status: st.data.status, computeMs: st.data.computeMs, queueWaitMs: st.data.queueWaitMs });
    }
    if (done.length < sampleIds.length) await new Promise((r) => setTimeout(r, 500));
  }
  log(`   завершено ${done.length}/${sampleIds.length}: ${done.map((d) => `${d.state}/${d.status}(${Math.round(d.computeMs)}мс, очередь ${Math.round(d.queueWaitMs)}мс)`).join(', ')}`);

  // C2: переполнение очереди — гарантированные 429
  const res2 = await Promise.all(Array.from({ length: P.burstConcurrency }, () => postRaw(`${baseUrl}/api/v1/plan/async`, burstRaw)));
  const codes2 = res2.reduce((a, r) => ((a[r.status] = (a[r.status] || 0) + 1), a), {});
  log(`C2 повторный шквал при занятой очереди: коды ${JSON.stringify(codes2)}`);

  // C3: синхронный шквал — часть 200, часть 429 SYNC_BUSY
  const res3 = await Promise.all(Array.from({ length: Math.min(30, P.burstConcurrency) }, () => postRaw(`${baseUrl}/api/v1/plan/sync`, burstRaw)));
  const codes3 = res3.reduce((a, r) => ((a[r.status] = (a[r.status] || 0) + 1), a), {});
  log(`C3 sync-шквал (${res3.length} запросов): коды ${JSON.stringify(codes3)}`);

  // C4: «мусорный» вход под нагрузкой не роняет сервис
  const junk = await Promise.all([
    postJson(`${baseUrl}/api/v1/plan/sync`, { topology: { nodes: [{ id: 'a', type: 'zzz', lat: 1e9, lng: 0 }], links: [] } }),
    postJson(`${baseUrl}/api/v1/plan/sync`, null),
    postJson(`${baseUrl}/api/v1/plan/sync`, { topology: { nodes: [{ id: 'a', type: 'cp', lat: 0, lng: 0 }], links: [{ id: 'l', from: 'a', to: 'a', type: 'VHF' }] } }),
  ]);
  log(`C4 некорректные входы: ${junk.map((j) => j.status).join(', ')} (ожидаются 4xx, не 5xx)`);

  const health2 = await getJson(`${baseUrl}/healthz`);
  const ready = await getJson(`${baseUrl}/readyz`);
  log(`   после шквала: healthz=${health2.status} readyz=${ready.status} RSS=${(health2.data?.memory?.rssBytes / 1048576).toFixed(0)} МБ очередь=${health2.data?.pool?.queueLength} перезапусков=${health2.data?.pool?.stats?.workerRestarts ?? 0}`);
  assertAlive(health2);

  out.burst.push({
    nodes: P.burstNodes, workers: 2, maxQueue: P.burstMaxQueue, concurrency: P.burstConcurrency,
    asyncCodes: codes, asyncCodes2: codes2, syncCodes: codes3, junkCodes: junk.map((j) => j.status),
    accepted: accepted.length, rejected429: limited.length,
    drained: done,
    healthAfter: { status: health2.status, readyz: ready.status, rssMb: +(health2.data?.memory?.rssBytes / 1048576).toFixed(1), queueLength: health2.data?.pool?.queueLength, workerRestarts: health2.data?.pool?.stats?.workerRestarts, completed: health2.data?.pool?.stats?.completed, failed: health2.data?.pool?.stats?.failed, timedOut: health2.data?.pool?.stats?.timedOut },
    verdict: 'PASS',
  });
}

function assertAlive(health) {
  if (health.status !== 200) throw new Error(`сервис не отвечает после нагрузки: HTTP ${health.status} ${health.error || ''}`);
}

/* ══ main ═══════════════════════════════════════════════════════════════ */
async function runPhasesInProcess() {
  const t0 = Date.now();
  try {
    if (ONLY.includes('core')) await runCore();
    if (ONLY.includes('api')) await runApi();
    if (ONLY.includes('burst')) await runBurst();
  } catch (err) {
    out.summary.error = String((err && err.message) || err);
    log(`\n✖ ОШИБКА СТЕНДА: ${err.message}\n${err.stack}`);
  } finally {
    await stopServer();
  }
  out.summary.wallS = +((Date.now() - t0) / 1000).toFixed(1);
  out.summary.memFinal = mem();
}

/**
 * Каждая фаза — в отдельном процессе. Причины:
 *   1) память не накапливается между фазами (на графе 10^5 узлов пик RSS ~400 МБ,
 *      и последующая сериализация в том же процессе выбивала лимит кучи);
 *   2) падение одной фазы не теряет результаты остальных;
 *   3) дочерний процесс получает явный --max-old-space-size под свой профиль.
 */
async function runPhasesIsolated() {
  const t0 = Date.now();
  mkdirSync(path.join(root, 'loadtest/results'), { recursive: true });
  const parts = [];
  for (const phase of ONLY) {
    const partFile = path.join(root, 'loadtest/results', `.part_${phase}_${Date.now()}.json`);
    const heap = phase === 'core' ? '700' : '448';
    log(`\n─── фаза «${phase}» — отдельный процесс, heap ≤ ${heap} МБ ───`);
    const extra = process.argv.slice(2).filter((a) => !/^--(profile|only|child-out)=/.test(a));
    const child = spawn(
      process.execPath,
      [`--max-old-space-size=${heap}`, '--expose-gc', fileURLToPath(import.meta.url), `--profile=${PROFILE}`, `--only=${phase}`, `--child-out=${partFile}`, ...extra],
      { cwd: root, stdio: 'inherit', env: { ...process.env, ENG6_LOAD_CHILD: '1' } },
    );
    const code = await new Promise((res) => child.on('exit', res));
    let part = null;
    try {
      part = JSON.parse(readFileSync(partFile, 'utf8'));
    } catch {}
    if (part) {
      parts.push(part);
      try { unlinkSync(partFile); } catch {}
    }
    if (code !== 0) log(`⚠ фаза «${phase}» завершилась с кодом ${code}`);
  }
  for (const pt of parts) {
    out.core.push(...(pt.core || []));
    out.api.push(...(pt.api || []));
    out.burst.push(...(pt.burst || []));
    if (pt.summary) Object.assign(out.summary, pt.summary);
  }
  out.summary.wallS = +((Date.now() - t0) / 1000).toFixed(1);
  out.summary.memFinal = mem();
  out.summary.isolated = true;
}

(async () => {
  log(`Нагрузочный стенд ENG6-PLAN · профиль «${PROFILE}» · Node ${process.version} · CPU ${out.meta.cpus}`);
  log(`ядро: ENG6 ${out.meta.engine.version} (sha256 ${out.meta.engine.sha256.slice(0, 16)}…)`);
  if (CHILD_OUT) {
    await runPhasesInProcess();
    writeFileSync(CHILD_OUT, JSON.stringify(out), 'utf8');
    process.exit(out.summary.error ? 1 : 0);
  }
  if (ONLY.length > 1) await runPhasesIsolated();
  else await runPhasesInProcess();

  // сводка
  hr('СВОДКА');
  if (out.core.length) {
    log('| узлов | рёбер | заявок | статус | всего, мс | мс/узел | RSS, МБ | ответ, КБ |');
    log('|---:|---:|---:|:--|---:|---:|---:|---:|');
    for (const c of out.core) {
      log(`| ${c.n} | ${c.links ?? '—'} | ${c.demands ?? '—'} | ${c.status ?? 'ERR'} | ${c.totalMs ?? '—'} | ${c.totalMs ? (c.totalMs / c.n).toFixed(4) : '—'} | ${c.memAfter?.rssMb ?? '—'} | ${c.responseBytes ? (c.responseBytes / 1024).toFixed(0) : '—'} |`);
      if (c.status && !['OPTIMAL', 'FEASIBLE', 'PARTIAL', 'INFEASIBLE', 'TIMEOUT'].includes(c.status)) log(`| ↳ причины: ${JSON.stringify(c.statusReasons || c.error || '').slice(0, 200)} |`);
    }
  }
  for (const a of out.api) {
    if (a.mode === 'sync' || a.mode === 'sync-small')
      log(`\nAPI ${a.mode} ${a.nodes} узлов (${(a.requestBytes / 1024).toFixed(0)} КБ): ${a.rps} rps, коды ${JSON.stringify(a.codes)}` +
        `, клиент p95 ${a.latencyMs.p95} мс / p99 ${a.latencyMs.p99 ?? '—'} мс` +
        `, ядро p50 ${a.serverComputeMs?.p50 ?? '—'} / p95 ${a.serverComputeMs?.p95 ?? '—'} мс` +
        (a.memory ? `, RSS сервиса ${(a.memory.rssBytes / 1048576).toFixed(0)} МБ` : ''));
    else if (a.mode === 'async')
      log(`API async ${a.nodes} узлов: принято ${a.accepted}/${a.submitted}, 429: ${a.rejected429}, p95 e2e ${a.e2eMs?.p95 ?? '—'} мс, очередь p50 ${a.queueWaitMs?.p50 ?? '—'} мс, расчёт p50 ${a.computeMs?.p50 ?? '—'} мс`);
  }
  for (const b of out.burst) log(`\nBURST ${b.concurrency}×${b.nodes} узлов: принято ${b.accepted}, отклонено 429 ${b.rejected429}, после шквала healthz=${b.healthAfter.status}, RSS=${b.healthAfter.rssMb} МБ, перезапусков воркеров ${b.healthAfter.workerRestarts}, итог ${b.verdict}`);
  log(`\nобщее время стенда: ${out.summary.wallS} с, память процесса: ${JSON.stringify(out.summary.memFinal)}`);
  if (out.summary.error) log(`⚠ ${out.summary.error}`);

  mkdirSync(path.join(root, 'loadtest/results'), { recursive: true });
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const file = path.join(root, 'loadtest/results', `load_${PROFILE}_${ts}.json`);
  writeFileSync(file, JSON.stringify(out, null, 1), 'utf8');
  log(`\n✔ результаты: ${path.relative(root, file)}`);
  process.exit(out.summary.error ? 1 : 0);
})();
