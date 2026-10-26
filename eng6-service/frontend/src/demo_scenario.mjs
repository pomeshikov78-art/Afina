/**
 * frontend/src/demo_scenario.mjs — демонстрационный сценарий для сборки UI.
 *
 * Состав: организационно-штатная структура роты связи (FR-RES-01/02), парк
 * техники с состояниями 🟢🟡🔴 (FR-RES-03), топология с узлом связи
 * (FR-MAP-04) и линиями разных типов (FR-MAP-06), заявки на ресурсы,
 * ограничения (бюджет, задержка, хопы) и веса целевой функции.
 *
 * Используется ТОЛЬКО сборщиком frontend (scripts/build_frontend.mjs):
 * результат расчёта ядра встраивается в index.html, чтобы интерфейс открывался
 * с настоящей визуализацией даже без запущенного сервиса и без сети.
 */

const ORIGIN = { lat: 55.75, lng: 37.62 };
const KM_PER_DEG_LAT = 111.32;

function pos(dxKm, dyKm) {
  const lat = ORIGIN.lat + dyKm / KM_PER_DEG_LAT;
  const lng = ORIGIN.lng + dxKm / (KM_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180));
  return { lat: Math.round(lat * 1e6) / 1e6, lng: Math.round(lng * 1e6) / 1e6 };
}

/** Штатная структура: взвод → расчёт → единица техники. */
const ORG = [
  { subunit: 'Взвод управления', units: [
    { unitId: 'U-00001', kind: 'П-240И — командно-штабная машина', shortName: 'П-240И', nodeType: 'cp', condition: 'green', costDeploy: 30, deployTimeMin: 45, powerW: 900, dx: 0, dy: 0 },
    { unitId: 'U-00002', kind: 'Р-419Л1 — радиорелейная станция', shortName: 'Р-419Л1', nodeType: 'relay', condition: 'green', costDeploy: 18, deployTimeMin: 25, powerW: 1100, dx: 6.4, dy: 4.1 },
    { unitId: 'U-00003', kind: 'Р-419Л1 — радиорелейная станция', shortName: 'Р-419Л1', nodeType: 'relay', condition: 'yellow', costDeploy: 18, deployTimeMin: 30, powerW: 1100, note: 'требуется ЗИП: блок питания', dx: -7.6, dy: -4.8 },
    { unitId: 'U-00004', kind: 'Р-438Н «Белозёр» — станция спутниковой связи', shortName: 'Р-438Н', nodeType: 'sat', condition: 'green', costDeploy: 60, deployTimeMin: 60, powerW: 1500, dx: 1.9, dy: -3.1 },
  ] },
  { subunit: '1-й радиовзвод (УКВ)', units: [
    { unitId: 'U-00005', kind: 'Р-168-100К — радиостанция УКВ', shortName: 'Р-168-100К', nodeType: 'radio', condition: 'green', costDeploy: 6, deployTimeMin: 10, powerW: 300, dx: -9.3, dy: 6.2 },
    { unitId: 'U-00006', kind: 'Р-168-100К — радиостанция УКВ', shortName: 'Р-168-100К', nodeType: 'radio', condition: 'green', costDeploy: 6, deployTimeMin: 10, powerW: 300, dx: 9.8, dy: -7.3 },
    { unitId: 'U-00007', kind: 'Р-168-100К — радиостанция УКВ', shortName: 'Р-168-100К', nodeType: 'radio', condition: 'green', costDeploy: 6, deployTimeMin: 10, powerW: 300, dx: -3.4, dy: -11.6 },
    { unitId: 'U-00008', kind: 'Р-187П1 «Азарт» — носимая радиостанция', shortName: 'Р-187П1', nodeType: 'radio', condition: 'red', costDeploy: 6, deployTimeMin: 10, powerW: 60, note: 'в ремонте: отказ приёмопередатчика', dx: 12.6, dy: 6.4 },
  ] },
  { subunit: '2-й радиовзвод (КВ)', units: [
    { unitId: 'U-00009', kind: 'Р-161А2М — радиостанция КВ', shortName: 'Р-161А2М', nodeType: 'radio', condition: 'green', costDeploy: 8, deployTimeMin: 20, powerW: 700, dx: 15.4, dy: 11.2 },
    { unitId: 'U-00010', kind: 'Р-161А2М — радиостанция КВ', shortName: 'Р-161А2М', nodeType: 'radio', condition: 'yellow', costDeploy: 8, deployTimeMin: 25, powerW: 700, note: 'требуется ЗИП: антенный ввод', dx: -14.8, dy: -3.2 },
  ] },
  { subunit: 'Взвод наблюдения и РЭБ', units: [
    { unitId: 'U-00011', kind: '1Л261 — пост радионаблюдения', shortName: '1Л261', nodeType: 'observer', condition: 'green', costDeploy: 4, deployTimeMin: 15, powerW: 150, dx: 18.2, dy: -12.4 },
    { unitId: 'U-00012', kind: 'Р-381Т1 «Таран» — станция РЭР', shortName: 'Р-381Т1', nodeType: 'ew_rer', condition: 'green', costDeploy: 20, deployTimeMin: 20, powerW: 400, dx: -17.5, dy: 10.8 },
    { unitId: 'U-00013', kind: 'РБ-341В «Леер-3» — комплекс РЭБ', shortName: 'РБ-341В', nodeType: 'ew_jam', condition: 'red', costDeploy: 25, deployTimeMin: 30, powerW: 2000, note: 'в ремонте: неисправен блок передатчика', dx: -19.6, dy: -11.5 },
  ] },
  { subunit: 'Кабельно-линейный взвод', units: [
    { unitId: 'U-00014', kind: 'П-238 — кабельная машина (ВОЛС)', shortName: 'П-238', nodeType: 'relay', condition: 'green', costDeploy: 12, deployTimeMin: 35, powerW: 250, dx: 3.2, dy: 8.6 },
  ] },
];

const ALL_UNITS = ORG.flatMap((g) => g.units.map((u) => ({ ...u, subunit: g.subunit, parentId: g.subunit })));

/** Размещаем только исправную и ограниченно пригодную технику (FR-RES-03). */
const PLACED = ALL_UNITS.filter((u) => u.condition !== 'red');

const NODE_ID = {};
for (const u of PLACED) NODE_ID[u.unitId] = `n-${u.unitId.slice(-5).toLowerCase()}`;

/**
 * Радиопараметры, отличающиеся от пресетов ENG6 (направленные антенны,
 * повышенная чувствительность приёмников разведпостов).
 */
const RADIO_OVERRIDE = {
  ew_rer: { txPowerDbm: 36, txGainDbi: 12, rxGainDbi: 12, rxSensDbm: -112, antHeightM: 8, freqMhz: 150, feederLossDb: 1.5, noiseFigureDb: 4 },
  ew_jam: { txPowerDbm: 50, txGainDbi: 14, rxGainDbi: 8, rxSensDbm: -95, antHeightM: 9, freqMhz: 300, feederLossDb: 2, noiseFigureDb: 6 },
  observer: { txPowerDbm: 33, txGainDbi: 6, rxGainDbi: 6, rxSensDbm: -106, antHeightM: 5, freqMhz: 450, feederLossDb: 0.8, noiseFigureDb: 6 },
  sat: { txPowerDbm: 40, txGainDbi: 38, rxGainDbi: 38, rxSensDbm: -118, antHeightM: 2, freqMhz: 12000, feederLossDb: 2, noiseFigureDb: 1.2 },
  relay: { txPowerDbm: 43, txGainDbi: 24, rxGainDbi: 24, rxSensDbm: -107, antHeightM: 18, freqMhz: 150, feederLossDb: 2, noiseFigureDb: 5 },
};

function nodeFor(u) {
  const p = pos(u.dx, u.dy);
  return {
    id: NODE_ID[u.unitId],
    type: u.nodeType,
    name: `${u.shortName} · ${u.unitId}`,
    lat: p.lat,
    lng: p.lng,
    status: 'active',
    unitId: u.unitId,
    parentId: u.subunit === 'Взвод управления' ? null : NODE_ID['U-00001'],
    condition: u.condition,
    tags: [u.subunit],
    ...(RADIO_OVERRIDE[u.nodeType] || {}),
  };
}

/** Узел связи (FR-MAP-04): КП + спутниковая станция + кабельная машина. */
const GROUP_MEMBERS = ['U-00001', 'U-00004', 'U-00014'].map((id) => NODE_ID[id]).filter(Boolean);
const GROUP_ID = 'us-bat-1';

/**
 * Топология: ВОЛС внутри узла связи, УКВ/ДМВ к ротным и взводным объектам,
 * РРЛ на дальние направления, КВ — резерв. Тип подбирается по дальности,
 * как это делает инструмент «Линия» в UI (FR-MAP-06).
 */
function typeFor(dKm) {
  if (dKm <= 6) return 'FOC';
  if (dKm <= 20) return 'VHF';
  if (dKm <= 40) return 'UHF';
  if (dKm <= 90) return 'RRL';
  return 'SAT';
}

export function buildDemoScenario() {
  const nodes = [];
  const placed = new Map();
  for (const u of PLACED) {
    const nd = nodeFor(u);
    placed.set(nd.id, nd);
    if (GROUP_MEMBERS.includes(nd.id)) nd.groupId = GROUP_ID;
    nodes.push(nd);
  }
  const cp = placed.get(NODE_ID['U-00001']);
  const gpos = {
    lat: Math.round(((GROUP_MEMBERS.map((id) => placed.get(id).lat).reduce((a, b) => a + b, 0)) / GROUP_MEMBERS.length) * 1e6) / 1e6,
    lng: Math.round(((GROUP_MEMBERS.map((id) => placed.get(id).lng).reduce((a, b) => a + b, 0)) / GROUP_MEMBERS.length) * 1e6) / 1e6,
  };
  nodes.push({
    id: GROUP_ID, type: cp ? cp.type : 'cp', name: 'Узел связи батальона УС-1',
    lat: gpos.lat, lng: gpos.lng, status: 'active', parentId: null,
    children: GROUP_MEMBERS, tags: ['узел связи'],
  });

  const links = [];
  let li = 0;
  const add = (from, to, type, extra = {}) => {
    if (!from || !to || from === to) return;
    if (links.some((l) => (l.from === from && l.to === to) || (l.from === to && l.to === from))) return;
    links.push({ id: `l${++li}`, from, to, type: type || typeFor(dist(from, to)), priority: 2, isBackup: false, ...extra });
  };
  const dist = (a, b) => {
    const na = nodes.find((n) => n.id === a), nb = nodes.find((n) => n.id === b);
    if (!na || !nb) return 0;
    const dLat = ((nb.lat - na.lat) * Math.PI) / 180;
    const dLng = ((nb.lng - na.lng) * Math.PI) / 180;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos((na.lat * Math.PI) / 180) * Math.cos((nb.lat * Math.PI) / 180) * Math.sin(dLng / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.sqrt(h));
  };

  // внутренние линии узла связи — ВОЛС
  for (const m of GROUP_MEMBERS) add(GROUP_ID, m, 'FOC', { priority: 1 });
  // магистрали от узла связи
  const hub = GROUP_ID;
  for (const u of PLACED) {
    const id = NODE_ID[u.unitId];
    if (GROUP_MEMBERS.includes(id)) continue;
    add(hub, id, null, { priority: u.nodeType === 'observer' ? 3 : 2 });
  }
  // кольцевой резерв между ретрансляторами (живучесть)
  add(NODE_ID['U-00002'], NODE_ID['U-00009'], 'RRL', { isBackup: true, priority: 3 });
  add(NODE_ID['U-00003'], NODE_ID['U-00010'], 'UHF', { isBackup: true, priority: 3 });
  add(NODE_ID['U-00005'], NODE_ID['U-00012'], 'UHF', { isBackup: true, priority: 3 });

  const demands = [
    { id: 'd-cp-r1', from: hub, to: NODE_ID['U-00005'], rateKbps: 16, maxDelayMs: 150, priority: 1, protected: true },
    { id: 'd-cp-r2', from: hub, to: NODE_ID['U-00006'], rateKbps: 32, maxDelayMs: 150, priority: 1, protected: true },
    { id: 'd-cp-r3', from: hub, to: NODE_ID['U-00007'], rateKbps: 16, maxDelayMs: 200, priority: 2 },
    { id: 'd-cp-hf1', from: hub, to: NODE_ID['U-00009'], rateKbps: 4.8, maxDelayMs: 400, priority: 2, typeHint: 'КВ' },
    { id: 'd-cp-hf2', from: hub, to: NODE_ID['U-00010'], rateKbps: 4.8, maxDelayMs: 400, priority: 3 },
    { id: 'd-cp-np', from: hub, to: NODE_ID['U-00011'], rateKbps: 8, maxDelayMs: 250, priority: 3 },
    { id: 'd-cp-rer', from: hub, to: NODE_ID['U-00012'], rateKbps: 64, maxDelayMs: 250, priority: 2 },
    { id: 'd-cp-rrl', from: hub, to: NODE_ID['U-00002'], rateKbps: 512, maxDelayMs: 120, priority: 1, protected: true },
    { id: 'd-np-rer', from: NODE_ID['U-00011'], to: NODE_ID['U-00012'], rateKbps: 16, maxDelayMs: 300, priority: 4 },
    { id: 'd-hf-rrl', from: NODE_ID['U-00009'], to: NODE_ID['U-00003'], rateKbps: 8, maxDelayMs: 350, priority: 3 },
  ].filter((d) => d.from && d.to && d.from !== d.to);

  const request = {
    meta: { name: 'Демо: батальонный узел связи (рота связи в/ч 41001)', author: 'ENG6-UI demo', seed: 20260116, description: 'Встроенный сценарий визуального планировщика' },
    environment: {
      terrain: { source: 'synth', amplitudeM: 40 },
      clutterType: 'forest',
      weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 },
      ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' },
      miscLossDb: 2,
    },
    topology: { nodes, links },
    demands,
    fleet: ALL_UNITS.map((u) => ({
      unitId: u.unitId, kind: u.kind, nodeType: u.nodeType, condition: u.condition,
      costDeploy: u.costDeploy, deployTimeMin: u.deployTimeMin, powerW: u.powerW, note: u.note || '',
    })),
    constraints: {
      budget: 900, minMarginDb: 3, maxDelayMs: 400, maxHops: 6, maxSimultaneousDeploy: 3,
      reserveFactor: 1.3, requireRedundancy: false, allowYellowUnits: true, allowRedUnits: false,
      physics: 'fast', quality: 'fast', timeLimitMs: 20000, maxUtilizationPct: 85, crewSpeedKmh: 40,
      backbone: 'cp',
    },
    weights: { cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 },
  };

  const oush = [
    {
      id: 'org-root', name: 'Рота связи (в/ч 41001)', type: 'unit',
      children: ORG.map((g) => ({
        id: `org-${g.subunit}`, name: g.subunit, type: 'squad',
        children: g.units.map((u) => ({ id: `veh-${u.unitId}`, type: 'vehicle', name: `${u.kind} · ${u.unitId}`, unit: { ...u, subunit: g.subunit, parentId: g.subunit } })),
      })),
    },
  ];

  return { request, fleet: ALL_UNITS.map((u) => ({ ...u })), oush };
}

export default buildDemoScenario;
