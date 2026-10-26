/**
 * integrations/erp_connector.mjs — коннектор к внешней ERP/CRM/системе
 * диспетчеризации («Путь А» ЭТАПА 4).
 *
 * Назначение: забрать данные из учётной системы, скормить их ядру ENG6-PLAN
 * и вернуть результат обратно в систему диспетчеризации — без участия человека
 * и без картографического интерфейса.
 *
 * Коннектор демонстрационно-промышленный: источники и приёмники реализованы
 * как подключаемые адаптеры (CSV/JSON-файл → заглушка БД), а транспорт до ядра —
 * настоящий REST-клиент с повторами, экспоненциальной паузой, обработкой
 * 429/503 (Retry-After) и опросом асинхронных задач.
 *
 * Запуск:
 *   node integrations/erp_connector.mjs --from=examples/regional_500.json \
 *        --api=http://localhost:8080/api/v1 --requestor="Диспетчер АСУ" \
 *        --out=var/erp_result.json --mode=auto
 *
 * Код возврата: 0 — план построен (OPTIMAL/FEASIBLE/PARTIAL),
 *               2 — план невыполним (INFEASIBLE), 3 — ошибка интеграции.
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Чистые функции UI (парсер CSV, построитель пакета) переиспользуются из
 * frontend/src/app.js без дублирования: файл исполняется в изолированном
 * V8-контексте с подставным `module`, как в tests/unit/frontend_pure.test.mjs.
 * Браузерная часть при этом не запускается (document не определён).
 */
function loadUiPure() {
  const src = readFileSync(path.resolve(__dirname, '../frontend/src/app.js'), 'utf8');
  const sandbox = { module: { exports: {} }, console };
  sandbox.window = undefined;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(src, ctx, { filename: 'app.js', timeout: 15000 });
  const pure = sandbox.module.exports?.pure;
  if (!pure || typeof pure.parseCsv !== 'function') throw new Error('не удалось загрузить чистые функции UI (parseCsv)');
  return pure;
}
const { parseCsv, csvToScenario } = loadUiPure();

/* ── аргументы ─────────────────────────────────────────────────────────── */
const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const m = a.match(/^--([^=]+)=(.*)$/);
    return m ? [m[1], m[2]] : [a.replace(/^--/, ''), 'true'];
  }),
);
const API = String(args.api || process.env.ENG6_API || 'http://localhost:8080/api/v1').replace(/\/+$/, '');
const FROM = args.from || 'examples/battalion.json';
const OUT = args.out || 'var/erp_result.json';
const MODE = String(args.mode || 'auto').toLowerCase(); // sync | async | auto
const REQUESTOR = args.requestor || 'ERP-коннектор';
const KEY = args.key || process.env.ENG6_API_KEY || '';
const AUTO_NODES = Number(args.autoNodes || 3000); // порог выбора async
const MAX_RETRIES = Number(args.retries || 5);
const SYNC_THRESHOLD_MS = Number(args.syncTimeoutMs || 120000);
const POLL_INTERVAL_MS = Number(args.pollMs || 700);
const POLL_MAX_MS = Number(args.pollMaxMs || 600000);

const log = (level, msg, extra) =>
  process.stdout.write(JSON.stringify({ ts: new Date().toISOString(), level, msg, ...(extra || {}) }) + '\n');

/** Заголовок X-Requestor обязан быть ASCII → кириллица в percent-encoding. */
const requestorHeader = () => encodeURIComponent(REQUESTOR);

/* ══ 1. АДАПТЕР ИСТОЧНИКА (ERP/CRM → пакет ENG6-PLAN) ═════════════════════ */

/**
 * Прочитать исходные данные учётной системы.
 * Поддерживаются:
 *   • JSON-экспорт в формате запроса ENG6-PLAN (топологией занимается АСУ);
 *   • JSON-экспорт «сырой» ERP: {assets:[…], sites:[…], circuits:[…], orders:[…]};
 *   • CSV-выгрузки (две секции [nodes]/[links] либо плоские листы).
 *
 * В промышленной эксплуатации здесь стоит драйвер БД (SELECT из map_object,
 * node_group, link_line, CommAsset) — интерфейс тот же: на выход подаётся
 * объект запроса ENG6-PLAN.
 */
function loadFromErp(file) {
  const raw = readFileSync(file, 'utf8');
  if (/\.csv$/i.test(file)) return fromCsv(raw);
  const data = JSON.parse(raw);
  if (data.request && data.request.topology) return normalizeRequest(data.request, data);
  if (data.topology && Array.isArray(data.topology.nodes)) return normalizeRequest(data, data);
  if (data.assets || data.sites || data.circuits || data.orders) return fromErpModel(data);
  throw new Error('Неизвестный формат источника: ожидаются topology.nodes, request.topology или ERP-модель {assets, sites, circuits, orders}');
}

/**
 * Преобразование «сырой» модели ERP в пакет ENG6-PLAN.
 *   sites   → позиции (id, name, lat, lng, type, parent)
 *   assets  → единицы техники (inventoryNo, model, nodeType, state, cost, deployMin)
 *   circuits→ линии связи (a, b, kind, bandwidth, freq)
 *   orders  → заявки (src, dst, kbps, deadlineMs, prio)
 */
function fromErpModel(d) {
  const stateMap = { ok: 'green', serviceable: 'green', limited: 'yellow', degraded: 'yellow', broken: 'red', repair: 'red', failed: 'red' };
  const kindMap = { fiber: 'FOC', optical: 'FOC', vhf: 'VHF', uhf: 'UHF', hf: 'HF', rrl: 'RRL', microwave: 'RRL', sat: 'SAT', satellite: 'SAT' };
  const typeMap = { hq: 'cp', cp: 'cp', command: 'cp', relay: 'relay', retrans: 'relay', radio: 'radio', station: 'radio', sat: 'sat', observer: 'observer', post: 'observer', ew: 'ew_jam' };
  const nodes = (d.sites || []).map((s) => ({
    id: String(s.id ?? s.code),
    type: typeMap[String(s.type || s.kind || 'radio').toLowerCase()] || 'radio',
    name: s.name || s.title || String(s.id),
    lat: Number(s.lat ?? s.latitude),
    lng: Number(s.lng ?? s.lon ?? s.longitude),
    status: s.status === 'failed' || s.status === 'destroyed' ? 'failed' : 'active',
    parentId: s.parent ?? s.parentId ?? null,
    ...(s.txPowerDbm !== undefined ? { txPowerDbm: Number(s.txPowerDbm) } : {}),
    ...(s.antHeightM !== undefined ? { antHeightM: Number(s.antHeightM) } : {}),
  }));
  const fleet = (d.assets || []).map((a) => ({
    unitId: String(a.inventoryNo ?? a.id ?? a.serial),
    kind: a.model || a.name || 'Аппаратура связи',
    nodeType: typeMap[String(a.nodeType || a.type || 'radio').toLowerCase()] || 'radio',
    condition: stateMap[String(a.state || a.condition || 'ok').toLowerCase()] || 'green',
    costDeploy: Number(a.cost ?? a.deployCost ?? 10),
    deployTimeMin: Number(a.deployMin ?? a.setupMinutes ?? 20),
    powerW: Number(a.powerW ?? 0) || undefined,
    note: a.note || a.defect || '',
  }));
  const links = (d.circuits || d.lines || []).map((c, i) => ({
    id: String(c.id ?? `erp-l${i + 1}`),
    from: String(c.a ?? c.from ?? c.src),
    to: String(c.b ?? c.to ?? c.dst),
    type: kindMap[String(c.kind || c.type || 'vhf').toLowerCase()] || 'VHF',
    bwKhz: c.bandwidth ? Number(c.bandwidth) : undefined,
    freqMhz: c.freq ? Number(c.freq) : undefined,
    cost: c.cost !== undefined ? Number(c.cost) : undefined,
    priority: c.priority ? Number(c.priority) : 2,
  }));
  const demands = (d.orders || d.demands || d.tasks || []).map((o, i) => ({
    id: String(o.id ?? `erp-q${i + 1}`),
    from: String(o.src ?? o.from ?? o.a),
    to: String(o.dst ?? o.to ?? o.b),
    rateKbps: Number(o.kbps ?? o.rate ?? o.bandwidth ?? 16),
    maxDelayMs: Number(o.deadlineMs ?? o.maxDelayMs ?? 250),
    priority: Number(o.prio ?? o.priority ?? 3),
    protected: !!(o.protected ?? o.critical ?? false),
  }));
  return normalizeRequest(
    {
      meta: { name: d.projectName || 'Импорт из ERP', author: REQUESTOR },
      environment: d.environment,
      topology: { nodes, links },
      fleet,
      demands,
      constraints: d.constraints,
      weights: d.weights,
    },
    d,
  );
}

function fromCsv(raw) {
  // сначала пробуем штатный конвертер UI (секции [nodes]/[links], русские заголовки)
  const viaUi = csvToScenario(raw);
  if (viaUi.nodes.length || viaUi.links.length) {
    return {
      meta: { name: 'Импорт CSV', author: REQUESTOR },
      topology: {
        nodes: viaUi.nodes.map((n) => ({ ...n, lat: Number(n.lat), lng: Number(n.lng) })),
        links: viaUi.links.map((l) => ({ priority: 2, ...l })),
      },
      // секция [demands] — наряды на каналы связи (русские заголовки поддерживаются)
      demands: (viaUi.demands || []).filter((d) => d.from && d.to && d.from !== d.to),
    };
  }
  const { header, rows } = parseCsv(raw);
  const lower = header.map((h) => h.toLowerCase());
  const get = (row, ...names) => {
    for (const n of names) {
      const i = lower.indexOf(n);
      if (i >= 0 && row[i] !== undefined && row[i] !== '') return row[i];
    }
    return undefined;
  };
  const isLinks = lower.includes('from') && lower.includes('to');
  if (isLinks) {
    const links = rows.map((r, i) => ({
      id: String(get(r, 'id') || `csv-l${i + 1}`),
      from: String(get(r, 'from', 'от')),
      to: String(get(r, 'to', 'до')),
      type: String(get(r, 'type', 'тип') || 'VHF'),
      bwKhz: get(r, 'bwkhz', 'полоса') !== undefined ? Number(get(r, 'bwkhz', 'полоса')) : undefined,
    }));
    return { topology: { nodes: [], links }, meta: { name: 'Импорт CSV (линии)', author: REQUESTOR } };
  }
  const nodes = rows.map((r, i) => ({
    id: String(get(r, 'id') || `csv-n${i + 1}`),
    type: String(get(r, 'type', 'тип') || 'radio'),
    name: String(get(r, 'name', 'наименование') || get(r, 'id') || `Узел ${i + 1}`),
    lat: Number(String(get(r, 'lat', 'широта')).replace(',', '.')),
    lng: Number(String(get(r, 'lng', 'lon', 'долгота')).replace(',', '.')),
    parentId: get(r, 'parentid', 'parent', 'подчинение') || null,
    unitId: get(r, 'unitid', 'инвентарный номер') || null,
  }));
  return { topology: { nodes, links: [] }, meta: { name: 'Импорт CSV (узлы)', author: REQUESTOR } };
}

/** Довести пакет до полного контракта ENG6-PLAN (дефолты, округление координат). */
function normalizeRequest(req, source = {}) {
  const round6 = (x) => (Number.isFinite(x) ? Math.round(x * 1e6) / 1e6 : x);
  const nodes = (req.topology?.nodes || []).map((n) => ({ ...n, lat: round6(Number(n.lat)), lng: round6(Number(n.lng)) }));
  const links = req.topology?.links || [];
  return {
    meta: { name: req.meta?.name || 'План из ERP', author: req.meta?.author || REQUESTOR, requestId: source.requestId || `erp-${Date.now().toString(36)}`, ...(req.meta?.seed !== undefined ? { seed: req.meta.seed } : {}) },
    // Рельеф: если источник не передал DEM/описание местности, используется
    // ПЛОСКАЯ модель. Синтетический рельеф (source: 'synth') здесь недопустим —
    // он создаёт вымышленные возвышенности и, как следствие, фиктивные
    // дифракционные потери до 40 дБ, из-за которых реальный план выглядит
    // невыполнимым. Для учёта рельефа источник обязан передать terrain.source
    // и данные DEM (либо заказчик включает 'synth' осознанно).
    environment: req.environment || { clutterType: 'open', terrain: { source: 'flat', amplitudeM: 0 }, weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 }, ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' }, miscLossDb: 2 },
    topology: { nodes, links },
    ...(req.fleet ? { fleet: req.fleet } : {}),
    demands: req.demands || [],
    constraints: {
      minMarginDb: 3, maxDelayMs: 250, maxHops: 12, reserveFactor: 1.3,
      maxSimultaneousDeploy: 4, physics: 'auto', timeLimitMs: 120000,
      eng6Analysis: true, ...(req.constraints || {}),
    },
    weights: req.weights || { cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 },
  };
}

/* ══ 2. ТРАНСПОРТ ДО ЯДРА (с повторами и учётом backpressure) ═════════════ */

async function call(method, p, body, extraHeaders = {}) {
  const headers = { 'X-Requestor': requestorHeader(), ...extraHeaders };
  if (KEY) headers.Authorization = `Bearer ${KEY}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(API + p, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch { data = text.slice(0, 400); }
  return { status: r.status, headers: Object.fromEntries(r.headers.entries()), data };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Отправить план в расчёт. Режим auto: мелкие задачи — sync, тяжёлые — async.
 * Обрабатывает 429/503 по Retry-After с экспоненциальной паузой.
 */
async function submitPlan(request) {
  const n = request.topology.nodes.length;
  const mode = MODE === 'auto' ? (n > AUTO_NODES ? 'async' : 'sync') : MODE;
  log('info', 'отправка в ядро', { mode, nodes: n, links: request.topology.links.length, demands: (request.demands || []).length, api: API });

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    if (mode === 'sync') {
      const r = await call('POST', '/plan/sync', request, { 'X-Result-Format': 'full' });
      if (r.status === 200 || r.status === 422) return { mode, result: r.data, http: r.status, computeMs: Number(r.headers['x-compute-ms']), queueWaitMs: Number(r.headers['x-queue-wait-ms']) };
      if (r.status === 429 || r.status === 503) {
        const wait = retryAfter(r) * 1000 || backoff(attempt);
        log('warn', `ядро занято (${r.data?.error?.code || r.status}), повтор через ${Math.round(wait)} мс`, { attempt, suggest: r.data?.error?.details?.suggest });
        await sleep(wait);
        continue;
      }
      throw new Error(`sync: HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 300)}`);
    }
    // async
    const r = await call('POST', '/plan/async', request);
    if (r.status === 202) {
      const taskId = r.data.taskId;
      log('info', 'задача принята в очередь', { taskId, position: r.data.position, estimatedWaitMs: r.data.estimatedWaitMs });
      return { mode, taskId, result: await pollTask(taskId) };
    }
    if (r.status === 429 || r.status === 503) {
      const wait = retryAfter(r) * 1000 || backoff(attempt);
      log('warn', `очередь/память ограничены (${r.data?.error?.code || r.status}), повтор через ${Math.round(wait)} мс`, { attempt, queueLength: r.data?.error?.details?.queueLength });
      await sleep(wait);
      continue;
    }
    if (r.status === 422) return { mode, result: r.data, http: r.status };
    throw new Error(`async: HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 300)}`);
  }
  throw new Error(`ядро не приняло задачу за ${MAX_RETRIES} попыток (429/503)`);
}

function retryAfter(r) {
  const v = Number(r.headers['retry-after']);
  return Number.isFinite(v) && v > 0 ? Math.min(v, 60) : 0;
}
function backoff(attempt) {
  return Math.min(30000, 500 * 2 ** (attempt - 1)) + Math.floor(Math.random() * 250);
}

async function pollTask(taskId) {
  const t0 = Date.now();
  let last = '';
  while (Date.now() - t0 < POLL_MAX_MS) {
    const r = await call('GET', `/tasks/${taskId}`, undefined, { 'X-Result-Format': 'full' });
    if (r.status === 404) throw new Error(`задача ${taskId} не найдена (возможно, истёк TTL результата)`);
    const st = r.data?.state;
    if (st !== last) {
      last = st;
      log('info', `состояние задачи: ${st}`, { taskId, position: r.data?.position, queueWaitMs: r.data?.queueWaitMs });
    }
    if (st === 'SUCCEEDED') return r.data.result;
    if (st === 'FAILED' || st === 'TIMEOUT' || st === 'CANCELLED') {
      const res = r.data.result || { status: st, statusReasons: [{ code: st, message: r.data?.error?.message || 'задача не выполнена' }] };
      return res;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  throw new Error(`задача ${taskId} не завершилась за ${POLL_MAX_MS / 1000} с`);
}

/* ══ 3. АДАПТЕР ПРИЁМНИКА (результат → система диспетчеризации) ═══════════ */

/**
 * Сформировать «обратный пакет» для системы диспетчеризации: плоские таблицы,
 * готовые к загрузке в БД заказчика (map_object, node_group, link_line,
 * route, deployment_wave).
 */
function toDispatchPayload(request, response, meta) {
  const p = response.plan || {};
  const m = response.metrics || {};
  return {
    header: {
      generatedAt: new Date().toISOString(),
      source: 'ENG6-PLAN',
      engineVersion: response.core?.engineVersion || null,
      engineSha256: response.core?.engineSha256 || null,
      plannerVersion: response.core?.planner || null,
      requestId: meta.requestId || null,
      status: response.status,
      grade: m.grade || null,
      objectiveValue: m.objectiveValue ?? null,
      computeMs: meta.computeMs ?? null,
      totalCost: m.totalCost ?? null,
      budget: m.budget ?? null,
    },
    // map_object: позиции размещения с координатами 6 знаков (NFR-03)
    map_object: (p.deployedUnits || []).map((d) => ({
      object_id: d.nodeId, object_type: d.nodeType, unit_id: d.unitId, condition: d.condition,
      geo_lat: d.lat, geo_lng: d.lng, cost: d.cost, wave_index: d.wave, start_min: d.startMin,
    })),
    // link_line: активированные линии с качеством
    link_line: (p.activeLinks || []).map((l) => ({
      line_id: l.id, a: l.from, b: l.to, kind: l.type, length_km: l.distKm,
      margin_db: l.marginDb, sens_margin_db: l.sensMarginDb, capacity_kbps: l.capacityKbps,
      load_kbps: l.loadKbps, util_pct: l.utilPct, delay_ms: l.delayMs,
      availability_pct: l.availabilityPct, cost: l.cost, role: l.role,
    })),
    link_rejected: (p.rejectedLinks || []).map((l) => ({ line_id: l.id, a: l.from, b: l.to, kind: l.type, code: l.code, reason: l.reason, margin_db: l.marginDb, length_km: l.distKm })),
    // route: маршруты заявок
    route: (p.routes || []).map((r) => ({
      demand_id: r.demandId, src: r.from, dst: r.to, requested_kbps: r.requestedKbps,
      granted_kbps: r.grantedKbps, deficit_kbps: r.deficitKbps, status: r.status, reason: r.reason,
      delay_ms: r.delayMs, limit_ms: r.maxDelayMs, hops: r.hops, margin_db: r.marginDb,
      availability_pct: r.availabilityPct, redundant: !!r.redundant,
      path_nodes: r.path, path_lines: r.links,
      backup_nodes: r.backupPath?.nodes || null, backup_lines: r.backupPath?.links || null,
    })),
    // deployment_wave: расписание развертывания
    deployment_wave: (p.schedule?.waves || []).map((w) => ({
      wave_index: w.index, t_start_min: w.tStartMin, t_end_min: w.tEndMin,
      duration_min: w.durationMin, crews_used: w.crewsUsed,
      items: w.items.map((i) => ({ object_id: i.nodeId, unit_id: i.unitId, crew: i.crew, start_min: i.startMin, end_min: i.endMin })),
    })),
    schedule_summary: { makespan_min: p.schedule?.makespanMin ?? null, crews: p.schedule?.crews ?? null, crew_util_pct: p.schedule?.crewUtilPct ?? null, critical_path: (p.schedule?.criticalPath || []).map((c) => c.id) },
    // survivability: для отчёта о живучести
    survivability: p.survivability || null,
    // what-if: что сделать, чтобы план стал полным
    actions_required: (response.diagnostics?.infeasibility?.minimalRelaxations || []).map((x) => ({
      action: x.code, at_lat: x.at?.lat ?? null, at_lng: x.at?.lng ?? null, value: x.value ?? null,
      line_id: x.linkId ?? null, connects: x.wouldConnect ?? null, span_km: x.spanKm ?? null,
      required_eirp_dbm: x.requiredEirpDbm ?? null, reason: x.reason ?? null,
    })),
    issues: (response.diagnostics?.issues || []).slice(0, 100).map((i) => ({ sev: i.sev, code: i.code, title: i.title, text: i.text })),
    recommendations: (response.diagnostics?.recommendations || []).map((r) => ({ id: r.id, priority: r.priority, title: r.title, text: r.text })),
    metrics: m,
  };
}

function pushToDispatch(payload, outFile) {
  // В промышленной эксплуатации здесь — INSERT/UPSERT в БД диспетчеризации
  // либо POST в её REST-контур. В стендовой версии пишем файл и логируем объём.
  mkdirSync(path.dirname(path.resolve(outFile)), { recursive: true });
  writeFileSync(outFile, JSON.stringify(payload, null, 1), 'utf8');
  log('info', 'результат передан в систему диспетчеризации', {
    file: outFile,
    bytes: JSON.stringify(payload).length,
    map_object: payload.map_object.length,
    link_line: payload.link_line.length,
    route: payload.route.length,
    waves: payload.deployment_wave.length,
    actions_required: payload.actions_required.length,
  });
}

/* ══ 4. ГЛАВНЫЙ СЦЕНАРИЙ ══════════════════════════════════════════════════ */

async function main() {
  log('info', 'ERP-коннектор ENG6-PLAN запущен', { api: API, from: FROM, mode: MODE, requestor: REQUESTOR, key: KEY ? 'задан' : 'не задан' });

  // 0a) офлайн-режим контроля интеграции: собрать пакет и выгрузить, не требуя
  //     доступности ядра (иначе --dump-request бесполезен при отладке маппинга)
  if (args['dump-request']) {
    const req = loadFromErp(FROM);
    const dumpTo = String(args['dump-request']);
    if (dumpTo === 'stdout') process.stdout.write(JSON.stringify(req, null, 1) + '\n');
    else {
      mkdirSync(path.dirname(path.resolve(dumpTo)), { recursive: true });
      writeFileSync(dumpTo, JSON.stringify(req, null, 1), 'utf8');
    }
    log('info', 'пакет для ядра сформирован и выгружен (ядро не запрашивалось)', {
      file: dumpTo, bytes: JSON.stringify(req).length,
      nodes: req.topology.nodes.length, links: req.topology.links.length, demands: (req.demands || []).length, fleet: (req.fleet || []).length,
    });
    return 0;
  }

  // 0) проверка доступности и версии ядра
  const meta = await call('GET', '/meta');
  if (meta.status !== 200) throw new Error(`ядро недоступно: HTTP ${meta.status} по адресу ${API}/meta`);
  log('info', 'ядро на связи', { service: meta.data.service, engine: `${meta.data.core.name} ${meta.data.core.version}`, sha256: String(meta.data.core.sha256).slice(0, 16), planner: meta.data.planner, limits: meta.data.apiLimits });

  const conf = await call('GET', '/conformance');
  if (conf.status === 200 && conf.data && conf.data.ok !== true)
    log('warn', 'конформанс ядра НЕ пройден — результаты расчёта недостоверны', { failedSelf: conf.data.selfTestFailed, failedReg: conf.data.regressionFailed });
  else log('info', 'конформанс ядра подтверждён', { selfTest: `${conf.data.selfTestTotal - conf.data.selfTestFailed.length}/${conf.data.selfTestTotal}`, regression: `${conf.data.regressionTotal - conf.data.regressionFailed.length}/${conf.data.regressionTotal}` });

  // 1) забор данных из учётной системы
  const t0 = Date.now();
  const request = loadFromErp(FROM);
  if (!request.environment || request.environment.terrain?.source === 'flat')
    log('warn', 'рельеф источником не задан — использована плоская модель (дифракция не учитывается); для учёта местности передайте terrain/DEM', { terrain: request.environment?.terrain });
  log('info', 'данные получены из источника', {
    file: FROM, nodes: request.topology.nodes.length, links: request.topology.links.length,
    demands: (request.demands || []).length, fleet: (request.fleet || []).length, ms: Date.now() - t0,
  });

  // 2) расчёт
  const t1 = Date.now();
  const { result, mode, taskId, computeMs, queueWaitMs } = await submitPlan(request);
  log('info', 'расчёт завершён', { mode, taskId: taskId || null, status: result.status, grade: result.metrics?.grade, computeMs: computeMs ?? null, queueWaitMs: queueWaitMs ?? null, wallMs: Date.now() - t1 });

  if (result.status === 'INVALID') {
    log('error', 'вход отклонён валидатором ядра', { reasons: (result.statusReasons || []).slice(0, 10) });
    return 3;
  }
  if (result.status === 'ERROR') {
    log('error', 'сбой ядра', { reasons: result.statusReasons });
    return 3;
  }

  // 3) сводка для диспетчера
  const m = result.metrics || {};
  log('info', 'сводка плана', {
    status: result.status,
    заявки: `${m.demandsSatisfied}/${m.demandsTotal} обслужено, ${m.demandsPartial || 0} частично, ${m.demandsRejected || 0} отклонено`,
    полоса: `${m.throughputKbps} кбит/с из ${m.capacityTotalKbps} кбит/с (загрузка max ${m.utilMaxPct}%)`,
    задержка: `avg ${m.avgDelayMs} мс / max ${m.maxDelayMs} мс (нарушений ${m.delayViolations})`,
    стоимость: `${m.totalCost}${m.budget !== null && m.budget !== undefined ? ` из ${m.budget} (${m.budgetUsedPct}%)` : ''} у.е.`,
    связность: `${m.connectivityPct}% (компонент ${m.componentsCount})`,
    живучесть: `ЕТО ${m.spofNodes} узл., мостов ${m.bridgeLinks}${m.minCutKbps != null ? `, min-cut ${m.minCutKbps} кбит/с` : ''}`,
    расписание: `${m.scheduleMakespanMin} мин, волн ${m.scheduleWaves}, бригад ${result.plan?.schedule?.crews}, загрузка ${m.crewUtilPct}%`,
    оценка: `${m.grade} (F=${m.objectiveValue})`,
  });
  const actions = result.diagnostics?.infeasibility?.minimalRelaxations || [];
  if (actions.length) log('warn', 'требуемые действия для полного выполнения', { actions: actions.slice(0, 12) });
  if (result.diagnostics?.eng6Analysis && !result.diagnostics.eng6Analysis.skipped)
    log(result.diagnostics.eng6Analysis.verdict === 'CONSISTENT' ? 'info' : 'warn', 'перекрёстная проверка ENG6', { verdict: result.diagnostics.eng6Analysis.verdict, ratings: result.diagnostics.eng6Analysis.ratings, comparison: result.diagnostics.eng6Analysis.comparison });

  // 4) возврат результата в систему диспетчеризации
  const payload = toDispatchPayload(request, result, { requestId: request.meta?.requestId, computeMs, mode });
  pushToDispatch(payload, OUT);

  const exitCode = result.status === 'INFEASIBLE' ? 2 : result.status === 'TIMEOUT' ? 2 : 0;
  log(exitCode === 0 ? 'info' : 'warn', 'коннектор завершил работу', { exitCode, status: result.status, outFile: OUT });
  return exitCode;
}

/ true — запуск как исполняемый сценарий; при импорте модуль не запускается */
const isMain = (() => {
  try {
    return process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
})();

if (isMain) {
  main()
    .then((code) => process.exit(code))
    .catch((err) => {
      log('error', 'ошибка интеграции', { message: String((err && err.message) || err), stack: process.env.ENG6_STRICT === '1' ? String(err && err.stack) : undefined });
      process.exit(3);
    });
}

export { loadFromErp, fromErpModel, fromCsv, normalizeRequest, toDispatchPayload, submitPlan, pollTask, main, isMain };
