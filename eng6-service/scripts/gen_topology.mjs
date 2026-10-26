/**
 * scripts/gen_topology.mjs — детерминированные генераторы топологий.
 *
 * Используется тремя потребителями:
 *   • tests/unit/*.test.mjs      — эталонные графы 5–10 узлов (ЭТАП 2);
 *   • loadtest/*.mjs, k6, Locust — случайные графы 10^2…10^5 узлов (ЭТАП 5);
 *   • frontend/index.html        — демо-сценарий «из коробки».
 *
 * Все генераторы детерминированы при фиксированном seed (NFR-DET-01):
 * одинаковый seed → побайтово одинаковый JSON. ГПСЧ — mulberry32.
 */
import { mulberry32 } from '../core/util.mjs';

const R_EARTH = 6371;

/** Перевести «километры на восток/север» в градусы от опорной точки. */
export function offsetToLatlng(lat0, lng0, eastKm, northKm) {
  const lat = lat0 + northKm / 111.32;
  const lng = lng0 + eastKm / (111.32 * Math.cos((lat * Math.PI) / 180));
  return { lat: round6(lat), lng: round6(lng) };
}

function round6(x) {
  return Math.round(x * 1e6) / 1e6;
}

function haversine(lat1, lng1, lat2, lng2) {
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLng = ((lng2 - lng1) * Math.PI) / 180;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLng / 2) ** 2;
  return 2 * R_EARTH * Math.asin(Math.sqrt(a));
}

/** Выбор типа линии по дальности (типовая эвристика планировщика). */
export function linkTypeForDistance(dKm, opts = {}) {
  if (opts.allowFiber && dKm <= (opts.fiberMaxKm ?? 15)) return 'FOC';
  if (dKm <= (opts.vhfMaxKm ?? 25)) return 'VHF';
  if (dKm <= (opts.uhfMaxKm ?? 45)) return 'UHF';
  if (dKm <= (opts.rrlMaxKm ?? 90)) return 'RRL';
  return 'SAT';
}

/**
 * Эталонный сценарий «батальон»: 7 узлов, 8 линий, 5 заявок.
 * Топология — «звезда с кольцом»: КП батальона + 3 ротных узла + 2 наблюдателя
 * + ретранслятор. Используется в unit-тестах как «золотой» экземпляр.
 */
export function battalionScenario(opts = {}) {
  const origin = opts.origin || { lat: 55.75, lng: 37.62 };
  const pts = [
    { id: 'cp-bat', type: 'cp', name: 'КП батальона', dx: 0, dy: 0, parentId: null },
    { id: 'r1', type: 'relay', name: 'Ретранслятор Р-1', dx: 6, dy: 4, parentId: 'cp-bat' },
    { id: 'co-a', type: 'radio', name: 'Рота А', dx: -8, dy: 6, parentId: 'cp-bat' },
    { id: 'co-b', type: 'radio', name: 'Рота Б', dx: 9, dy: -7, parentId: 'cp-bat' },
    { id: 'co-c', type: 'radio', name: 'Рота В', dx: -3, dy: -11, parentId: 'cp-bat' },
    { id: 'obs-1', type: 'observer', name: 'НП-1', dx: 14, dy: 9, parentId: 'r1' },
    { id: 'obs-2', type: 'observer', name: 'НП-2', dx: -14, dy: -4, parentId: 'co-a' },
  ];
  const nodes = pts.map((p) => {
    const { lat, lng } = offsetToLatlng(origin.lat, origin.lng, p.dx, p.dy);
    return { id: p.id, type: p.type, name: p.name, lat, lng, status: 'active', parentId: p.parentId };
  });
  const edges = [
    ['cp-bat', 'r1', 'FOC'],
    ['cp-bat', 'co-a', 'VHF'],
    ['cp-bat', 'co-b', 'VHF'],
    ['cp-bat', 'co-c', 'VHF'],
    ['r1', 'co-b', 'VHF'],
    ['co-a', 'co-c', 'VHF'],
    ['r1', 'obs-1', 'UHF'],
    ['co-a', 'obs-2', 'UHF'],
  ];
  const links = edges.map(([from, to, type], i) => ({ id: `l${i + 1}`, from, to, type, priority: 2 }));
  const demands = [
    { id: 'd1', from: 'cp-bat', to: 'co-a', rateKbps: 16, maxDelayMs: 120, priority: 1 },
    { id: 'd2', from: 'cp-bat', to: 'co-b', rateKbps: 32, maxDelayMs: 120, priority: 1 },
    { id: 'd3', from: 'cp-bat', to: 'co-c', rateKbps: 16, maxDelayMs: 150, priority: 2 },
    { id: 'd4', from: 'cp-bat', to: 'obs-1', rateKbps: 8, maxDelayMs: 200, priority: 3 },
    // 4 кбит/с при кадре 1 кбит = 250 мс сериализации → лимит задержки 400 мс
    { id: 'd5', from: 'co-a', to: 'obs-2', rateKbps: 4, maxDelayMs: 400, priority: 4 },
  ];
  const fleet = [
    { unitId: 'PU-001', kind: 'П-240И', nodeType: 'cp', condition: 'green', costDeploy: 30, deployTimeMin: 45 },
    { unitId: 'PU-002', kind: 'Р-419Л1', nodeType: 'relay', condition: 'green', costDeploy: 18, deployTimeMin: 25 },
    { unitId: 'PU-003', kind: 'Р-168-100К', nodeType: 'radio', condition: 'green', costDeploy: 6, deployTimeMin: 10 },
    { unitId: 'PU-004', kind: 'Р-168-100К', nodeType: 'radio', condition: 'yellow', costDeploy: 6, deployTimeMin: 12, note: 'требуется ЗИП: блок питания' },
    { unitId: 'PU-005', kind: 'Р-168-100К', nodeType: 'radio', condition: 'green', costDeploy: 6, deployTimeMin: 10 },
    { unitId: 'PU-006', kind: '1Л261', nodeType: 'observer', condition: 'green', costDeploy: 4, deployTimeMin: 15 },
    { unitId: 'PU-007', kind: '1Л261', nodeType: 'observer', condition: 'red', costDeploy: 4, deployTimeMin: 15, note: 'в ремонте, отказ блока АЦП' },
    { unitId: 'PU-008', kind: 'П-240И', nodeType: 'cp', condition: 'green', costDeploy: 30, deployTimeMin: 45 },
  ];
  return {
    meta: { name: opts.name || 'Эталон: батальон', author: 'ENG6-PLAN/tests', seed: 1 },
    environment: opts.environment || { clutterType: 'forest', weather: { rainRateMmHr: 0, temperatureC: 15, humidityPercent: 60 }, ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' }, terrain: { source: 'synth', amplitudeM: 40 } },
    topology: { nodes, links },
    demands,
    fleet,
    constraints: { budget: 400, minMarginDb: 3, maxDelayMs: 250, maxHops: 6, requireRedundancy: false, physics: 'fast', ...(opts.constraints || {}) },
    weights: opts.weights || { cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 },
  };
}

/**
 * Случайная географическая топология: n узлов в квадрате areaKm × areaKm,
 * рёбра — по k ближайшим соседям (симметризация), тип линии — по дальности.
 *
 * Детерминированно при фиксированном seed. Для n > 5000 ближайшие соседи
 * ищутся по сеточной корзине (grid bucketing), иначе O(n²) недопустимо.
 */
export function randomTopology({ n = 1000, seed = 42, areaKm = 200, avgDegree = 6, origin = { lat: 55.75, lng: 37.62 }, demands = null, demandRatio = 0.05, fleet = null, linkTypes = null, clutterType = 'open', budget = null, types = null, localTrafficPct = 0.7 }) {
  const rnd = mulberry32(seed);
  const nodes = new Array(n);
  const xs = new Float64Array(n);
  const ys = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const x = (rnd() - 0.5) * areaKm;
    const y = (rnd() - 0.5) * areaKm;
    xs[i] = x;
    ys[i] = y;
    const { lat, lng } = offsetToLatlng(origin.lat, origin.lng, x, y);
    const type = i === 0 ? 'cp' : pickType(rnd, types);
    nodes[i] = { id: `n${i}`, type, name: type === 'cp' ? 'КП' : `Узел ${i}`, lat, lng, status: 'active', parentId: i === 0 ? null : 'n0' };
  }
  // k ближайших соседей через сеточную корзину
  const k = Math.max(1, Math.min(n - 1, avgDegree));
  const cell = Math.max(1e-6, areaKm / Math.max(8, Math.sqrt(n)));
  const buckets = new Map();
  const key = (cx, cy) => cx * 100003 + cy;
  for (let i = 0; i < n; i++) {
    const cx = Math.floor((xs[i] + areaKm) / cell);
    const cy = Math.floor((ys[i] + areaKm) / cell);
    const kk = key(cx, cy);
    if (!buckets.has(kk)) buckets.set(kk, []);
    buckets.get(kk).push(i);
  }
  const pairs = new Set();
  const links = [];
  const nbr = Array.from({ length: n }, () => []);
  const pushLink = (a, b) => {
    if (a === b) return;
    const pk = a < b ? `${a}:${b}` : `${b}:${a}`;
    if (pairs.has(pk)) return;
    pairs.add(pk);
    nbr[a].push(b);
    nbr[b].push(a);
    const d = haversine(nodes[a].lat, nodes[a].lng, nodes[b].lat, nodes[b].lng);
    const type = linkTypes ? linkTypes[Math.floor(rnd() * linkTypes.length)] : linkTypeForDistance(d, { allowFiber: false });
    links.push({ id: `e${links.length}`, from: nodes[a].id, to: nodes[b].id, type, priority: 2, distKm: round6(d) });
  };
  for (let i = 0; i < n; i++) {
    const cx = Math.floor((xs[i] + areaKm) / cell);
    const cy = Math.floor((ys[i] + areaKm) / cell);
    const cand = [];
    let ring = 0;
    while (cand.length < k * 3 && ring < 6) {
      for (let dx = -ring; dx <= ring; dx++)
        for (let dy = -ring; dy <= ring; dy++) {
          if (ring > 0 && Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue;
          const arr = buckets.get(key(cx + dx, cy + dy));
          if (arr) for (const j of arr) if (j !== i) cand.push(j);
        }
      ring++;
    }
    cand.sort((a, b) => dist2(xs[i], ys[i], xs[a], ys[a]) - dist2(xs[i], ys[i], xs[b], ys[b]) || a - b);
    for (let t = 0; t < Math.min(k, cand.length); t++) pushLink(i, cand[t]);
  }
  const dm = [];
  if (demands) {
    for (const d of demands) dm.push(d);
  } else {
    const count = Math.max(1, Math.round(n * demandRatio));
    // Реалистичное распределение заявок: основа — узкополосные каналы ТЗУ/КВ
    // (2,4…16 кбит/с), реже — широкополосные (64…512 кбит/с) под РРЛ/ВОЛС/СПУТНИК.
    // Без этого на топологии из УКВ-линий 25 кГц почти любая заявка > 32 кбит/с
    // заведомо невыполнима и нагрузочный тест мерил бы только ветвь отказа.
    const RATE_POOL = [2.4, 4.8, 4.8, 9.6, 9.6, 16, 16, 16, 32, 32, 64, 128, 256, 512];
    for (let q = 0; q < count; q++) {
      const a = Math.floor(rnd() * n);
      let b;
      if (rnd() < (localTrafficPct ?? 0.7)) {
        // локальная заявка: 2…5 хопов от источника (преобладающий трафик в сети)
        let cur = a;
        const steps = 2 + Math.floor(rnd() * 4);
        for (let st = 0; st < steps; st++) {
          const list = nbr[cur];
          if (!list || !list.length) break;
          cur = list[Math.floor(rnd() * list.length)];
        }
        b = cur === a ? (a + 1) % n : cur;
      } else {
        b = Math.floor(rnd() * n);
        if (a === b) b = (b + 1) % n;
      }
      dm.push({
        id: `q${q}`,
        from: nodes[a].id,
        to: nodes[b].id,
        rateKbps: RATE_POOL[Math.floor(rnd() * RATE_POOL.length)],
        maxDelayMs: [120, 200, 250, 400][Math.floor(rnd() * 4)],
        priority: 1 + Math.floor(rnd() * 5),
        protected: rnd() < 0.1,
      });
    }
  }
  const req = {
    meta: { name: `Случайная топология n=${n} seed=${seed}`, author: 'gen_topology', seed },
    environment: { clutterType, weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 }, ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' }, terrain: { source: 'synth', amplitudeM: 60 } },
    topology: { nodes, links },
    demands: dm,
    // maxHops/maxDelayMs подобраны под геометрию: при шаге kNN ~ areaKm/√n и
    // дальности УКВ 25 км типичный диаметр сети 15…25 хопов
    constraints: { budget: budget === null ? undefined : budget, minMarginDb: 3, maxDelayMs: 600, maxHops: 24, physics: 'fast', timeLimitMs: 60000, reserveFactor: 1.3, maxUtilizationPct: 90 },
    weights: { cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 },
  };
  if (fleet) req.fleet = fleet;
  return req;
}

function dist2(x1, y1, x2, y2) {
  const dx = x1 - x2;
  const dy = y1 - y2;
  return dx * dx + dy * dy;
}

function pickType(rnd, types) {
  const t = types || { cp: 0.02, relay: 0.12, radio: 0.7, observer: 0.1, sat: 0.06 };
  let x = rnd();
  for (const [k, p] of Object.entries(t)) {
    x -= p;
    if (x <= 0) return k;
  }
  return 'radio';
}

/**
 * Парк техники под n узлов: green 80 %, yellow 15 %, red 5 % (детерминированно).
 * При deficit > 0 часть узлов остаётся без техники — проверка UNIT_SHORTAGE.
 */
export function fleetFor(nodes, { seed = 7, deficit = 0, yellowPct = 0.15, redPct = 0.05 } = {}) {
  const rnd = mulberry32(seed);
  const units = [];
  const byType = new Map();
  for (const nd of nodes) byType.set(nd.type, (byType.get(nd.type) || 0) + 1);
  let idx = 0;
  for (const [type, count] of byType) {
    const eff = Math.max(0, count - (type === 'radio' ? deficit : 0));
    for (let i = 0; i < eff; i++) {
      const r = rnd();
      const condition = r < redPct ? 'red' : r < redPct + yellowPct ? 'yellow' : 'green';
      units.push({
        unitId: `U-${String(++idx).padStart(5, '0')}`,
        kind: KIND_BY_TYPE[type] || 'Аппаратная',
        nodeType: type,
        condition,
        costDeploy: COST_BY_TYPE[type] ?? 10,
        deployTimeMin: DEPLOY_BY_TYPE[type] ?? 20,
        powerW: 200 + Math.floor(rnd() * 800),
        note: condition === 'red' ? 'в ремонте' : condition === 'yellow' ? 'требуется ЗИП' : '',
      });
    }
  }
  return units;
}

const KIND_BY_TYPE = { cp: 'П-240И (командно-штабная машина)', relay: 'Р-419Л1 (ретранслятор)', radio: 'Р-168-100К (КВ/УКВ)', observer: '1Л261 (наблюдательный пост)', sat: 'Р-438Н «Белозёр»', ew_jam: 'РБ-341В «Леер-3»', ew_rlr: '1Л260 «Кольчуга»', ew_rer: 'Р-381Т1 «Таран»' };
const COST_BY_TYPE = { cp: 30, relay: 18, radio: 6, observer: 4, sat: 60, ew_jam: 25, ew_rlr: 25, ew_rer: 20 };
const DEPLOY_BY_TYPE = { cp: 45, relay: 25, radio: 10, observer: 15, sat: 60, ew_jam: 30, ew_rlr: 30, ew_rer: 20 };

/**
 * Граничный случай «сеть несвязна»: две изолированные группы узлов и заявка
 * между ними. Ожидание: status ∈ {PARTIAL, INFEASIBLE} + minimalRelaxations с ADD_RELAY.
 */
export function disconnectedScenario(opts = {}) {
  const base = battalionScenario(opts);
  const nodes = base.topology.nodes.slice(0, 5); // cp-bat, r1, co-a, co-b, co-c
  const far = offsetToLatlng(base.topology.nodes[0].lat, base.topology.nodes[0].lng, 400, 300);
  nodes.push({ id: 'far-1', type: 'radio', name: 'Дальний узел 1', lat: far.lat, lng: far.lng, status: 'active', parentId: null });
  nodes.push({ id: 'far-2', type: 'radio', name: 'Дальний узел 2', ...offsetToLatlng(far.lat, far.lng, 8, 3), status: 'active', parentId: 'far-1' });
  const links = base.topology.links.filter((l) => !['obs-1', 'obs-2'].includes(l.from) && !['obs-1', 'obs-2'].includes(l.to));
  links.push({ id: 'lf1', from: 'far-1', to: 'far-2', type: 'VHF', priority: 2 });
  return {
    ...base,
    meta: { ...base.meta, name: 'Граничный случай: несвязная сеть' },
    topology: { nodes, links },
    demands: [
      { id: 'd1', from: 'cp-bat', to: 'co-a', rateKbps: 16, maxDelayMs: 120, priority: 1 },
      { id: 'd2', from: 'cp-bat', to: 'co-b', rateKbps: 32, maxDelayMs: 120, priority: 1 },
      { id: 'dx', from: 'cp-bat', to: 'far-1', rateKbps: 16, maxDelayMs: 300, priority: 1 },
      { id: 'dy', from: 'co-a', to: 'far-2', rateKbps: 8, maxDelayMs: 300, priority: 2 },
    ],
    fleet: fleetFor(nodes, { seed: 11 }),
  };
}

/**
 * Граничный случай «заявок больше, чем ёмкости каналов»: узкая ВОЛС-горловина
 * и 8 заявок по 512 кбит/с через неё. Ожидание: PARTIAL + CAPACITY_SHORTAGE.
 */
export function oversubscribedScenario(opts = {}) {
  const origin = opts.origin || { lat: 55.75, lng: 37.62 };
  const mk = (id, type, dx, dy, parentId = null) => {
    const { lat, lng } = offsetToLatlng(origin.lat, origin.lng, dx, dy);
    return { id, type, name: id, lat, lng, status: 'active', parentId };
  };
  const nodes = [mk('cp', 'cp', 0, 0), mk('hub', 'relay', 10, 0, 'cp'), mk('a1', 'radio', 20, 5, 'hub'), mk('a2', 'radio', 20, -5, 'hub'), mk('a3', 'radio', 25, 0, 'hub'), mk('a4', 'radio', 20, 10, 'hub')];
  const links = [
    { id: 'l-cp-hub', from: 'cp', to: 'hub', type: 'VHF', priority: 1, bwKhz: 25, mod: 'QPSK', fec: 'none' },
    { id: 'l-hub-a1', from: 'hub', to: 'a1', type: 'VHF', priority: 2 },
    { id: 'l-hub-a2', from: 'hub', to: 'a2', type: 'VHF', priority: 2 },
    { id: 'l-hub-a3', from: 'hub', to: 'a3', type: 'VHF', priority: 2 },
    { id: 'l-hub-a4', from: 'hub', to: 'a4', type: 'VHF', priority: 2 },
  ];
  // суммарная заявка 8 × 16 кбит/с = 128 кбит/с при ёмкости УКВ-канала 25 кГц
  // (42,5 кбит/с теоретических / 1,3 резерв ≈ 32,7 кбит/с полезных) — гарантированный дефицит
  const demands = [1, 2, 3, 4].flatMap((i) => [
    { id: `d${i}a`, from: 'cp', to: `a${i}`, rateKbps: 16, maxDelayMs: 200, priority: 1 },
    { id: `d${i}b`, from: `a${i}`, to: 'cp', rateKbps: 16, maxDelayMs: 200, priority: 3 },
  ]);
  return {
    meta: { name: 'Граничный случай: перегрузка каналов', seed: 5 },
    environment: { clutterType: 'open', terrain: { source: 'flat', amplitudeM: 0 } },
    topology: { nodes, links },
    demands,
    fleet: fleetFor(nodes, { seed: 3 }),
    constraints: { budget: 1000, minMarginDb: 3, maxDelayMs: 250, physics: 'fast', maxUtilizationPct: 85, upgradeCostRatio: 0.25, upgradeFactor: 0.5 },
  };
}
