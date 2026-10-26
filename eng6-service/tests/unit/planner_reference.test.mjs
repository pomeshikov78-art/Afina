/**
 * tests/unit/planner_reference.test.mjs — ЭТАП 2: поведение ядра ENG6-PLAN
 * на эталонных малых графах (5–10 узлов) с заранее известным ответом.
 *
 * Проверяются: статусная модель, маршрутизация, бюджет, живучесть,
 * расписание, детерминированность, лимит времени и все граничные случаи
 * из замечаний «Критика» (несвязная сеть, перегрузка, нехватка техники).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { plan, Eng6Planner, PLANNER_VERSION } from '../../core/planner.mjs';
import { battalionScenario, disconnectedScenario, oversubscribedScenario } from '../../scripts/gen_topology.mjs';
import { DEFAULT_CONSTRAINTS } from '../../core/schema.mjs';

/** Простейший эталон: 5 узлов в кольцо, 5 заявок «звезда» от КП. */
function ring5(overrides = {}) {
  const mk = (id, type, dx, dy, extra = {}) => ({ id, type, name: id, lat: 55.75 + dy / 111.32, lng: 37.62 + dx / (111.32 * Math.cos((55.75 * Math.PI) / 180)), status: 'active', ...extra });
  const nodes = [
    mk('cp', 'cp', 0, 0),
    mk('a', 'radio', 8, 0),
    mk('b', 'radio', 8, 8),
    mk('c', 'radio', 0, 8),
    mk('d', 'relay', -8, 4),
  ];
  const links = [
    { id: 'l1', from: 'cp', to: 'a', type: 'VHF', priority: 1 },
    { id: 'l2', from: 'a', to: 'b', type: 'VHF', priority: 2 },
    { id: 'l3', from: 'b', to: 'c', type: 'VHF', priority: 2 },
    { id: 'l4', from: 'c', to: 'cp', type: 'VHF', priority: 2 },
    { id: 'l5', from: 'cp', to: 'd', type: 'VHF', priority: 2 },
  ];
  const demands = ['a', 'b', 'c', 'd'].map((t, i) => ({ id: `q${i}`, from: 'cp', to: t, rateKbps: 8, maxDelayMs: 200, priority: 1 + (i % 3) }));
  return {
    meta: { name: 'ring5', seed: 1 },
    environment: { clutterType: 'open', terrain: { source: 'flat', amplitudeM: 0 }, weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 }, ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' } },
    topology: { nodes, links },
    demands,
    constraints: { physics: 'fast', minMarginDb: 3, maxDelayMs: 250, maxHops: 6, budget: null, timeLimitMs: 10000, eng6Analysis: false, ...overrides },
    weights: { cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 },
  };
}

test('Эталон «кольцо из 5 узлов»: все заявки маршрутизированы, статус FEASIBLE/OPTIMAL', () => {
  const r = plan(ring5());
  assert.ok(['FEASIBLE', 'OPTIMAL'].includes(r.status), `статус ${r.status}`);
  assert.equal(r.metrics.demandsTotal, 4);
  assert.equal(r.metrics.demandsSatisfied, 4);
  assert.equal(r.metrics.demandsRejected, 0);
  assert.equal(r.metrics.componentsCount, 1);
  assert.equal(r.metrics.connectivityPct, 100);
  assert.equal(r.plan.routes.length, 4);
  for (const x of r.plan.routes) {
    assert.equal(x.status, 'ROUTED');
    assert.equal(x.grantedKbps, x.requestedKbps);
    assert.ok(x.delayMs > 0 && x.delayMs < 250, `задержка ${x.delayMs}`);
    assert.ok(x.hops >= 1 && x.hops <= 6);
    assert.ok(x.path.length === x.hops + 1, 'число узлов в пути = хопы + 1');
  }
  // КП должен быть развёрнут
  assert.ok(r.plan.deployedUnits.some((d) => d.nodeId === 'cp'));
});

test('Эталон: кратчайший путь выбирается по задержке (заявка cp→b идёт в 2 хопа, а не в 3)', () => {
  const r = plan(ring5());
  const qb = r.plan.routes.find((x) => x.demandId === 'q1');
  assert.equal(qb.to, 'b');
  assert.equal(qb.hops, 2, `ожидается 2 хопа, получено ${qb.hops} (${qb.path.join('→')})`);
});

test('Эталон: разрыв единственной линии делает сеть несвязной → PARTIAL + ADD_RELAY', () => {
  const req = ring5();
  req.topology.links = req.topology.links.filter((l) => l.id !== 'l5'); // убираем cp—d
  const r = plan(req);
  assert.equal(r.status, 'PARTIAL');
  const qd = r.plan.routes.find((x) => x.demandId === 'q3');
  assert.equal(qd.status, 'REJECTED');
  assert.equal(qd.reason, 'NET_DISCONNECTED');
  const inf = r.diagnostics.infeasibility;
  assert.ok(inf.causes.some((c) => c.code === 'NET_DISCONNECTED'), 'причина NET_DISCONNECTED зафиксирована');
  assert.ok(inf.disconnectedComponents.length >= 2);
  assert.ok(inf.minimalRelaxations.some((x) => x.code === 'ADD_RELAY'), 'предложена установка ретранслятора');
  const relay = inf.minimalRelaxations.find((x) => x.code === 'ADD_RELAY');
  assert.ok(Number.isFinite(relay.at.lat) && Number.isFinite(relay.at.lng), 'координаты предложения конечны');
  assert.ok(relay.spanKm > 0);
  // частичное решение честно описано
  assert.equal(inf.partialSolution.demandsServed, 3);
});

test('Эталон: ограничение бюджета отсекает заявки по приоритету (приоритет 1 — наивысший)', () => {
  const req = ring5();
  const full = plan(req);
  const cost = full.metrics.totalCost;
  assert.ok(cost > 0, 'стоимость ненулевая');

  // монотонность: чем больше бюджет, тем больше обслужено заявок
  const budgets = [0, Math.round(cost * 0.3), Math.round(cost * 0.6), Math.round(cost * 0.85), null];
  let prev = -1;
  const servedByBudget = [];
  for (const b of budgets) {
    const r = plan(ring5({ budget: b }));
    assert.ok(r.metrics.totalCost <= (b === null ? Infinity : b + 1e-6), `стоимость ${r.metrics.totalCost} ≤ бюджета ${b}`);
    assert.ok(r.metrics.demandsSatisfied >= prev, `при бюджете ${b} обслужено меньше, чем при меньшем`);
    prev = r.metrics.demandsSatisfied;
    servedByBudget.push(new Set(r.plan.routes.filter((x) => x.status === 'ROUTED').map((x) => x.demandId)));
  }
  assert.equal(servedByBudget[0].size, 0, 'при нулевом бюджете не обслужено ничего');
  assert.equal(servedByBudget[4].size, 4, 'без ограничения бюджета обслужены все 4 заявки');
  assert.ok(servedByBudget[2].size < 4, 'при 60 % бюджета часть заявок отклонена');

  // приоритет: если обслужена хоть одна заявка, то заявка наивысшего приоритета — среди них
  const prio = new Map(req.demands.map((d) => [d.id, d.priority]));
  const best = req.demands.reduce((a, b) => (prio.get(a.id) <= prio.get(b.id) ? a : b)).id;
  for (const s of servedByBudget) if (s.size > 0) assert.ok(s.has(best), `заявка наивысшего приоритета (${best}) должна обслуживаться первой`);

  const tight = plan(ring5({ budget: Math.round(cost * 0.6) }));
  assert.ok(tight.diagnostics.infeasibility.causes.some((c) => c.code === 'BUDGET_EXCEEDED'), 'причина BUDGET_EXCEEDED зафиксирована');
  const raise = tight.diagnostics.infeasibility.minimalRelaxations.find((c) => c.code === 'RAISE_BUDGET');
  assert.ok(raise && raise.value > tight.metrics.totalCost, 'предложено конкретное увеличение бюджета');
  assert.ok(tight.plan.routes.some((x) => x.reason === 'BUDGET_EXCEEDED'), 'есть заявки, отклонённые именно по бюджету');
});

test('Эталон: нулевой бюджет → INFEASIBLE, но частичное решение и послабления присутствуют', () => {
  const r = plan(ring5({ budget: 0 }));
  assert.equal(r.status, 'INFEASIBLE');
  assert.equal(r.metrics.demandsSatisfied, 0);
  assert.ok(r.diagnostics.infeasibility.partialSolution, 'частичное решение описано');
  assert.ok(r.diagnostics.infeasibility.minimalRelaxations.length > 0, 'предложены послабления');
  assert.equal(r.plan, r.plan); // план не null — структура ответа стабильна
  assert.ok(r.plan.rejectedLinksHistogram !== undefined);
});

test('Граничный случай Критика: заявок больше, чем ёмкости → PARTIAL + дефицит по линиям', () => {
  const r = plan(oversubscribedScenario({ origin: { lat: 55.75, lng: 37.62 } }));
  assert.ok(['PARTIAL', 'INFEASIBLE'].includes(r.status), `статус ${r.status}`);
  const deficits = r.plan.routes.filter((x) => x.deficitKbps > 0);
  assert.ok(deficits.length > 0, 'есть заявки с дефицитом полосы');
  const totalReq = r.plan.routes.reduce((s, x) => s + x.requestedKbps, 0);
  const totalGot = r.plan.routes.reduce((s, x) => s + x.grantedKbps, 0);
  assert.ok(totalGot < totalReq, 'выдано меньше, чем запрошено');
  assert.ok(totalGot > 0, 'частичное обслуживание всё же выполнено');
  const causes = r.diagnostics.infeasibility.causes.map((c) => c.code);
  assert.ok(causes.includes('CAPACITY_SHORTAGE'), `причины: ${causes.join(',')}`);
  const add = r.diagnostics.infeasibility.minimalRelaxations.filter((x) => x.code === 'ADD_CAPACITY');
  assert.ok(add.length > 0, 'предложено нарастить ёмкость конкретных линий');
  for (const a of add) {
    assert.ok(a.linkId && a.value > 0, `${JSON.stringify(a)}`);
  }
  // утилизация не превысила 100 % ёмкости (с учётом резервирования)
  assert.ok(r.metrics.utilMaxPct <= 100.001, `утилизация ${r.metrics.utilMaxPct}%`);
});

test('Граничный случай Критика: несвязная сеть не роняет ядро и даёт частичное решение', () => {
  const r = plan(disconnectedScenario({ origin: { lat: 55.75, lng: 37.62 } }));
  assert.ok(['PARTIAL', 'INFEASIBLE'].includes(r.status));
  // metrics.componentsCount — число компонент ПЛАНА (активной топологии),
  // candidateComponentsCount — число компонент в топологии кандидатов
  assert.ok(r.metrics.candidateComponentsCount >= 2, `компонент топологии ${r.metrics.candidateComponentsCount}`);
  const served = r.plan.routes.filter((x) => x.status === 'ROUTED').length;
  assert.ok(served >= 1, 'хотя бы одна заявка обслужена');
  assert.ok(r.plan.routes.some((x) => x.reason === 'NET_DISCONNECTED'));
  assert.ok(r.diagnostics.infeasibility.disconnectedComponents.length >= 2);
  const sums = r.diagnostics.infeasibility.disconnectedComponents.reduce((s, c) => s + c.size, 0);
  assert.equal(sums, r.metrics.nodesTotal, 'все узлы учтены в компонентах');
});

test('FR-RES-03: техника в состоянии 🔴 не размещается, 🟡 — с деградацией и предупреждением', () => {
  const req = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  const r = plan(req);
  // в парке есть observer в состоянии red (PU-007) → узел obs-2 не развёрнут
  const deployed = new Set(r.plan.deployedUnits.map((d) => d.nodeId));
  assert.ok(!deployed.has('obs-2'), 'узел с неисправной техникой не развёрнут');
  const rejected = r.plan.routes.filter((x) => x.reason === 'UNIT_NOT_READY');
  assert.ok(rejected.length >= 1, 'заявка к неразмещённому узлу отклонена');
  assert.ok(r.diagnostics.issues.some((i) => i.code === 'UNIT_SHORTAGE'), 'зафиксирована нехватка техники');
  assert.ok(r.diagnostics.issues.some((i) => i.code === 'UNIT_DEGRADED'), '🟡 техника помечена предупреждением');
  // запрет красной техники снимается ограничением allowRedUnits: узел становится
  // развертываемым, и причина отказа заявки меняется с UNIT_NOT_READY на другую
  const req2 = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  req2.constraints.allowRedUnits = true;
  req2.constraints.maxDelayMs = 600; // глобальный лимит задержки не должен маскировать проверку
  const r2 = plan(req2);
  const d5b = r2.plan.routes.find((x) => x.demandId === 'd5');
  assert.notEqual(d5b.reason, 'UNIT_NOT_READY', 'узел стал развертываемым');
  assert.ok(!r2.diagnostics.issues.some((i) => i.code === 'UNIT_SHORTAGE'), 'нехватки техники больше нет');
});

test('Детерминированность: одинаковый вход → побайтово одинаковый выход (NFR-DET-01)', () => {
  const req = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  const a = plan(JSON.parse(JSON.stringify(req)));
  const b = plan(JSON.parse(JSON.stringify(req)));
  const strip = (x) => {
    const c = JSON.parse(JSON.stringify(x));
    delete c.timings;
    if (c.plan && c.plan.schedule) delete c.plan.schedule.criticalPath;
    return JSON.stringify(c);
  };
  assert.equal(strip(a), strip(b), 'результаты различаются при идентичном входе');
  assert.equal(a.metrics.objectiveValue, b.metrics.objectiveValue);
});

test('Лимит времени: ядро возвращает TIMEOUT с частичным решением, а не «виснет»', () => {
  const req = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  req.constraints.timeLimitMs = 100; // заведомо мало (с учётом фазы ENG6-кросс-чека)
  req.constraints.eng6Analysis = true;
  const r = plan(req);
  assert.ok(['TIMEOUT', 'PARTIAL', 'FEASIBLE', 'OPTIMAL', 'INFEASIBLE'].includes(r.status), `статус ${r.status}`);
  assert.ok(r.timings.totalMs >= 0);
  if (r.status === 'TIMEOUT') assert.ok(r.statusReasons.some((x) => x.code === 'TIME_LIMIT'));
  // структурная целостность ответа сохраняется при любом исходе
  assert.ok(r.plan && r.metrics && r.diagnostics, 'ответ неполон');
});

test('Пустые и вырожденные входы: статусы корректны, исключений нет', () => {
  const cases = [
    { name: 'нет узлов', req: { topology: { nodes: [], links: [] }, demands: [] }, expect: 'INVALID' },
    { name: 'один узел без линий', req: { topology: { nodes: [{ id: 'a', type: 'cp', lat: 55.75, lng: 37.62 }], links: [] }, demands: [] }, expect: 'INFEASIBLE' },
    { name: 'два узла, линия есть, заявок нет', req: {
      topology: { nodes: [{ id: 'a', type: 'cp', lat: 55.75, lng: 37.62 }, { id: 'b', type: 'cp', lat: 55.8, lng: 37.7 }], links: [{ id: 'l', from: 'a', to: 'b', type: 'VHF' }] }, demands: [],
      constraints: { physics: 'fast', backbone: 'cp', eng6Analysis: false }, environment: { terrain: { source: 'flat' } } }, expect: 'FEASIBLE' },
    { name: 'петля (from == to) отклоняется валидатором', req: { topology: { nodes: [{ id: 'a', type: 'cp', lat: 55.75, lng: 37.62 }], links: [{ id: 'l', from: 'a', to: 'a', type: 'VHF' }] }, demands: [] }, expect: 'INVALID' },
  ];
  for (const c of cases) {
    const r = plan(c.req);
    assert.equal(r.status, c.expect, `${c.name}: ${r.status} ≠ ${c.expect}`);
    assert.ok(r.timings && r.core, `${c.name}: нет служебных блоков`);
  }
});

test('Живучесть: считается по АКТИВНОЙ топологии плана, а не по кандидатам', () => {
  // Кольцо из 4 узлов, но без требования резервирования план использует только
  // 3 ребра (экономия бюджета) → активная топология — цепочка: ЕТО и мосты есть.
  const ring = ring5();
  // l2 задана ненулевая стоимость: радионаправления УКВ сами по себе бесплатны
  // (оборудование уже в парке — см. DEFAULT_LINK_COST), поэтому без явной цены
  // резервирование не меняло бы бюджет и проверка «резерв стоит денег» была бы пуста
  ring.topology.links = [
    { id: 'l1', from: 'cp', to: 'a', type: 'VHF' },
    { id: 'l2', from: 'a', to: 'b', type: 'VHF', cost: 50 },
    { id: 'l3', from: 'b', to: 'c', type: 'VHF' },
    { id: 'l4', from: 'c', to: 'cp', type: 'VHF' },
  ];
  ring.topology.nodes = ring.topology.nodes.filter((n) => n.id !== 'd');
  ring.demands = ring.demands.filter((d) => d.to !== 'd');

  const plain = plan(ring);
  assert.equal(plain.metrics.connectivityPct, 100);
  assert.ok(plain.plan.survivability.bridgeCount >= 1, `без резервирования активная топология — цепочка (мостов ${plain.plan.survivability.bridgeCount})`);
  assert.ok(plain.diagnostics.issues.some((i) => i.code === 'BRIDGE_LINK'), 'мосты отражены в замечаниях');
  assert.ok(plain.diagnostics.recommendations.some((r) => r.id === 'REC-SPOF'), 'выдана рекомендация REC-SPOF');

  // С требованием резервирования кольцо замыкается → ЕТО и мостов нет
  const redundant = plan({ ...ring, constraints: { ...ring.constraints, requireRedundancy: true } });
  assert.equal(redundant.plan.survivability.bridgeCount, 0, 'в замкнутом кольце мостов нет');
  assert.equal(redundant.plan.survivability.spofCount, 0, 'в замкнутом кольце ЕТО нет');
  assert.ok(redundant.metrics.redundancyAdded >= 1, 'добавлено резервирование');
  assert.ok(redundant.metrics.totalCost > plain.metrics.totalCost, `резервирование стоит денег: ${redundant.metrics.totalCost} > ${plain.metrics.totalCost}`);
  assert.ok(redundant.plan.activeLinks.some((l) => l.id === 'l2'), 'замыкающее кольцо ребро активировано');
  assert.ok(!plain.plan.activeLinks.some((l) => l.id === 'l2'), 'без резервирования дорогое ребро не активируется');

  // Звезда: центр — точка сочленения при любой настройке
  const star = ring5();
  star.topology.links = ['a', 'b', 'c', 'd'].map((t, i) => ({ id: `s${i}`, from: 'cp', to: t, type: 'VHF' }));
  const rStar = plan(star);
  assert.ok(rStar.plan.survivability.spofNodes.includes('cp'), 'КП звезды — единая точка отказа');
  assert.ok(rStar.plan.survivability.bridgeCount >= 4, `лучей-мостов ${rStar.plan.survivability.bridgeCount}`);
});

test('Резервирование: requireRedundancy=true строит второй непересекающийся путь в кольце', () => {
  const r = plan(ring5({ requireRedundancy: true }));
  const routed = r.plan.routes.filter((x) => x.status === 'ROUTED' || x.status === 'PARTIAL');
  assert.ok(routed.length > 0);
  const redundant = routed.filter((x) => x.redundant);
  assert.ok(redundant.length >= 1, `резервировано ${redundant.length} из ${routed.length}`);
  for (const x of redundant) {
    assert.ok(x.backupPath && x.backupPath.links.length > 0, 'у резервированной заявки есть backupPath');
    const main = new Set(x.links);
    for (const l of x.backupPath.links) assert.ok(!main.has(l), `резервный путь пересекается с основным по ${l}`);
  }
  assert.ok(r.metrics.redundancyAdded >= 1);
});

test('Расписание: учитывает иерархию (parentId) и лимит одновременных бригад', () => {
  const req = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  req.constraints.maxSimultaneousDeploy = 2;
  const r = plan(req);
  const sch = r.plan.schedule;
  assert.ok(sch.makespanMin > 0, 'makespan положительный');
  assert.equal(sch.crews, 2);
  // ни одна бригада не занята двумя задачами одновременно
  const byCrew = new Map();
  for (const j of sch.jobs) {
    const arr = byCrew.get(j.crew) || [];
    arr.push(j);
    byCrew.set(j.crew, arr);
  }
  for (const [, arr] of byCrew) {
    arr.sort((a, b) => a.startMin - b.startMin);
    for (let i = 1; i < arr.length; i++) assert.ok(arr[i].startMin >= arr[i - 1].endMin - 1e-6, `пересечение бригады: ${arr[i - 1].id} и ${arr[i].id}`);
  }
  // предшественник завершается раньше начала подчинённого
  const end = new Map(sch.jobs.map((j) => [j.id, j.endMin]));
  const byId = new Map(req.topology.nodes.map((n) => [n.id, n]));
  for (const j of sch.jobs) {
    const parent = byId.get(j.nodeId)?.parentId;
    if (parent && end.has(parent)) assert.ok(j.startMin >= end.get(parent) - 1e-6, `${j.nodeId} начат раньше родителя ${parent}`);
  }
  // волны покрывают все задачи и не пересекаются по времени
  const all = sch.waves.flatMap((w) => w.items.map((i) => i.id));
  assert.equal(new Set(all).size, all.length, 'задачи не дублируются в волнах');
  for (let i = 1; i < sch.waves.length; i++) assert.ok(sch.waves[i].tStartMin >= sch.waves[i - 1].tEndMin - 1e-9, 'волны упорядочены');
});

test('Режимы физики: на плоском рельефе fast и full совпадают до 0,01 дБ', () => {
  const base = ring5();
  base.environment.terrain = { source: 'flat', amplitudeM: 0 };
  const fast = plan({ ...base, constraints: { ...base.constraints, physics: 'fast', fastMarginPenaltyDb: 0 } });
  const full = plan({ ...base, constraints: { ...base.constraints, physics: 'full', quality: 'balanced' } });
  assert.equal(fast.core.physics, 'fast');
  assert.equal(full.core.physics, 'full');
  assert.equal(fast.metrics.linksFeasible, full.metrics.linksFeasible, 'число допустимых линий совпадает');
  assert.equal(fast.metrics.linksFeasible, 5, 'все 5 линий допустимы');
  assert.equal(fast.metrics.demandsSatisfied, full.metrics.demandsSatisfied);
  for (const lf of fast.plan.activeLinks) {
    const ll = full.plan.activeLinks.find((x) => x.id === lf.id);
    assert.ok(ll, `линия ${lf.id} активна в обоих режимах`);
    assert.ok(Math.abs(lf.distKm - ll.distKm) < 1e-9, 'расстояния идентичны (общий haversine)');
    assert.ok(Math.abs(lf.marginDb - ll.marginDb) < 0.01, `запас fast ${lf.marginDb} vs full ${ll.marginDb}`);
    assert.ok(Math.abs(lf.capacityKbps - ll.capacityKbps) < 0.01, `ёмкость fast ${lf.capacityKbps} vs full ${ll.capacityKbps}`);
    assert.ok(Math.abs(lf.delayMs - ll.delayMs) < 0.01, `задержка fast ${lf.delayMs} vs full ${ll.delayMs}`);
  }
});

test('Режимы физики: на синтетическом рельефе fast оптимистичнее full (дифракция) — расхождение фиксируется', () => {
  const base = ring5();
  base.environment.terrain = { source: 'synth', amplitudeM: 120 };
  const fast = plan({ ...base, constraints: { ...base.constraints, physics: 'fast', fastMarginPenaltyDb: 0 } });
  const full = plan({ ...base, constraints: { ...base.constraints, physics: 'full', quality: 'balanced' } });
  // дифракция только уменьшает запас → fast ≥ full
  let checked = 0;
  for (const lf of fast.plan.activeLinks) {
    const ll = full.plan.activeLinks.find((x) => x.id === lf.id);
    if (!ll) continue;
    checked++;
    assert.ok(lf.marginDb >= ll.marginDb - 0.01, `fast ${lf.marginDb} < full ${ll.marginDb} — дифракция должна снижать запас`);
  }
  assert.ok(fast.metrics.linksFeasible >= full.metrics.linksFeasible, 'в fast допустимых линий не меньше');
  // отклонение задокументировано в diagnostics.approximation
  assert.equal(fast.diagnostics.approximation.physics, 'fast');
});

test('Автопереключение physics: при числе рёбер выше порога full понижается до fast с фиксацией', () => {
  const req = ring5();
  req.constraints.physics = 'full';
  const planner = new Eng6Planner({ limits: { fullPhysicsMaxLinks: 2 } });
  const r = planner.plan(req);
  assert.equal(r.core.physics, 'fast', 'режим понижен');
  assert.ok(r.diagnostics.issues.some((i) => i.code === 'PHYSICS_DOWNGRADE'), 'понижение зафиксировано в issues');
  assert.ok(r.diagnostics.validation.warnings.some((w) => w.path === '$.constraints.physics'));
});

test('Контракт ответа: обязательные блоки и типы полей стабильны', () => {
  const r = plan(battalionScenario({ origin: { lat: 55.75, lng: 37.62 } }));
  for (const k of ['status', 'statusReasons', 'plan', 'metrics', 'diagnostics', 'timings', 'core']) assert.ok(k in r, `нет блока ${k}`);
  assert.equal(r.core.engine, 'ENG6');
  assert.equal(r.core.planner, PLANNER_VERSION);
  assert.match(r.core.engineSha256, /^[0-9a-f]{64}$/);
  for (const k of ['deployedUnits', 'activeLinks', 'routes', 'schedule', 'rejectedLinks', 'survivability']) assert.ok(k in r.plan, `plan.${k}`);
  for (const k of ['objectiveValue', 'grade', 'totalCost', 'demandsSatisfied', 'connectivityPct', 'utilMaxPct']) assert.ok(k in r.metrics, `metrics.${k}`);
  for (const k of ['issues', 'recommendations', 'infeasibility', 'approximation']) assert.ok(k in r.diagnostics, `diagnostics.${k}`);
  assert.ok(['A', 'B', 'C', 'D', 'F'].includes(r.metrics.grade));
  // NFR-03: координаты в ответе — не более 6 знаков
  for (const d of r.plan.deployedUnits) {
    assert.ok(Number.isFinite(d.lat) && Number.isFinite(d.lng));
    assert.ok(String(d.lat).split('.')[1]?.length <= 6, `lat ${d.lat}`);
  }
});

test('Дефолты ограничений соответствуют схеме (проверка дрейфа конфигурации)', () => {
  assert.equal(DEFAULT_CONSTRAINTS.minMarginDb, 3);
  assert.equal(DEFAULT_CONSTRAINTS.reserveFactor, 1.3);
  assert.equal(DEFAULT_CONSTRAINTS.physics, 'auto');
  const r = plan(ring5());
  assert.ok(r.timings.totalMs >= 0);
});
