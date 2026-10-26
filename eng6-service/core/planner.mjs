/**
 * core/planner.mjs — ENG6-PLAN: вычислительное ядро планировщика (ЭТАП 2).
 *
 * Контракт «Айтишника» выполнен буквально: это ЧИСТАЯ ФУНКЦИЯ
 *     plan(request) → response
 * без БД, сети, файловой системы, часов реального времени в логике принятия
 * решений и глобального состояния. Единственная внешняя зависимость —
 * вендорное математическое ядро ENG6 (core/vendor/eng6_core.js), которое
 * исполняется в изолированном V8-контексте (core/eng6.mjs).
 *
 * Последовательность фаз (полное описание — docs/01_MATH_MODEL.md):
 *   0  валидация и нормализация               → INVALID
 *   1  парк техники → развертываемость узлов   → UNIT_SHORTAGE
 *   2  таблица рёбер-кандидатов, стоимости
 *   3  физический уровень (ENG6, fast|full)   → отбраковка недопустимых рёбер
 *   4  допустимый граф (CSR)
 *   5  связность, терминалы, остов Штейнера   → NET_DISCONNECTED, LB
 *   6  многопродуктовая маршрутизация заявок под бюджетом
 *   7  резервирование (мосты активного подграфа)
 *   8  живучесть (SPOF, min-cut)
 *   9  расписание развертывания (волны)
 *  10  метрики, статус, диагностика, рекомендации
 *  11  перекрёстная проверка плана полным ядром ENG6 (для малых экземпляров)
 *
 * Любая фаза проверяет дедлайн (constraints.timeLimitMs): при исчерпании
 * возвращается status=TIMEOUT с честным частичным решением, а не «падением».
 */
import { loadEng6, conformance } from './eng6.mjs';
import { validateRequest } from './validate.mjs';
import {
  LIMITS,
  DEFAULT_CONSTRAINTS,
  DEFAULT_WEIGHTS,
  DEFAULT_LINK_COST,
  DEFAULT_LINK_COST_PER_KM,
  DEFAULT_NODE_COST,
  DEFAULT_NODE_DEPLOY_MIN,
  objective,
  normalizeWeights,
} from './schema.mjs';
import {
  EdgeTable,
  NodeTable,
  LINK_TYPE_IX,
  MOD_IX,
  FEC_IX,
  REJECT,
  REJECT_IX,
  REJECT_TEXT,
  computeEdgesFast,
  computeEdgesFull,
  normalizeNodeParams,
  haversineKm,
} from './physics.mjs';
import { normalizeEnvironment, buildScenario, eng6Validate } from './scenario.mjs';
import { CsrGraph, components, bridgesAndArticulations, steinerApprox, trivialLowerBound, maxFlowUndirected } from './graph/index.mjs';
import { dijkstra, pathFromPrev } from './graph/index.mjs';
import { Router } from './route.mjs';
import { knapsack01 } from './knapsack.mjs';
import { buildSchedule, criticalPath } from './schedule.mjs';
import { round, round6, clamp, nowMs } from './util.mjs';

/** Версия контракта ядра. */
export const PLANNER_VERSION = '1.0.0';
/** Максимум объектов в ответе (защита от гигантских JSON на 10^5 узлов). */
const OUT_LIMITS = Object.freeze({ links: 5000, routes: 2000, nodes: 5000, rejectedLinks: 500, issues: 200, spof: 200, scheduleJobs: 2000 });

const CONDITION_RANK = { green: 0, yellow: 1, red: 2 };

/**
 * Класс-фасад ядра (требование ЭТАПА 2: «написать класс/модуль Eng6Planner»).
 * Не хранит состояния между вызовами — экземпляр нужен только для
 * конфигурации лимитов и инъекции часов в тестах.
 */
export class Eng6Planner {
  constructor({ limits = {}, clock = nowMs, core = null } = {}) {
    this.limits = { ...LIMITS, ...limits };
    this.clock = clock;
    this.core = core || loadEng6();
  }

  /** Описание публичной поверхности ядра (для /api/v1/meta и документации). */
  static describe(core = loadEng6()) {
    return {
      planner: PLANNER_VERSION,
      engine: { name: 'ENG6', version: core.ENG6.version, keys: core.keys.length, sha256: core.sha256, chars: core.chars, meta: core.meta },
      limits: LIMITS,
      outLimits: OUT_LIMITS,
      statuses: ['OPTIMAL', 'FEASIBLE', 'PARTIAL', 'INFEASIBLE', 'INVALID', 'ERROR', 'TIMEOUT'],
      physics: { modes: ['fast', 'full'], autoSwitchLinks: LIMITS.fullPhysicsMaxLinks },
    };
  }

  /** @param {object} request @returns {object} response */
  plan(request) {
    return plan(request, { limits: this.limits, clock: this.clock, core: this.core });
  }
}

/**
 * Точка входа ядра. Чистая функция: вход → математика → выход.
 * @param {object} request
 * @param {{limits?:object, clock?:function, core?:object}} [cfg]
 */
export function plan(request, cfg = {}) {
  const clock = cfg.clock || nowMs;
  const limits = cfg.limits || LIMITS;
  const t0 = clock();
  const timings = { validateMs: 0, fleetMs: 0, physicsMs: 0, graphMs: 0, backboneMs: 0, routingMs: 0, redundancyMs: 0, survivabilityMs: 0, scheduleMs: 0, metricsMs: 0, eng6Ms: 0, totalMs: 0 };
  const core = cfg.core || loadEng6();
  const E = core.ENG6;
  const meta = { engine: 'ENG6', engineVersion: E.version, engineSha256: core.sha256, planner: PLANNER_VERSION, physics: 'fast' };

  const base = {
    status: 'ERROR',
    statusReasons: [],
    plan: null,
    metrics: null,
    diagnostics: null,
    timings,
    core: meta,
  };

  // ── ФАЗА 0. Валидация ────────────────────────────────────────────────────
  let mark = clock();
  let report;
  try {
    report = validateRequest(request);
  } catch (err) {
    return { ...base, status: 'ERROR', statusReasons: [{ code: 'VALIDATOR_CRASH', message: String((err && err.message) || err) }], timings: { ...timings, totalMs: clock() - t0 } };
  }
  timings.validateMs = round(clock() - mark, 3);
  if (!report.ok) {
    return {
      ...base,
      status: 'INVALID',
      statusReasons: report.errors.slice(0, 50).map((e) => ({ code: e.code, path: e.path, message: e.message })),
      diagnostics: { validation: report.toJSON(), issues: [], recommendations: [], infeasibility: null },
      timings: { ...timings, totalMs: round(clock() - t0, 3) },
    };
  }

  const C = { ...DEFAULT_CONSTRAINTS, ...(request.constraints || {}) };
  const weights = normalizeWeights(request.weights);
  const env = normalizeEnvironment(request.environment, report);
  const deadline = t0 + Math.min(C.timeLimitMs || DEFAULT_CONSTRAINTS.timeLimitMs, limits.maxTimeLimitMs);
  const out = { ...base, timings, core: meta };
  const issues = [];
  const recommendations = [];
  let timedOut = false;
  const checkDeadline = () => {
    if (!timedOut && clock() > deadline) timedOut = true;
    return timedOut;
  };

  try {
    // ── ФАЗА 1. Узлы и парк техники ───────────────────────────────────────
    mark = clock();
    const nodes = request.topology.nodes;
    const n = nodes.length;
    const nodeIndex = new Map();
    for (let i = 0; i < n; i++) nodeIndex.set(nodes[i].id, i);

    const nt = new NodeTable(n);
    const nodeTypeIx = new Uint8Array(n);
    const NODE_TYPE_IDS = ['cp', 'relay', 'radio', 'sat', 'observer', 'ew_jam', 'ew_rlr', 'ew_rer'];
    for (let i = 0; i < n; i++) {
      const nd = nodes[i];
      const p = normalizeNodeParams(nd, E);
      nt.lat[i] = round6(nd.lat);
      nt.lng[i] = round6(nd.lng);
      nt.txPowerDbm[i] = p.txPowerDbm;
      nt.txGainDbi[i] = p.txGainDbi;
      nt.rxGainDbi[i] = p.rxGainDbi;
      nt.rxSensDbm[i] = p.rxSensDbm;
      nt.antHeightM[i] = p.antHeightM;
      nt.feederLossDb[i] = p.feederLossDb;
      nt.noiseFigureDb[i] = p.noiseFigureDb;
      nt.typeIdx[i] = NODE_TYPE_IDS.indexOf(nd.type);
      nt.ready[i] = nd.status === 'failed' ? 0 : 1;
      nt.deployMin[i] = nd.deployTimeMin ?? DEFAULT_NODE_DEPLOY_MIN[nd.type] ?? 20;
      nt.cost[i] = nd.costDeploy ?? DEFAULT_NODE_COST[nd.type] ?? 10;
    }

    const fleet = assignFleet(nodes, request.fleet, nt, C, nodeIndex, issues);
    timings.fleetMs = round(clock() - mark, 3);

    // ── ФАЗА 2. Рёбра-кандидаты ──────────────────────────────────────────
    mark = clock();
    const links = request.topology.links || [];
    const m = links.length;
    const table = new EdgeTable(m);
    const linkOrigin = new Array(m);
    const forbid = new Set(C.forbidLinkTypes || []);
    let unknownRef = 0;
    for (let e = 0; e < m; e++) {
      const l = links[e];
      linkOrigin[e] = l;
      const u = nodeIndex.get(l.from);
      const v = nodeIndex.get(l.to);
      if (u === undefined || v === undefined) {
        table.rejectCode[e] = REJECT_IX.NODE_NOT_FOUND;
        unknownRef++;
        continue;
      }
      table.from[e] = u;
      table.to[e] = v;
      const tix = LINK_TYPE_IX[l.type];
      if (tix === undefined) {
        table.rejectCode[e] = REJECT_IX.UNKNOWN_LINK_TYPE;
        continue;
      }
      table.typeIdx[e] = tix;
      // дефолты — из таблицы LINK_TYPES ENG6 (семантика LINK_PRESETS оригинала):
      // без этого РРЛ/СПУТНИК считались бы с полосой 25 кГц и ёмкость падала в 100 раз
      const lt = E.LINK_TYPES[l.type] || {};
      table.modIdx[e] = MOD_IX[l.mod || lt.mod || 'QPSK'] ?? MOD_IX.QPSK;
      table.fecIdx[e] = FEC_IX[l.fec || lt.fec || 'none'] ?? 0;
      table.freqMhz[e] = typeof l.freqMhz === 'number' && l.freqMhz > 0 ? l.freqMhz : lt.freq || 0;
      table.bwKhz[e] = typeof l.bwKhz === 'number' && l.bwKhz > 0 ? l.bwKhz : lt.bw || 25;
      if (forbid.has(l.type)) table.rejectCode[e] = REJECT_IX.FORBIDDEN_TYPE;
      if (!fleet.deployable[u] || !fleet.deployable[v]) {
        if (table.rejectCode[e] === 0) table.rejectCode[e] = REJECT_IX.UNIT_NOT_READY;
      }
    }

    // ── ФАЗА 3. Физический уровень ENG6 ──────────────────────────────────
    const physicsMode = resolvePhysicsMode(C.physics, m, limits, report, issues);
    meta.physics = physicsMode;
    mark = clock();
    let scenario = null;
    if (physicsMode === 'full') {
      const built = buildScenario({ nodes, links, environment: env, threats: request.threats || [], meta: request.meta || {}, nodeIndex, quality: C.quality });
      scenario = built.scenario;
      const eng6Issues = eng6Validate(scenario);
      for (const s of eng6Issues) report.warn('ENG6', '$.scenario', `Замечание валидатора ENG6: ${s}`);
      computeEdgesFull({ table, scenario, linkIndex: built.linkIndex, opts: { quality: C.quality, miscLossDb: C.miscLossDb, minMarginDb: C.minMarginDb, maxDelayMs: C.maxDelayMs, requireLosForRrl: C.requireLosForRrl } });
    } else {
      computeEdgesFast({ table, nodes: nt, env, opts: { fastMarginPenaltyDb: C.fastMarginPenaltyDb, minMarginDb: C.minMarginDb, maxDelayMs: C.maxDelayMs, miscLossDb: C.miscLossDb } });
    }
    // стоимость активации ребра (если не задана явно) и ёмкость с резервом
    for (let e = 0; e < m; e++) {
      const l = linkOrigin[e];
      const typeName = Object.keys(LINK_TYPE_IX)[table.typeIdx[e]] || 'VHF';
      const d = table.distKm[e] || 0;
      table.cost[e] = typeof l.cost === 'number' && Number.isFinite(l.cost) ? l.cost : (DEFAULT_LINK_COST[typeName] || 0) + (DEFAULT_LINK_COST_PER_KM[typeName] || 0) * d;
      table.capKbps[e] = table.capKbps[e] / Math.max(1, C.reserveFactor || 1);
    }
    timings.physicsMs = round(clock() - mark, 3);
    if (checkDeadline()) return timeoutResult(out, { issues, recommendations, report, timedOut, timings, t0, clock, phase: 'physics' });

    // ── ФАЗА 4. Допустимый граф ──────────────────────────────────────────
    mark = clock();
    const feas = [];
    const rejectHist = Object.fromEntries(REJECT.map((r) => [r, 0]));
    for (let e = 0; e < m; e++) {
      if (table.feasible[e] && table.rejectCode[e] === 0) feas.push(e);
      else rejectHist[table.rejectName(e)] = (rejectHist[table.rejectName(e)] || 0) + 1;
    }
    const gFrom = new Int32Array(feas.length);
    const gTo = new Int32Array(feas.length);
    const gIds = new Int32Array(feas.length);
    for (let i = 0; i < feas.length; i++) {
      const e = feas[i];
      gFrom[i] = table.from[e];
      gTo[i] = table.to[e];
      gIds[i] = e;
    }
    const g = new CsrGraph(n, gFrom, gTo, null, false, gIds);
    const orig = g.edgeIds; // внутренний индекс → индекс в table
    const ms = g.m;
    const delayMs = new Float64Array(ms);
    const capKbps = new Float64Array(ms);
    const marginDb = new Float64Array(ms);
    const availability = new Float64Array(ms);
    const edgeCost = new Float64Array(ms);
    for (let i = 0; i < ms; i++) {
      const e = orig[i];
      delayMs[i] = table.delayMs[e];
      capKbps[i] = table.capKbps[e] > 0 ? table.capKbps[e] : 0;
      marginDb[i] = table.marginDb[e];
      availability[i] = table.availability[e];
      edgeCost[i] = table.cost[e];
    }
    const edgeActive = new Uint8Array(ms);
    const nodeActive = new Uint8Array(n);
    const nodeCostArr = Float64Array.from(nt.cost);
    timings.graphMs = round(clock() - mark, 3);

    // ── ФАЗА 5. Связность, терминалы, остов ──────────────────────────────
    mark = clock();
    const demands = request.demands || [];
    const demandIx = new Int32Array(demands.length);
    for (let k = 0; k < demands.length; k++) demandIx[k] = k;
    const demandSrc = new Int32Array(demands.length);
    const demandDst = new Int32Array(demands.length);
    let demandRefBroken = 0;
    for (let k = 0; k < demands.length; k++) {
      const d = demands[k];
      const s = nodeIndex.get(d.from);
      const t = nodeIndex.get(d.to);
      demandSrc[k] = s === undefined ? -1 : s;
      demandDst[k] = t === undefined ? -1 : t;
      if (s === undefined || t === undefined) demandRefBroken++;
    }

    const comps = components(g);
    const connPctAll = connectivityOf(g, comps, null);
    const cpNodes = [];
    for (let i = 0; i < n; i++) if (nt.typeIdx[i] === 0 && fleet.deployable[i]) cpNodes.push(i);
    const terminals = new Set(cpNodes);
    for (let k = 0; k < demands.length; k++) {
      if (demandSrc[k] >= 0) terminals.add(demandSrc[k]);
      if (demandDst[k] >= 0) terminals.add(demandDst[k]);
    }
    const termArr = [...terminals];

    // нижняя оценка стоимости связывания терминалов (для честного OPTIMAL)
    const lb = ms > 0 && termArr.length > 1 ? trivialLowerBound(g, termArr, edgeCost) : 0;

    let backbone = { edges: new Int32Array(0), totalWeight: 0, lowerBound: 0, mode: 'none', unreachable: [] };
    const backboneMode = C.backbone || 'cp';
    const backboneTerminals = backboneMode === 'terminals' ? termArr : cpNodes;
    let spent = 0;
    const budget = C.budget === null || C.budget === undefined ? Infinity : Number(C.budget);
    const budgetLimited = Number.isFinite(budget);
    const budgetBlocked = [];

    if (ms > 0 && backboneMode !== 'none' && backboneTerminals.length > 1 && !checkDeadline()) {
      backbone = steinerApprox(g, backboneTerminals, { weight: edgeCost, maxTerminals: limits.steinerExactMaxTerminals });
      const bbNodes = new Set();
      let bbCost = backbone.totalWeight;
      for (const e of backbone.edges) {
        bbNodes.add(g.edgeFrom[e]);
        bbNodes.add(g.edgeTo[e]);
      }
      for (const v of bbNodes) if (!nodeActive[v]) bbCost += nodeCostArr[v];
      if (!budgetLimited || bbCost <= budget) {
        for (const e of backbone.edges) {
          if (!edgeActive[e]) {
            edgeActive[e] = 1;
            spent += edgeCost[e];
          }
        }
        for (const v of bbNodes) {
          if (!nodeActive[v]) {
            nodeActive[v] = 1;
            spent += nodeCostArr[v];
          }
        }
      } else {
        issues.push({ sev: 'crit', code: 'BACKBONE_OVER_BUDGET', obj: { kind: 'net' }, title: 'Каркас связности дороже бюджета', text: `Требуется ${round(bbCost, 1)} у.е., бюджет ${round(budget, 1)} у.е. Каркас не активирован — заявки маршрутизируются по остаточному бюджету.` });
        budgetBlocked.push({ code: 'RAISE_BUDGET', value: round(bbCost, 2), reason: `минимальный каркас связности КП (${backboneTerminals.length} терминалов)` });
      }
      if (backbone.unreachable && backbone.unreachable.length) {
        issues.push({ sev: 'crit', code: 'BACKBONE_UNREACHABLE', obj: { kind: 'net' }, title: 'Часть терминалов недостижима', text: `Недостижимых терминалов: ${backbone.unreachable.length}. Требуется установка дополнительных ретрансляторов.` });
      }
    }
    timings.backboneMs = round(clock() - mark, 3);

    // ── ФАЗА 6. Маршрутизация заявок под бюджетом ────────────────────────
    mark = clock();
    const router = new Router(g, {
      delayMs,
      capKbps,
      marginDb,
      availability,
      cost: edgeCost,
      nodeCost: nodeCostArr,
      edgeActive,
      nodeActive,
      hopProcessingMs: C.hopProcessingMs ?? DEFAULT_CONSTRAINTS.hopProcessingMs,
      frameKbit: C.frameKbit ?? DEFAULT_CONSTRAINTS.frameKbit,
      utilPenaltyMs: C.utilPenaltyMs ?? 10,
    });
    // обязательные рёбра каркаса уже «заняты» нулевой нагрузкой — учтём в active

    const order = demandIx.length
      // приоритет 1 = наивысший (см. docs/01_MATH_MODEL.md, п. 2.3); тай-брейк —
      // бо́льшая полоса, затем лексикографически id (детерминированность)
      ? [...demandIx].sort((a, b) => (demands[a].priority ?? 3) - (demands[b].priority ?? 3) || (demands[b].rateKbps || 0) - (demands[a].rateKbps || 0) || String(demands[a].id).localeCompare(String(demands[b].id)))
      : [];
    const routeResults = new Array(demands.length);
    const relaxation = { delayMs: Infinity, budget: Infinity, marginDb: -Infinity, capacity: [] };
    let satisfied = 0;
    let partialCount = 0;
    let rejectedCount = 0;
    let grantedTotal = 0;
    let demandTotal = 0;
    /* значения пересчитываются после фазы 6.5 — см. recountRoutes */

    for (const k of order) {
      if (checkDeadline()) {
        for (const kk of order) if (!routeResults[kk]) routeResults[kk] = { status: 'NOT_PROCESSED', reason: 'TIMEOUT' };
        break;
      }
      const d = demands[k];
      const rate = d.rateKbps || 0;
      demandTotal += rate;
      const s = demandSrc[k];
      const t = demandDst[k];
      const maxDelayMs = Math.min(d.maxDelayMs ?? C.maxDelayMs, C.maxDelayMs);
      const maxHops = Math.min(d.maxHops ?? C.maxHops, C.maxHops);
      if (s < 0 || t < 0) {
        routeResults[k] = { status: 'REJECTED', reason: 'NODE_NOT_FOUND', grantedKbps: 0, deficitKbps: rate };
        rejectedCount++;
        continue;
      }
      if (!fleet.deployable[s] || !fleet.deployable[t]) {
        routeResults[k] = { status: 'REJECTED', reason: 'UNIT_NOT_READY', grantedKbps: 0, deficitKbps: rate, nodes: [s, t] };
        rejectedCount++;
        continue;
      }
      if (comps.comp[s] !== comps.comp[t]) {
        routeResults[k] = { status: 'REJECTED', reason: 'NET_DISCONNECTED', grantedKbps: 0, deficitKbps: rate, components: [comps.comp[s], comps.comp[t]] };
        rejectedCount++;
        continue;
      }
      const r = router.route(s, t, rate, { maxDelayMs, maxHops });
      if (r.rejected) {
        routeResults[k] = { status: 'REJECTED', reason: r.reason, grantedKbps: 0, deficitKbps: rate, bestEffort: r.bestEffort || null, delayMs: Number.isFinite(r.delayMs) ? round(r.delayMs, 2) : null, hops: r.hops };
        rejectedCount++;
        if (r.reason === 'DELAY_EXCEEDED' || r.reason === 'HOP_LIMIT') {
          const be = r.bestEffort || (Number.isFinite(r.delayMs) ? { delayMs: r.delayMs } : null);
          if (be && Number.isFinite(be.delayMs) && be.delayMs < relaxation.delayMs) relaxation.delayMs = be.delayMs;
        }
        if (r.reason === 'CAPACITY_SHORTAGE' && r.bestEffort && r.bestEffort.bottleneckEdge >= 0) {
          const be = r.bestEffort.bottleneckEdge;
          relaxation.capacity.push({ edge: orig[be], linkId: linkOrigin[orig[be]]?.id, deficitKbps: round(r.bestEffort.deficitKbps || rate, 3) });
        }
        continue;
      }
      // бюджет: маржинальная стоимость маршрута
      const marginal = router.marginalCost(r.nodes, r.edges);
      if (budgetLimited && spent + marginal > budget + 1e-9) {
        const need = spent + marginal;
        if (need < relaxation.budget) relaxation.budget = need;
        budgetBlocked.push({ code: 'RAISE_BUDGET', value: round(need, 2), reason: `заявка ${d.id} (приоритет ${d.priority ?? 3})` });
        routeResults[k] = { status: 'REJECTED', reason: 'BUDGET_EXCEEDED', grantedKbps: 0, deficitKbps: rate, costNeeded: round(marginal, 2), budgetLeft: round(Math.max(0, budget - spent), 2) };
        rejectedCount++;
        continue;
      }
      router.commit(r.nodes, r.edges, r.grantedKbps);
      spent += marginal;
      const full = r.grantedKbps >= rate - 1e-9;
      grantedTotal += r.grantedKbps;
      if (full) satisfied++;
      else partialCount++;
      routeResults[k] = {
        status: full ? 'ROUTED' : 'PARTIAL',
        reason: full ? 'OK' : 'CAPACITY_SHORTAGE',
        grantedKbps: round(r.grantedKbps, 3),
        deficitKbps: round(Math.max(0, rate - r.grantedKbps), 3),
        delayMs: round(r.delayMs, 3),
        hops: r.hops,
        marginDb: round(r.marginDb, 2),
        availabilityPct: round(r.availability * 100, 3),
        utilMaxPct: round(r.utilMaxPct, 1),
        pathNodes: r.nodes,
        pathEdges: r.edges,
        cost: round(marginal, 2),
        protected: !!d.protected,
      };
      if (!full && r.bottleneckEdge >= 0) {
        const oe = orig[r.bottleneckEdge];
        relaxation.capacity.push({ edge: oe, linkId: linkOrigin[oe]?.id, deficitKbps: round(rate - r.grantedKbps, 3) });
      }
    }
    timings.routingMs = round(clock() - mark, 3);

    // ── ФАЗА 6.5. Улучшения за остаток бюджета (0/1-рюкзак) ───────────────
    mark = clock();
    const improvements = planImprovements({
      g, router, capKbps, edgeCost, edgeActive, demands, routeResults, order,
      spent, budget, budgetLimited, C, issues, orig, linkOrigin, checkDeadline,
      nodeIndexGet: (id) => (nodeIndex.has(id) ? nodeIndex.get(id) : -1),
    });
    spent += improvements.spent;
    {
      const rc = recountRoutes(routeResults);
      satisfied = rc.satisfied;
      partialCount = rc.partial;
      rejectedCount = rc.rejected;
      grantedTotal = 0;
      for (const r of routeResults) if (r && r.grantedKbps) grantedTotal += r.grantedKbps;
      grantedTotal = round(grantedTotal, 3);
    }
    timings.improveMs = round(clock() - mark, 3);

    // ── ФАЗА 7. Резервирование защищённых заявок ─────────────────────────
    mark = clock();
    let redundancyAdded = 0;
    let redundancySkipped = 0;
    const needRedundancy = !!C.requireRedundancy;
    if (!checkDeadline()) {
      for (const k of order) {
        const rr = routeResults[k];
        if (!rr || rr.status === 'NOT_PROCESSED') continue;
        const d = demands[k];
        const wantProtected = needRedundancy || !!d.protected;
        if (!wantProtected) {
          rr.redundant = null;
          continue;
        }
        if (rr.status !== 'ROUTED' && rr.status !== 'PARTIAL') {
          rr.redundant = false;
          continue;
        }
        const alt = findBackupPath(g, router, rr.pathEdges, demandSrc[k], demandDst[k], { maxDelayMs: Math.min(d.maxDelayMs ?? C.maxDelayMs, C.maxDelayMs) * (C.backupDelayFactor ?? 2), hopProcessingMs: C.hopProcessingMs ?? 2 });
        if (!alt) {
          rr.redundant = false;
          redundancySkipped++;
          issues.push({ sev: 'warn', code: 'NO_BACKUP_PATH', obj: { kind: 'demand', id: d.id }, title: `Нет резервного маршрута: ${d.id}`, text: 'Альтернативный реберно-непересекающийся путь не найден в пределах допустимой задержки.' });
          continue;
        }
        const marginal = router.marginalCost(alt.nodes, alt.edges);
        if (budgetLimited && spent + marginal > budget + 1e-9) {
          rr.redundant = false;
          redundancySkipped++;
          budgetBlocked.push({ code: 'RAISE_BUDGET', value: round(spent + marginal, 2), reason: `резервирование заявки ${d.id}` });
          issues.push({ sev: 'warn', code: 'BACKUP_OVER_BUDGET', obj: { kind: 'demand', id: d.id }, title: `Резервирование не по бюджету: ${d.id}`, text: `Требуется ещё ${round(marginal, 2)} у.е.` });
          continue;
        }
        router.commit(alt.nodes, alt.edges, 0);
        spent += marginal;
        rr.redundant = true;
        rr.backupPath = { nodes: alt.nodes, edges: alt.edges, delayMs: round(alt.delayMs, 2), cost: round(marginal, 2) };
        redundancyAdded++;
        if (checkDeadline()) break;
      }
    }
    timings.redundancyMs = round(clock() - mark, 3);

    // ── ФАЗА 8. Живучесть ────────────────────────────────────────────────
    mark = clock();
    const surv = analyzeSurvivability(g, edgeActive, nodeActive, capKbps, cpNodes, limits, issues);
    timings.survivabilityMs = round(clock() - mark, 3);

    // ── ФАЗА 9. Расписание развертывания ─────────────────────────────────
    mark = clock();
    const schedule = buildDeploySchedule({ nodes, nt, fleet, nodeActive, nodeIndex, crews: C.maxSimultaneousDeploy, crewSpeedKmh: C.crewSpeedKmh, cpNodes, lat: nt.lat, lng: nt.lng });
    timings.scheduleMs = round(clock() - mark, 3);

    // ── ФАЗА 10. Метрики, статус, диагностика ────────────────────────────
    mark = clock();
    const linkStats = collectLinkStats(g, edgeActive, orig, table, linkOrigin, router, marginDb, capKbps, delayMs);
    const metrics = computeMetrics({
      maxDelayRef: C.maxDelayMs,
      demands,
      routeResults,
      satisfied,
      partialCount,
      rejectedCount,
      grantedTotal,
      demandTotal,
      spent,
      budget,
      budgetLimited,
      surv,
      linkStats,
      weights,
      schedule,
      n,
      ms,
      ctx0Components: comps.count,
      linksTotal: m,
      feasCount: feas.length,
      redundancyAdded,
    });
    const infeasibility = buildDiagnostics({ issues, recommendations, comps, g, n, fleet, nodes, demands, routeResults, rejectHist, table, linkOrigin, orig, relaxation, budgetBlocked, surv, metrics, nt, report, C, weights, backbone, unknownRef, demandRefBroken, linkStats, improvements });
    const status = decideStatus({ routeResults, demands, satisfied, partialCount, rejectedCount, timedOut, feas, ms, n, metrics, surv, budgetBlocked, lb, spent, budgetLimited, budget, redundancySkipped, needRedundancy, C, noTerminals: cpNodes.length < 2 && termArr.length < 2 });
    timings.metricsMs = round(clock() - mark, 3);

    // ── ФАЗА 11. Перекрёстная проверка полным ядром ENG6 ─────────────────
    let eng6Analysis = { skipped: true, reason: 'DISABLED' };
    if (C.eng6Analysis !== false && status !== 'INVALID') {
      const limN = C.crossCheckMaxNodes ?? 400;
      const limL = C.crossCheckMaxLinks ?? 600;
      const sizeOk = linkStats.activeCount <= limL && surv.activeNodes <= limN;
      if (!sizeOk)
        eng6Analysis = {
          skipped: true,
          reason: 'SIZE_GUARD',
          activeLinks: linkStats.activeCount,
          nodes: surv.activeNodes,
          limit: { nodes: limN, links: limL },
          note: 'Полный анализ ENG6 (междуность, собственные значения, criticalNodes) имеет сложность O(V·E) — на крупных экземплярах отключается, чтобы не сорвать timeLimitMs',
        };
      else if (!checkDeadline()) {
        mark = clock();
        try {
          eng6Analysis = runEng6CrossCheck({ g, edgeActive, orig, table, linkOrigin, nodes, nt, env, request, E, quality: C.crossCheckQuality || 'balanced' });
        } catch (err) {
          eng6Analysis = { skipped: true, reason: 'ERROR', message: String((err && err.message) || err) };
        }
        timings.eng6Ms = round(clock() - mark, 3);
      } else eng6Analysis = { skipped: true, reason: 'TIMEOUT' };
    }

    if (eng6Analysis && !eng6Analysis.skipped && eng6Analysis.verdict !== 'CONSISTENT') {
      issues.push({
        sev: eng6Analysis.verdict === 'REJECTED_BY_ENG6' ? 'crit' : 'warn',
        code: 'ENG6_CROSSCHECK_' + eng6Analysis.verdict,
        obj: { kind: 'net' },
        title: 'Перекрёстная проверка ENG6: ' + (eng6Analysis.verdict === 'REJECTED_BY_ENG6' ? 'план не подтверждён' : 'план деградирует'),
        text: `Полное ядро ENG6 (quality=${eng6Analysis.quality}, рельеф ${env.terrain.source}) признаёт активными ${eng6Analysis.comparison.activeLinks.eng6} из ${eng6Analysis.comparison.activeLinks.planner} линий. Причина: в режиме physics=fast не учитывается дифракция на рельефе. Рекомендация: physics=full или terrain.source=flat.`,
      });
    }
    out.status = timedOut ? 'TIMEOUT' : status;
    out.statusReasons = buildStatusReasons({ status, timedOut, satisfied, partialCount, rejectedCount, demands, budgetBlocked, surv, feas, ms, metrics });
    out.plan = buildPlanOutput({ g, edgeActive, nodeActive, orig, table, linkOrigin, nodes, nt, fleet, router, demands, routeResults, schedule, linkStats, surv, nodeIndex, marginDb, capKbps, delayMs, availability, redundancyAdded, candidateComponents: comps.count });
    out.metrics = metrics;
    out.diagnostics = {
      validation: report.warnings.length ? report.toJSON() : { ok: true, errors: [], warnings: [], truncated: false },
      issues: issues.slice(0, OUT_LIMITS.issues),
      recommendations: recommendations.slice(0, 60),
      infeasibility,
      rejectedLinksHistogram: rejectHist,
      approximation: {
        physics: physicsMode,
        steiner: backbone.mode,
        routing: 'successive_shortest_paths_greedy',
        knapsack: ctx2Knap(improvements),
        schedule: 'list_scheduling_graham',
      },
      eng6Analysis,
      lowerBoundCost: round(lb, 3),
    };
    out.timings.totalMs = round(clock() - t0, 3);
    out.core = meta;
    return out;
  } catch (err) {
    // ядро не должно «падать» наружу 500-й без контекста — фиксируем всё
    return {
      ...out,
      status: 'ERROR',
      statusReasons: [{ code: 'CORE_EXCEPTION', message: String((err && err.message) || err), stack: process.env.ENG6_STRICT === '1' ? String(err && err.stack) : undefined }],
      diagnostics: { issues, recommendations, validation: report ? report.toJSON() : null, exception: String((err && err.message) || err) },
      timings: { ...timings, totalMs: round(clock() - t0, 3) },
    };
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Вспомогательные функции фаз
   ══════════════════════════════════════════════════════════════════════════ */

function timeoutResult(out, ctx) {
  out.status = 'TIMEOUT';
  out.statusReasons = [{ code: 'TIME_LIMIT', message: `Исчерпан лимит времени на фазе «${ctx.phase}» — возвращено частичное решение` }];
  out.diagnostics = { issues: ctx.issues, recommendations: ctx.recommendations, validation: ctx.report.toJSON(), infeasibility: null };
  out.timings.totalMs = round(ctx.clock() - ctx.t0, 3);
  return out;
}

function resolvePhysicsMode(requested, m, limits, report, issues) {
  if (requested === 'fast') return 'fast';
  if (requested === 'full') {
    if (m > limits.fullPhysicsMaxLinks) {
      report.warn('CROSS', '$.constraints.physics', `Режим 'full' запрошен для ${m} рёбер (лимит ${limits.fullPhysicsMaxLinks}) — автоматически переключено на 'fast'`);
      issues.push({ sev: 'warn', code: 'PHYSICS_DOWNGRADE', obj: { kind: 'net' }, title: 'Режим физики понижен до fast', text: `${m} рёбер > лимита ${limits.fullPhysicsMaxLinks} для полного расчёта ENG6.calcLink` });
      return 'fast';
    }
    return 'full';
  }
  return m <= limits.fullPhysicsMaxLinks ? 'full' : 'fast';
}

/**
 * Назначение единиц техники узлам (FR-RES-03: 🔴 не размещается, 🟡 — с предупреждением).
 */
function assignFleet(nodes, fleet, nt, C, nodeIndex, issues) {
  const n = nodes.length;
  const deployable = new Uint8Array(n);
  const unitOfNode = new Int32Array(n).fill(-1);
  const unitIssues = [];
  const shortage = {};
  for (let i = 0; i < n; i++) deployable[i] = nt.ready[i];

  if (!Array.isArray(fleet) || fleet.length === 0) {
    return { deployable, unitOfNode, unitIssues, shortage, byNode: null, mode: 'unlimited' };
  }
  // пулы по типам узлов, детерминированный порядок: green → yellow → red, дешевле → дороже
  const pools = new Map();
  const allowed = (u) => u.condition === 'green' || (u.condition === 'yellow' && C.allowYellowUnits !== false) || (u.condition === 'red' && C.allowRedUnits === true);
  fleet.forEach((u, i) => {
    if (!allowed(u)) {
      unitIssues.push({ unitId: u.unitId, code: 'UNIT_NOT_READY', condition: u.condition, message: u.condition === 'red' ? 'Неисправна/в ремонте — размещение запрещено (FR-RES-03)' : 'Ограниченно пригодна, использование запрещено ограничениями запроса' });
      return;
    }
    const key = u.nodeType || 'any';
    if (!pools.has(key)) pools.set(key, []);
    pools.get(key).push(i);
  });
  for (const arr of pools.values()) arr.sort((a, b) => (CONDITION_RANK[fleet[a].condition] || 0) - (CONDITION_RANK[fleet[b].condition] || 0) || (fleet[a].costDeploy || 0) - (fleet[b].costDeploy || 0) || String(fleet[a].unitId).localeCompare(String(fleet[b].unitId)));
  const usedUnit = new Uint8Array(fleet.length);
  const byId = new Map(fleet.map((u, i) => [u.unitId, i]));
  const byNode = new Array(n).fill(null);

  // 0) логические агрегаты: «Узел связи» (FR-MAP-04) — это группа уже
  //    назначенной техники, отдельная единица из парка ему НЕ требуется.
  //    Иначе узел связи забирал бы вторую КШМ и заявки от него отклонялись
  //    бы с UNIT_NOT_READY (обнаружено на демо-сценарии сборки UI).
  for (let i = 0; i < n; i++) {
    const nd = nodes[i];
    const isAggregate = Array.isArray(nd.children) && nd.children.length > 0 || nd.isGroup === true;
    if (!isAggregate) continue;
    if (nd.unitId) continue; // явная привязка имеет приоритет
    deployable[i] = nt.ready[i];
    unitOfNode[i] = -2; // маркер агрегата
    nt.cost[i] = typeof nd.costDeploy === 'number' ? nd.costDeploy : 0;
    nt.deployMin[i] = typeof nd.deployTimeMin === 'number' ? nd.deployTimeMin : 0;
  }
  // 1) явные привязки
  for (let i = 0; i < n; i++) {
    const uid = nodes[i].unitId;
    if (!uid) continue;
    const ui = byId.get(uid);
    if (ui === undefined) {
      unitIssues.push({ unitId: uid, nodeId: nodes[i].id, code: 'UNIT_NOT_FOUND', message: `Единица техники «${uid}» отсутствует в парке` });
      deployable[i] = 0;
      continue;
    }
    if (usedUnit[ui]) {
      unitIssues.push({ unitId: uid, nodeId: nodes[i].id, code: 'UNIT_ALREADY_USED', message: `Единица «${uid}» уже назначена другому узлу` });
      deployable[i] = 0;
      continue;
    }
    if (!allowed(fleet[ui])) {
      unitIssues.push({ unitId: uid, nodeId: nodes[i].id, code: 'UNIT_NOT_READY', condition: fleet[ui].condition, message: 'Техника не готова к применению' });
      deployable[i] = 0;
      continue;
    }
    usedUnit[ui] = 1;
    unitOfNode[i] = ui;
    byNode[i] = fleet[ui];
    applyUnit(nt, i, fleet[ui], C, issues, nodes[i]);
  }
  // 2) распределение из пулов. Курсоры обязательны: без них на 10^4 однотипных
  // узлов получаем O(n^2) (49 млн итераций на n=10 000) вместо O(n).
  const cursor = new Map();
  for (const key of pools.keys()) cursor.set(key, 0);
  const takeFromPool = (key) => {
    const pool = pools.get(key);
    if (!pool) return -1;
    let c = cursor.get(key) || 0;
    while (c < pool.length && usedUnit[pool[c]]) c++;
    cursor.set(key, c);
    return c < pool.length ? pool[c] : -1;
  };
  for (let i = 0; i < n; i++) {
    if (unitOfNode[i] !== -1 || !deployable[i]) continue;
    const type = nodes[i].type;
    let picked = takeFromPool(type);
    if (picked < 0) picked = takeFromPool('any');
    if (picked < 0) {
      deployable[i] = 0;
      shortage[type] = (shortage[type] || 0) + 1;
      continue;
    }
    usedUnit[picked] = 1;
    unitOfNode[i] = picked;
    byNode[i] = fleet[picked];
    applyUnit(nt, i, fleet[picked], C, issues, nodes[i]);
  }
  for (const [type, cnt] of Object.entries(shortage))
    issues.push({ sev: 'crit', code: 'UNIT_SHORTAGE', obj: { kind: 'net' }, title: `Нехватка техники: ${type}`, text: `${cnt} узл(ов) не могут быть развернуты — в парке нет готовых единиц типа «${type}»` });
  const unused = fleet.filter((u, i) => !usedUnit[i] && allowed(u)).map((u) => u.unitId);
  return { deployable, unitOfNode, unitIssues, shortage, byNode, unused, mode: 'fleet' };
}

function applyUnit(nt, i, unit, C, issues, node) {
  if (typeof unit.costDeploy === 'number') nt.cost[i] = unit.costDeploy;
  if (typeof unit.deployTimeMin === 'number') nt.deployMin[i] = unit.deployTimeMin;
  if (unit.condition === 'yellow') {
    nt.txPowerDbm[i] -= unit.degradationDb ?? 3; // деградация тракта ограниченно пригодной техники
    issues.push({ sev: 'warn', code: 'UNIT_DEGRADED', obj: { kind: 'node', id: node.id }, title: `Ограниченно пригодная техника: ${node.id}`, text: `Единица «${unit.unitId}» (🟡) — запас уменьшен на ${unit.degradationDb ?? 3} дБ; ${unit.note || 'требуется ЗИП'}` });
  }
  void C;
}

function connectivityOf(g, comps, activeMask) {
  if (g.n <= 1) return g.n === 1 ? 100 : 0;
  const sizes = new Map();
  for (let i = 0; i < g.n; i++) {
    if (activeMask && !activeMask[i]) continue;
    const c = comps.comp[i];
    sizes.set(c, (sizes.get(c) || 0) + 1);
  }
  let pairs = 0;
  let total = 0;
  for (const s of sizes.values()) {
    pairs += (s * (s - 1)) / 2;
    total += s;
  }
  const all = (total * (total - 1)) / 2;
  return all ? (pairs / all) * 100 : total ? 100 : 0;
}

/** Поиск резервного (реберно-непересекающегося) маршрута. */
function findBackupPath(g, router, usedEdges, s, t, opts) {
  const blocked = new Uint8Array(g.m);
  for (const e of usedEdges) blocked[e] = 1;
  const W = new Float64Array(g.arcs);
  for (let a = 0; a < g.arcs; a++) W[a] = blocked[g.edgeId[a]] ? Infinity : router.arcW[a] === Infinity ? Infinity : router.arcW[a];
  // если ребро уже активно, но не на основном пути — его стоимость не добавляется (штраф 0)
  for (let a = 0; a < g.arcs; a++) {
    const e = g.edgeId[a];
    if (!blocked[e] && router.p.edgeActive[e] && Number.isFinite(W[a])) W[a] = Math.max(0, W[a] - router.p.delayMs[e] * 0.0);
  }
  const r = dijkstra(g, s, { target: t, weight: W });
  if (!Number.isFinite(r.dist[t])) return null;
  const p = pathFromPrev(g, r.prevArc, s, t);
  if (!p) return null;
  const edges = [...new Set(p.arcs.map((a) => g.edgeId[a]))];
  const ev = router.evaluatePath(Int32Array.from(p.arcs), 0, false);
  if (ev.delayMs > opts.maxDelayMs) return null;
  return { edges: Int32Array.from(edges), nodes: Int32Array.from(p.nodes), delayMs: ev.delayMs };
}

/** Живучесть активного подграфа: SPOF, мосты, min-cut. */
function analyzeSurvivability(g, edgeActive, nodeActive, capKbps, cpNodes, limits, issues) {
  const res = { componentsCount: 0, largestComponentPct: 0, connectivityPct: 0, bridges: [], bridgeCount: 0, spofNodes: [], spofCount: 0, minCutKbps: null, minCutPair: null, activeNodes: 0, activeLinks: 0, computed: [] };
  let activeLinks = 0;
  for (let e = 0; e < g.m; e++) if (edgeActive[e]) activeLinks++;
  let activeNodes = 0;
  for (let i = 0; i < g.n; i++) if (nodeActive[i]) activeNodes++;
  res.activeLinks = activeLinks;
  res.activeNodes = activeNodes;
  if (activeLinks === 0) return res;

  const sub = g.subgraph(edgeActive);
  const comps = components(sub);
  // компоненты считаем только по активным узлам
  const sizesByComp = new Map();
  for (let i = 0; i < g.n; i++) {
    if (!nodeActive[i]) continue;
    const c = comps.comp[i];
    sizesByComp.set(c, (sizesByComp.get(c) || 0) + 1);
  }
  let pairs = 0;
  let totalNodes = 0;
  for (const s of sizesByComp.values()) {
    pairs += (s * (s - 1)) / 2;
    totalNodes += s;
  }
  const all = (totalNodes * (totalNodes - 1)) / 2;
  res.connectivityPct = all ? round((pairs / all) * 100, 3) : totalNodes ? 100 : 0;
  res.componentsCount = sizesByComp.size;
  let largest = 0;
  for (const s of sizesByComp.values()) if (s > largest) largest = s;
  res.largestComponentPct = totalNodes ? round((largest / totalNodes) * 100, 3) : 0;
  res.computed.push('components');

  const { bridgeEdges, articulations } = bridgesAndArticulations(sub);
  res.bridges = Array.from(bridgeEdges).slice(0, OUT_LIMITS.spof).map((e) => sub.edgeIds[e]);
  res.bridgeCount = bridgeEdges.length;
  res.spofNodes = Array.from(articulations).filter((i) => nodeActive[i]).slice(0, OUT_LIMITS.spof);
  res.spofCount = articulations.filter((i) => nodeActive[i]).length;
  res.computed.push('bridges', 'articulations');

  const BRIDGE_ISSUE_LIMIT = 5;
  for (let i = 0; i < Math.min(bridgeEdges.length, BRIDGE_ISSUE_LIMIT); i++) {
    const oid = sub.edgeIds[bridgeEdges[i]];
    issues.push({ sev: 'warn', code: 'BRIDGE_LINK', obj: { kind: 'link', index: oid }, title: 'Линия-мост (единственное соединение)', text: 'Удаление линии разрывает сеть — требуется резервное направление' });
  }
  if (bridgeEdges.length > BRIDGE_ISSUE_LIMIT)
    issues.push({ sev: 'warn', code: 'BRIDGE_LINK', obj: { kind: 'net' }, title: `Линий-мостов: ${bridgeEdges.length}`, text: `Показаны первые ${BRIDGE_ISSUE_LIMIT}; полный список — plan.survivability.bridgeLinks` });

  // min-cut между двумя первыми КП (точный расчёт ограничен по размеру)
  const cps = cpNodes.filter((c) => nodeActive[c]);
  if (cps.length >= 2 && sub.arcs <= limits.flowMaxArcs && g.n <= 20000) {
    const cap = new Float64Array(sub.m);
    for (let e = 0; e < sub.m; e++) cap[e] = capKbps[sub.subIndex[e]];
    const f = maxFlowUndirected(sub, cps[0], cps[1], cap);
    res.minCutKbps = round(f, 2);
    res.minCutPair = [cps[0], cps[1]];
    res.computed.push('maxflow');
  } else if (cps.length >= 2) {
    res.minCutSkipped = 'SIZE_GUARD';
  }
  return res;
}

/** Расписание развертывания по активным узлам. */
function buildDeploySchedule({ nodes, nt, fleet, nodeActive, crews, crewSpeedKmh, cpNodes, lat, lng }) {
  const jobs = [];
  // точка отсчёта марша — первый КП (или центроид активных узлов)
  let refLat = 0;
  let refLng = 0;
  if (cpNodes.length) {
    refLat = lat[cpNodes[0]];
    refLng = lng[cpNodes[0]];
  } else {
    let k = 0;
    for (let i = 0; i < nodes.length; i++)
      if (nodeActive[i]) {
        refLat += lat[i];
        refLng += lng[i];
        k++;
      }
    if (k) {
      refLat /= k;
      refLng /= k;
    }
  }
  const speed = Math.max(1, crewSpeedKmh || 40);
  for (let i = 0; i < nodes.length; i++) {
    if (!nodeActive[i]) continue;
    const nd = nodes[i];
    const dist = haversineKm(refLat, refLng, lat[i], lng[i]);
    const unit = fleet.byNode ? fleet.byNode[i] : null;
    const precede = [];
    if (nd.parentId !== undefined && nd.parentId !== null) precede.push(String(nd.parentId));
    jobs.push({
      id: String(nd.id),
      nodeId: String(nd.id),
      unitId: unit ? unit.unitId : null,
      kind: unit ? unit.kind || unit.nodeType || nd.type : nd.type,
      deployMin: nt.deployMin[i],
      marchMin: (dist / speed) * 60,
      marchKm: round(dist, 3),
      precede,
      criticality: nd.type === 'cp' ? 1 : 0.5,
    });
  }
  // предшественники должны существовать в плане
  const ids = new Set(jobs.map((j) => j.id));
  for (const j of jobs) j.precede = j.precede.filter((p) => ids.has(p) && p !== j.id);
  const sched = buildSchedule({ jobs, crews, t0: 0 });
  sched.referencePoint = { lat: round6(refLat), lng: round6(refLng) };
  sched.criticalPath = criticalPath(sched);
  return sched;
}

/** Сводка по активным линиям. */
function collectLinkStats(g, edgeActive, orig, table, linkOrigin, router, marginDb, capKbps, delayMs) {
  const active = [];
  let activeCount = 0;
  let capTotal = 0;
  let loadTotal = 0;
  let marginSum = 0;
  let marginMin = Infinity;
  let utilMax = 0;
  let delayMax = 0;
  let below10 = 0;
  let below3 = 0;
  const hist = { margin: { '<0': 0, '0-3': 0, '3-10': 0, '10-20': 0, '>20': 0 } };
  for (let i = 0; i < g.m; i++) {
    if (!edgeActive[i]) continue;
    activeCount++;
    const e = orig[i];
    const load = router.load[i];
    const cap = capKbps[i];
    capTotal += cap;
    loadTotal += load;
    const mg = marginDb[i];
    marginSum += mg;
    if (mg < marginMin) marginMin = mg;
    if (mg < 0) hist.margin['<0']++;
    else if (mg < 3) hist.margin['0-3']++;
    else if (mg < 10) { hist.margin['3-10']++; below10++; }
    else if (mg < 20) hist.margin['10-20']++;
    else hist.margin['>20']++;
    if (mg < 10) below10++;
    if (mg < 3) below3++;
    const util = cap > 0 ? load / cap : 0;
    if (util > utilMax) utilMax = util;
    if (delayMs[i] > delayMax) delayMax = delayMs[i];
    if (active.length < OUT_LIMITS.links) {
      active.push({
        id: linkOrigin[e] ? linkOrigin[e].id : `e${e}`,
        from: linkOrigin[e] ? linkOrigin[e].from : String(g.edgeFrom[i]),
        to: linkOrigin[e] ? linkOrigin[e].to : String(g.edgeTo[i]),
        type: linkOrigin[e] ? linkOrigin[e].type : Object.keys(LINK_TYPE_IX)[table.typeIdx[e]],
        distKm: round(table.distKm[e], 4),
        marginDb: round(mg, 2),
        capacityKbps: round(cap, 2),
        loadKbps: round(load, 2),
        utilPct: round(util * 100, 1),
        delayMs: round(delayMs[i], 3),
        availabilityPct: round(table.availability[e] * 100, 3),
        cost: round(table.cost[e], 2),
        role: 'primary',
        physics: physicsOf(e, table),
        sensMarginDb: round(table.sensMarginDb[e], 2),
      });
    }
  }
  return {
    active,
    activeCount,
    capTotalKbps: round(capTotal, 2),
    loadTotalKbps: round(loadTotal, 2),
    utilMaxPct: round(utilMax * 100, 2),
    utilAvgPct: capTotal > 0 ? round((loadTotal / capTotal) * 100, 2) : 0,
    avgMarginDb: activeCount ? round(marginSum / activeCount, 2) : 0,
    minMarginDb: Number.isFinite(marginMin) ? round(marginMin, 2) : null,
    linksBelow10Db: below10,
    linksBelow3Db: below3,
    delayMaxMs: round(delayMax, 3),
    marginHistogram: hist.margin,
  };
}

function physicsOf(e, table) {
  const typeName = Object.keys(LINK_TYPE_IX)[table.typeIdx[e]] || 'VHF';
  return {
    plDb: round(table.plDb[e], 2),
    rxDbm: round(table.rxDbm[e], 2),
    snrDb: round(table.snrDb[e], 2),
    sensMarginDb: round(table.sensMarginDb[e], 2),
    theoreticalKbps: round(table.theoreticalKbps[e], 2),
    los: !!table.los[e],
    type: typeName,
  };
}

function computeMetrics(ctx) {
  const { demands, routeResults, satisfied, partialCount, rejectedCount, grantedTotal, demandTotal, spent, budget, budgetLimited, surv, linkStats, weights, schedule, n, ms, feasCount, redundancyAdded, ctx0Components } = ctx;
  const delays = [];
  let delayViolations = 0;
  let availMin = 100;
  for (let k = 0; k < demands.length; k++) {
    const r = routeResults[k];
    if (!r || (r.status !== 'ROUTED' && r.status !== 'PARTIAL')) continue;
    if (Number.isFinite(r.delayMs)) {
      delays.push(r.delayMs);
      const lim = Math.min(demands[k].maxDelayMs ?? Infinity, Infinity);
      if (Number.isFinite(lim) && r.delayMs > lim) delayViolations++;
    }
    if (r.availabilityPct !== undefined && r.availabilityPct < availMin) availMin = r.availabilityPct;
  }
  const avgDelay = delays.length ? delays.reduce((a, b) => a + b, 0) / delays.length : 0;
  const maxDelay = delays.length ? Math.max(...delays) : 0;
  const demandServedPct = demandTotal > 0 ? (grantedTotal / demandTotal) * 100 : satisfied || demands.length === 0 ? 100 : 0;
  const budgetUsedPct = budgetLimited && budget > 0 ? (spent / budget) * 100 : null;

  const costNorm = budgetLimited && budget > 0 ? clamp(spent / budget, 0, 2) : clamp(spent / Math.max(1, spent + 1), 0, 1);
  const delayNorm = clamp(avgDelay / Math.max(1, ctx.maxDelayRef || 250), 0, 2);
  const survNorm = clamp(((surv.connectivityPct || 0) / 100) * 0.6 + (surv.spofCount === 0 ? 0.4 : clamp(0.4 - surv.spofCount * 0.05, 0, 0.4)), 0, 1);
  const marginNorm = clamp((linkStats.avgMarginDb + 10) / 40, 0, 1);
  const F = objective({ costNorm, delayNorm, survivabilityNorm: survNorm, marginNorm, weights });

  const gradeByObjective = F <= 0.15 ? 'A' : F <= 0.3 ? 'B' : F <= 0.5 ? 'C' : F <= 0.7 ? 'D' : 'F';
  // Уровень обслуживания заявок ограничивает оценку сверху: пустой план «дешев»
  // и «быстр», поэтому без этого правила INFEASIBLE получал бы оценку B —
  // маскировка провала. Берётся худшая из двух оценок.
  // Два уровня обслуживания:
  //  • по числу заявок (операционный смысл: сколько нарядов выполнено);
  //  • по полосе (сколько кбит/с из запрошенных выдано) — может быть низким
  //    при одной широкополосной заявке на узком канале.
  // Оценка плана ограничивается худшим из них, но считается по «заявочному»
  // уровню: иначе одна заявка 512 кбит/с обнуляла бы оценку при 5 выполненных.
  const servedByCountPct = demands.length > 0 ? ((satisfied + 0.5 * partialCount) / demands.length) * 100 : 100;
  const servedByBandwidthPct = demandTotal > 0 ? (grantedTotal / demandTotal) * 100 : demands.length === 0 ? 100 : 0;
  const servedPct = Math.max(servedByCountPct, 0);
  const gradeByService = servedPct >= 99.5 ? 'A' : servedPct >= 80 ? 'B' : servedPct >= 50 ? 'C' : servedPct >= 20 ? 'D' : 'F';
  const rank = { A: 0, B: 1, C: 2, D: 3, F: 4 };
  const grade = rank[gradeByService] > rank[gradeByObjective] ? gradeByService : gradeByObjective;
  return {
    objectiveValue: round(F, 5),
    grade,
    gradeByObjective,
    gradeByService,
    serviceLevelByCountPct: round(servedByCountPct, 3),
    serviceLevelByBandwidthPct: round(servedByBandwidthPct, 3),
    nodesTotal: n,
    nodesDeployed: surv.activeNodes,
    linksCandidate: ctx.linksTotal,
    linksFeasible: feasCount,
    linksActive: surv.activeCount ?? linkStats.activeCount,
    totalCost: round(spent, 2),
    budget: budgetLimited ? round(budget, 2) : null,
    budgetUsedPct: budgetUsedPct === null ? null : round(budgetUsedPct, 2),
    demandsTotal: demands.length,
    demandsSatisfied: satisfied,
    demandsPartial: partialCount,
    demandsRejected: rejectedCount,
    demandServedPct: round(demandServedPct, 3),
    throughputKbps: round(grantedTotal, 2),
    capacityTotalKbps: linkStats.capTotalKbps,
    loadTotalKbps: linkStats.loadTotalKbps,
    utilMaxPct: linkStats.utilMaxPct,
    utilAvgPct: linkStats.utilAvgPct,
    avgDelayMs: round(avgDelay, 3),
    maxDelayMs: round(maxDelay, 3),
    delayViolations,
    minAvailabilityPct: delays.length ? round(availMin, 3) : null,
    connectivityPct: surv.connectivityPct,
    componentsCount: surv.componentsCount,
    candidateComponentsCount: ctx0Components,
    largestComponentPct: surv.largestComponentPct,
    spofNodes: surv.spofCount || 0,
    bridgeLinks: surv.bridgeCount || 0,
    minCutKbps: surv.minCutKbps,
    avgMarginDb: linkStats.avgMarginDb,
    minMarginDb: linkStats.minMarginDb,
    linksBelow10Db: linkStats.linksBelow10Db,
    marginHistogram: linkStats.marginHistogram,
    redundancyAdded,
    scheduleMakespanMin: schedule ? schedule.makespanMin : 0,
    scheduleWaves: schedule ? schedule.waves.length : 0,
    crewUtilPct: schedule ? schedule.crewUtilPct : 0,
  };
}

function decideStatus(ctx) {
  const { routeResults, demands, satisfied, partialCount, rejectedCount, timedOut, feas, ms, n, metrics, surv, budgetBlocked, lb, spent, budgetLimited, budget, redundancySkipped, needRedundancy } = ctx;
  if (timedOut) return 'TIMEOUT';
  if (n === 0) return 'INFEASIBLE';
  if (ms === 0 || feas.length === 0) return demands.length === 0 ? 'INFEASIBLE' : 'INFEASIBLE';
  if (demands.length === 0) {
    // нет заявок: оцениваем только каркас связности КП
    if (surv.activeLinks === 0) return ctx.noTerminals ? 'FEASIBLE' : 'INFEASIBLE';
    return 'FEASIBLE';
  }
  const allSatisfied = satisfied === demands.length && rejectedCount === 0 && partialCount === 0;
  if (!allSatisfied) {
    if (satisfied + partialCount === 0) return 'INFEASIBLE';
    return 'PARTIAL';
  }
  if (needRedundancy && redundancySkipped > 0) return 'PARTIAL';
  if (budgetLimited && spent > budget + 1e-9) return 'PARTIAL';
  if (budgetBlocked.length) return 'PARTIAL';
  // OPTIMAL — только если стоимость достигла нижней оценки (доказуемо минимальна в модели)
  if (lb > 0 && spent <= lb * (1 + 1e-9)) return 'OPTIMAL';
  return 'FEASIBLE';
}

function buildStatusReasons(ctx) {
  const reasons = [];
  const { status, timedOut, satisfied, partialCount, rejectedCount, demands, budgetBlocked, surv, feas, ms, metrics } = ctx;
  if (timedOut) reasons.push({ code: 'TIME_LIMIT', message: 'Расчёт прерван по лимиту времени, возвращено частичное решение' });
  if (ms === 0) reasons.push({ code: 'NO_LINKS', message: 'В топологии нет ни одного ребра-кандидата' });
  else if (feas.length === 0) reasons.push({ code: 'NO_FEASIBLE_LINK', message: 'Ни одно ребро не прошло физический допуск (запас/ёмкость/задержка)' });
  if (rejectedCount > 0) reasons.push({ code: 'DEMANDS_REJECTED', message: `Отклонено заявок: ${rejectedCount} из ${demands.length}`, count: rejectedCount });
  if (partialCount > 0) reasons.push({ code: 'DEMANDS_PARTIAL', message: `Частично удовлетворено заявок: ${partialCount}`, count: partialCount });
  if (satisfied === demands.length && demands.length > 0 && !timedOut) reasons.push({ code: 'ALL_DEMANDS_MET', message: `Все заявки (${demands.length}) удовлетворены` });
  if (budgetBlocked.length) reasons.push({ code: 'BUDGET_LIMIT', message: `Бюджет исчерпан: требуется ≥ ${Math.min(...budgetBlocked.map((b) => b.value))} у.е.` });
  if (surv.spofCount > 0) reasons.push({ code: 'SPOF_PRESENT', message: `Единых точек отказа: ${surv.spofCount} узл., ${surv.bridgeCount || 0} лин.` });
  if (status === 'OPTIMAL') reasons.push({ code: 'PROVEN_OPTIMAL', message: 'Стоимость активации равна нижней оценке задачи Штейнера — решение оптимально в принятой модели' });
  if (metrics && metrics.utilMaxPct > 85) reasons.push({ code: 'HIGH_UTILIZATION', message: `Утилизация каналов до ${metrics.utilMaxPct}% — выше порога 85%` });
  return reasons;
}

function buildDiagnostics(ctx) {
  const { issues, recommendations, comps, g, n, fleet, nodes, demands, routeResults, rejectHist, table, linkOrigin, orig, relaxation, budgetBlocked, surv, metrics, nt, report, C, weights, backbone, unknownRef, demandRefBroken, linkStats } = ctx;
  const infeasibility = { causes: [], disconnectedComponents: [], partialSolution: null, minimalRelaxations: [] };

  // 1) причины невыполнимости
  if (comps.count > 1) {
    infeasibility.causes.push({ code: 'NET_DISCONNECTED', message: `Граф допустимых линий имеет ${comps.count} компонент связности`, components: comps.count });
    const groups = new Map();
    for (let i = 0; i < n; i++) {
      const c = comps.comp[i];
      if (!groups.has(c)) groups.set(c, { index: c, size: 0, nodeIds: [], centroid: { lat: 0, lng: 0 } });
      const gr = groups.get(c);
      gr.size++;
      if (gr.nodeIds.length < 12) gr.nodeIds.push(nodes[i].id);
      gr.centroid.lat += nt.lat[i];
      gr.centroid.lng += nt.lng[i];
    }
    for (const gr of groups.values()) {
      gr.centroid.lat = round6(gr.centroid.lat / Math.max(1, gr.size));
      gr.centroid.lng = round6(gr.centroid.lng / Math.max(1, gr.size));
      infeasibility.disconnectedComponents.push(gr);
    }
    infeasibility.disconnectedComponents.sort((a, b) => b.size - a.size);
    // предложение мостов между компонентами
    const suggestions = suggestBridges(g, comps, nodes, nt, table, C, linkOrigin);
    for (const s of suggestions) infeasibility.minimalRelaxations.push(s);
  }
  if (fleet.mode === 'fleet' && Object.keys(fleet.shortage || {}).length)
    infeasibility.causes.push({ code: 'UNIT_SHORTAGE', message: 'Не хватает готовой техники для развертывания всех узлов', shortage: fleet.shortage });
  if (budgetBlocked.length) {
    const need = Math.min(...budgetBlocked.map((b) => b.value));
    infeasibility.causes.push({ code: 'BUDGET_EXCEEDED', message: `Бюджета недостаточно: требуется ≥ ${round(need, 2)} у.е.`, required: round(need, 2) });
    infeasibility.minimalRelaxations.push({ code: 'RAISE_BUDGET', value: round(need, 2), reason: 'минимальная сумма, при которой все заявки и резервирование проходят по бюджету' });
  }
  if (Number.isFinite(relaxation.delayMs)) {
    infeasibility.causes.push({ code: 'DELAY_UNREACHABLE', message: `Часть заявок не укладывается в ограничение задержки ${C.maxDelayMs} мс` });
    infeasibility.minimalRelaxations.push({ code: 'RELAX_MAX_DELAY_MS', value: round(relaxation.delayMs, 2), reason: 'минимальная задержка, при которой все отклонённые по задержке заявки становятся маршрутизируемыми' });
  }
  // ёмкостные послабления пересчитываются по ИТОГОВЫМ результатам (фаза 6.5
  // могла добрать полосу — устаревшие записи недопустимы в отчёте)
  relaxation.capacity = collectCapacityRelaxations(demands, routeResults);
  if (relaxation.capacity.length) {
    const agg = new Map();
    for (const c of relaxation.capacity) {
      const oid = orig[c.edge];
      const k = (linkOrigin[oid] && linkOrigin[oid].id) || `e${oid}`;
      agg.set(k, (agg.get(k) || 0) + (c.deficitKbps || 0));
    }
    const list = [...agg.entries()].map(([linkId, deficitKbps]) => ({ linkId, deficitKbps: round(deficitKbps, 2) })).sort((a, b) => b.deficitKbps - a.deficitKbps).slice(0, 20);
    infeasibility.causes.push({ code: 'CAPACITY_SHORTAGE', message: 'Суммарная нагрузка заявок превышает пропускную способность каналов', links: list });
    for (const l of list) infeasibility.minimalRelaxations.push({ code: 'ADD_CAPACITY', linkId: l.linkId, value: l.deficitKbps, reason: 'требуемое увеличение пропускной способности, кбит/с' });
  }
  const rejectedByReason = {};
  for (let k = 0; k < demands.length; k++) {
    const r = routeResults[k];
    if (!r || r.status === 'ROUTED') continue;
    rejectedByReason[r.reason || 'UNKNOWN'] = (rejectedByReason[r.reason || 'UNKNOWN'] || 0) + 1;
  }
  if (Object.keys(rejectedByReason).length) infeasibility.causes.push({ code: 'DEMANDS_UNSATISFIED', message: 'Заявки, не получившие обслуживания', byReason: rejectedByReason });
  if (unknownRef > 0) infeasibility.causes.push({ code: 'LINK_REF_BROKEN', message: `${unknownRef} рёбер ссылаются на несуществующие узлы — исключены из расчёта` });
  if (demandRefBroken > 0) infeasibility.causes.push({ code: 'DEMAND_REF_BROKEN', message: `${demandRefBroken} заявок ссылаются на несуществующие узлы` });

  // частичное решение
  const routed = routeResults.filter((r) => r && (r.status === 'ROUTED' || r.status === 'PARTIAL')).length;
  infeasibility.partialSolution = {
    demandsServed: routed,
    demandsTotal: demands.length,
    throughputKbps: metrics.throughputKbps,
    cost: metrics.totalCost,
    linksActivated: linkStats.activeCount,
    nodesDeployed: surv.activeNodes,
    note: routed > 0 ? 'План содержит максимально возможное при данных ограничениях число обслуженных заявок (приоритетные — в первую очередь)' : 'Ни одна заявка не может быть обслужена без ослабления ограничений — см. minimalRelaxations',
  };

  // 2) замечания по качеству
  if (metrics.utilMaxPct > (C.maxUtilizationPct ?? 85))
    issues.push({ sev: 'warn', code: 'OVERLOAD', obj: { kind: 'net' }, title: 'Перегрузка каналов', text: `Максимальная утилизация ${metrics.utilMaxPct}% > порога ${C.maxUtilizationPct ?? 85}%` });
  if (linkStats.linksBelow3Db > 0)
    issues.push({ sev: 'crit', code: 'LOW_MARGIN', obj: { kind: 'net' }, title: 'Линии с критическим запасом', text: `${linkStats.linksBelow3Db} активных линий имеют запас < 3 дБ` });
  if (comps.count > 1) issues.push({ sev: 'crit', code: 'NET_DISCONNECTED', obj: { kind: 'net' }, title: 'Сеть несвязна', text: `${comps.count} компонент связности; largest = ${round((comps.largest / Math.max(1, n)) * 100, 1)}% узлов` });
  if (report.warnings.length) for (const w of report.warnings.slice(0, 20)) issues.push({ sev: 'info', code: w.code, obj: { kind: 'net' }, title: 'Предупреждение валидатора', text: `${w.path}: ${w.message}` });
  for (const u of (fleet.unitIssues || []).slice(0, 30)) issues.push({ sev: u.code === 'UNIT_NOT_READY' && u.condition === 'red' ? 'crit' : 'warn', code: u.code, obj: { kind: 'unit', id: u.unitId }, title: `Техника: ${u.unitId}`, text: u.message });

  // 3) рекомендации
  if (surv.spofCount > 0) recommendations.push({ id: 'REC-SPOF', priority: 1, target: { kind: 'net' }, title: 'Устранить единые точки отказа', text: `Развернуть резервные направления для ${surv.spofCount} узл. и ${surv.bridgeCount || 0} лин.-мостов (требуется requireRedundancy=true или protected=true у заявок)` });
  if (relaxation.capacity.length) recommendations.push({ id: 'REC-CAP', priority: 2, target: { kind: 'net' }, title: 'Нарастить пропускную способность', text: `Перегружено каналов: ${new Set(relaxation.capacity.map((c) => c.edge)).size}. Варианты: переход на 16QAM/64QAM, расширение полосы, РРЛ вместо УКВ.` });
  if (comps.count > 1) recommendations.push({ id: 'REC-RELAY', priority: 1, target: { kind: 'net' }, title: 'Установить ретрансляторы', text: infeasibility.minimalRelaxations.filter((r) => r.code === 'ADD_RELAY').map((r) => `${r.at.lat},${r.at.lng}`).join('; ') || 'См. предложения в infeasibility.minimalRelaxations' });
  if (budgetBlocked.length) recommendations.push({ id: 'REC-BUDGET', priority: 2, target: { kind: 'net' }, title: 'Увеличить бюджет или снизить стоимость', text: `Минимально необходимый бюджет: ${round(Math.min(...budgetBlocked.map((b) => b.value)), 2)} у.е.` });
  if (metrics.linksBelow10Db > 0) recommendations.push({ id: 'REC-MARGIN', priority: 3, target: { kind: 'net' }, title: 'Поднять антенны / усилить передатчики', text: `${metrics.linksBelow10Db} линий имеют запас < 10 дБ — снизить риск деградации при осадках и замираниях` });
  if (weights.delay > 0.4 && metrics.avgDelayMs > 100) recommendations.push({ id: 'REC-DELAY', priority: 3, target: { kind: 'net' }, title: 'Оптимизировать задержку', text: `Средняя задержка ${metrics.avgDelayMs} мс; приоритет ВОЛС/РРЛ вместо спутниковых и КВ-направлений` });
  if (backbone.mode === 'shortest_path_tree') recommendations.push({ id: 'REC-STEINER', priority: 4, target: { kind: 'net' }, title: 'Каркас построен приближённо', text: `Терминалов ${backboneTerminalsCount(backbone)} > ${LIMITS.steinerExactMaxTerminals}: использовано дерево кратчайших путей вместо 2-аппроксимации Штейнера` });
  void orig;
  void table;
  void linkOrigin;
  return infeasibility;
}

function backboneTerminalsCount(backbone) {
  return backbone && backbone.edges ? backbone.edges.length : 0;
}

/**
 * Предложение точек установки ретрансляторов, связывающих компоненты.
 * Ближайшая пара узлов между компонентами ищется по выборке (не более
 * sampleLimit узлов на компоненту) — при 10^5 узлов полный перебор O(n²)
 * недопустим; приближение документируется в diagnostics.approximation.
 */
function suggestBridges(g, comps, nodes, nt, table, C, linkOrigin, sampleLimit = 400) {
  const out = [];
  const groups = new Map();
  for (let i = 0; i < g.n; i++) {
    const c = comps.comp[i];
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c).push(i);
  }
  const list = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
  if (list.length < 2) return out;
  const rng = mulberry(12345);
  for (let i = 0; i < list.length - 1 && out.length < 8; i++) {
    const A = sample(list[i][1], sampleLimit, rng);
    const B = sample(list[i + 1][1], sampleLimit, rng);
    let best = null;
    for (const a of A)
      for (const b of B) {
        const d = haversineKm(nt.lat[a], nt.lng[a], nt.lat[b], nt.lng[b]);
        if (!best || d < best.d) best = { d, a, b };
      }
    if (!best) continue;
    const midLat = round6((nt.lat[best.a] + nt.lat[best.b]) / 2);
    const midLng = round6((nt.lng[best.a] + nt.lng[best.b]) / 2);
    // требуемая ЭИИМ для перекрытия пролёта (инверсия FSPL на типовых 150 МГц, чувств. −105 дБм, запас C.minMarginDb)
    const E = loadEng6().ENG6;
    const pl = E.PROP.fspl(Math.max(0.5, best.d / 2), 150);
    const reqEirp = -105 + pl + (C.minMarginDb ?? 3) + (C.fastMarginPenaltyDb ?? 6);
    out.push({
      code: 'ADD_RELAY',
      at: { lat: midLat, lng: midLng },
      count: 1,
      wouldConnect: [list[i][0], list[i + 1][0]],
      spanKm: round(best.d, 3),
      requiredEirpDbm: round(reqEirp, 1),
      reason: `компоненты ${list[i][0]} (${list[i][1].length} узл.) и ${list[i + 1][0]} (${list[i + 1].length} узл.), ближайшая пара ${nodes[best.a].id}—${nodes[best.b].id}`,
      nearestPair: [nodes[best.a].id, nodes[best.b].id],
    });
  }
  void table;
  void linkOrigin;
  return out;
}

function mulberry(seed) {
  let t = seed >>> 0;
  return () => {
    t = (t + 0x6d2b79f5) >>> 0;
    let x = Math.imul(t ^ (t >>> 15), 1 | t);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}

function sample(arr, limit, rng) {
  if (arr.length <= limit) return arr;
  const out = [];
  for (let i = 0; i < limit; i++) out.push(arr[Math.floor(rng() * arr.length)]);
  return out;
}

function buildPlanOutput(ctx) {
  const { g, edgeActive, nodeActive, orig, table, linkOrigin, nodes, nt, fleet, demands, routeResults, schedule, linkStats, surv, redundancyAdded } = ctx;
  const jobByNode = new Map(schedule.jobs.map((j) => [j.nodeId, j]));
  const waveByNode = new Map();
  for (const w of schedule.waves) for (const it of w.items) waveByNode.set(it.nodeId, w.index);
  const deployedUnits = [];
  for (let i = 0; i < nodes.length && deployedUnits.length < OUT_LIMITS.nodes; i++) {
    if (!nodeActive[i]) continue;
    const unit = fleet.byNode ? fleet.byNode[i] : null;
    const job = jobByNode.get(nodes[i].id);
    deployedUnits.push({
      nodeId: nodes[i].id,
      nodeType: nodes[i].type,
      unitId: unit ? unit.unitId : null,
      condition: unit ? unit.condition : 'n/a',
      lat: round6(nt.lat[i]),
      lng: round6(nt.lng[i]),
      cost: round(nt.cost[i], 2),
      deployMin: round(nt.deployMin[i], 2),
      wave: job ? (waveByNode.get(job.nodeId) ?? null) : null,
      startMin: job ? job.startMin : null,
    });
  }
  const routes = [];
  for (let k = 0; k < demands.length && routes.length < OUT_LIMITS.routes; k++) {
    const r = routeResults[k];
    if (!r) continue;
    const d = demands[k];
    routes.push({
      demandId: d.id,
      from: d.from,
      to: d.to,
      requestedKbps: d.rateKbps,
      grantedKbps: r.grantedKbps || 0,
      deficitKbps: r.deficitKbps || 0,
      status: r.status,
      reason: r.reason,
      delayMs: r.delayMs ?? null,
      maxDelayMs: d.maxDelayMs ?? null,
      hops: r.hops ?? null,
      marginDb: r.marginDb ?? null,
      availabilityPct: r.availabilityPct ?? null,
      utilMaxPct: r.utilMaxPct ?? null,
      redundant: r.redundant ?? false,
      path: r.pathNodes ? Array.from(r.pathNodes).map((i) => nodes[i].id) : [],
      links: r.pathEdges ? Array.from(r.pathEdges).map((i) => linkOrigin[orig[i]]?.id ?? `e${orig[i]}`) : [],
      backupPath: r.backupPath ? { nodes: Array.from(r.backupPath.nodes).map((i) => nodes[i].id), links: Array.from(r.backupPath.edges).map((i) => linkOrigin[orig[i]]?.id ?? `e${orig[i]}`), delayMs: r.backupPath.delayMs } : null,
      bestEffort: r.bestEffort || null,
    });
  }
  const rejectedLinks = [];
  const hist = {};
  for (let e = 0; e < table.m; e++) {
    if (table.feasible[e] && table.rejectCode[e] === 0) continue;
    const code = table.rejectName(e);
    hist[code] = (hist[code] || 0) + 1;
    if (rejectedLinks.length < OUT_LIMITS.rejectedLinks)
      rejectedLinks.push({
        id: linkOrigin[e] ? linkOrigin[e].id : `e${e}`,
        from: linkOrigin[e] ? linkOrigin[e].from : null,
        to: linkOrigin[e] ? linkOrigin[e].to : null,
        type: linkOrigin[e] ? linkOrigin[e].type : null,
        code,
        reason: REJECT_TEXT[code] || code,
        distKm: round(table.distKm[e], 3),
        marginDb: round(table.marginDb[e], 2),
        capKbps: round(table.capKbps[e], 2),
        delayMs: round(table.delayMs[e], 2),
      });
  }
  return {
    deployedUnits,
    deployedUnitsTotal: surv.activeNodes,
    activeLinks: linkStats.active,
    activeLinksTotal: linkStats.activeCount,
    routes,
    routesTotal: demands.length,
    schedule: {
      waves: schedule.waves,
      makespanMin: schedule.makespanMin,
      crews: schedule.crews,
      crewUtilPct: schedule.crewUtilPct,
      criticalPath: schedule.criticalPath,
      referencePoint: schedule.referencePoint,
      violations: schedule.violations.slice(0, 20),
      jobs: schedule.jobs.slice(0, OUT_LIMITS.scheduleJobs),
      jobsTotal: schedule.jobs.length,
    },
    rejectedLinks,
    rejectedLinksHistogram: hist,
    survivability: {
      componentsCount: surv.componentsCount,
      candidateComponentsCount: ctx.candidateComponents ?? null,
      connectivityPct: surv.connectivityPct,
      largestComponentPct: surv.largestComponentPct,
      spofNodes: surv.spofNodes.map((i) => nodes[i].id),
      spofCount: surv.spofCount || 0,
      bridgeLinks: (surv.bridges || []).map((oid) => linkOrigin[oid]?.id ?? `e${oid}`),
      bridgeCount: surv.bridgeCount || 0,
      minCutKbps: surv.minCutKbps,
      minCutPair: surv.minCutPair ? surv.minCutPair.map((i) => nodes[i].id) : null,
      computed: surv.computed,
    },
    truncation: {
      links: linkStats.activeCount > OUT_LIMITS.links ? `показаны ${OUT_LIMITS.links} из ${linkStats.activeCount}` : null,
      routes: demands.length > OUT_LIMITS.routes ? `показаны ${OUT_LIMITS.routes} из ${demands.length}` : null,
      nodes: surv.activeNodes > OUT_LIMITS.nodes ? `показаны ${OUT_LIMITS.nodes} из ${surv.activeNodes}` : null,
    },
    redundancyAdded,
  };
}

/** Пересчёт итогов по заявкам после фазы улучшений. */
function recountRoutes(routeResults) {
  let satisfied = 0;
  let partial = 0;
  let rejected = 0;
  let notProcessed = 0;
  for (const r of routeResults) {
    if (!r) {
      notProcessed++;
      continue;
    }
    if (r.status === 'ROUTED') satisfied++;
    else if (r.status === 'PARTIAL') partial++;
    else if (r.status === 'REJECTED') rejected++;
    else notProcessed++;
  }
  return { satisfied, partial, rejected, notProcessed };
}

/**
 * ФАЗА 6.5 — оптимальное распределение остатка бюджета (0/1-рюкзак).
 *
 * Кандидаты-«улучшения»: активные рёбра с утилизацией выше порога и узкие места
 * частично/не обслуженных заявок. Стоимость улучшения — upgradeCostRatio от
 * стоимости активации ребра; полезность — приоритет заявок × дефицит полосы.
 * После выбора улучшений полоса перераспределяется:
 *   1) частичные заявки добирают полосу на уже построенном маршруте;
 *   2) отклонённые по ёмкости заявки маршрутизируются повторно.
 *
 * Если бюджет не ограничен — задача выбора вырождена, улучшения не применяются
 * (иначе ядро «бесплатно» раздувало бы ёмкость, скрывая перегрузку).
 */
function planImprovements(ctx) {
  const { g, router, capKbps, edgeCost, edgeActive, demands, routeResults, order, spent, budget, budgetLimited, C, issues, orig, linkOrigin, checkDeadline } = ctx;
  const res = { spent: 0, candidates: 0, applied: [], knapsack: { mode: 'none', exact: true, cellsUsed: 0 }, extraGrantedKbps: 0, newlyRouted: 0, upgradedToFull: 0, skipped: null };
  if (!budgetLimited) {
    res.skipped = 'UNBOUNDED_BUDGET';
    return res;
  }
  const remaining = budget - spent;
  if (remaining <= 1e-9) {
    res.skipped = 'NO_BUDGET_LEFT';
    return res;
  }
  const utilLimit = (C.maxUtilizationPct ?? 85) / 100;
  const cand = new Map();
  const add = (e, deficit, prio) => {
    const c = cand.get(e) || { deficit: 0, prio: 0 };
    c.deficit += deficit || 0;
    c.prio = Math.max(c.prio, prio || 0);
    cand.set(e, c);
  };
  for (let k = 0; k < demands.length; k++) {
    const r = routeResults[k];
    if (!r) continue;
    const prio = demands[k].priority ?? 3;
    if (r.status === 'PARTIAL' && r.pathEdges) {
      for (const e of r.pathEdges) add(e, r.deficitKbps || 0, prio);
    } else if (r.status === 'REJECTED' && r.reason === 'CAPACITY_SHORTAGE' && r.bestEffort && r.bestEffort.bottleneckEdge >= 0) {
      add(r.bestEffort.bottleneckEdge, r.deficitKbps || demands[k].rateKbps || 0, prio);
    }
  }
  for (let e = 0; e < g.m; e++) {
    if (!edgeActive[e]) continue;
    const cap = capKbps[e];
    if (cap <= 0) continue;
    const u = router.load[e] / cap;
    if (u > utilLimit) add(e, Math.max(0, router.load[e] - cap * utilLimit), 3);
  }
  res.candidates = cand.size;
  if (cand.size === 0) {
    res.skipped = 'NO_CANDIDATES';
    return res;
  }
  const upgradeCostRatio = C.upgradeCostRatio ?? 0.25;
  const upgradeFactor = C.upgradeFactor ?? 0.5;
  const items = [];
  const edgeOf = [];
  let i = 0;
  for (const [e, c] of cand) {
    const cost = Math.max(1, edgeCost[e] * upgradeCostRatio + 2);
    const cap = Math.max(1e-9, capKbps[e]);
    const value = (c.prio || 1) * clamp(c.deficit / cap, 0, 4) * 10;
    if (value <= 0) continue;
    items.push({ value, cost, id: e });
    edgeOf.push(e);
    i++;
  }
  void i;
  if (items.length === 0) {
    res.skipped = 'NO_POSITIVE_VALUE';
    return res;
  }
  const ks = knapsack01(items, remaining, { dpCellLimit: 2_000_000 });
  res.knapsack = { mode: ks.exact ? 'exact_dp' : 'greedy_improved', exact: ks.exact, cellsUsed: ks.cellsUsed, items: items.length, chosen: ks.chosen.length, value: round(ks.totalValue, 3) };
  if (checkDeadline && checkDeadline()) {
    res.skipped = 'TIMEOUT';
    return res;
  }
  for (const idx of ks.chosen) {
    const e = edgeOf[idx];
    const oldCap = capKbps[e];
    const newCap = oldCap * (1 + upgradeFactor);
    capKbps[e] = newCap;
    router.p.capKbps[e] = newCap;
    router.recomputeEdge(e);
    res.spent += items[idx].cost;
    res.applied.push({
      linkId: linkOrigin[orig[e]] ? linkOrigin[orig[e]].id : `e${orig[e]}`,
      edge: orig[e],
      capacityBeforeKbps: round(oldCap, 2),
      capacityAfterKbps: round(newCap, 2),
      cost: round(items[idx].cost, 2),
      value: round(items[idx].value, 3),
    });
  }
  if (res.applied.length === 0) {
    res.skipped = 'KNAPSACK_EMPTY';
    return res;
  }
  // 1) добор полосы частичными заявками
  for (const k of order) {
    const r = routeResults[k];
    if (!r || r.status !== 'PARTIAL' || !r.pathEdges) continue;
    let extra = r.deficitKbps || 0;
    for (const e of r.pathEdges) extra = Math.min(extra, router.residual(e));
    if (extra <= 1e-9) continue;
    for (const e of r.pathEdges) {
      router.load[e] += extra;
      router.recomputeEdge(e);
    }
    r.grantedKbps = round((r.grantedKbps || 0) + extra, 3);
    r.deficitKbps = round(Math.max(0, (r.deficitKbps || 0) - extra), 3);
    res.extraGrantedKbps += extra;
    if (r.deficitKbps <= 1e-9) {
      r.status = 'ROUTED';
      r.reason = 'OK';
      r.upgraded = true;
      res.upgradedToFull++;
    }
    const cap0 = Math.min(...Array.from(r.pathEdges).map((e) => capKbps[e]));
    r.utilMaxPct = cap0 > 0 ? round(((r.grantedKbps || 0) / cap0) * 100, 1) : r.utilMaxPct;
  }
  // 2) повторная маршрутизация отклонённых по ёмкости
  for (const k of order) {
    const r = routeResults[k];
    if (!r || r.status !== 'REJECTED' || r.reason !== 'CAPACITY_SHORTAGE') continue;
    const d = demands[k];
    const s = ctx.nodeIndexGet ? ctx.nodeIndexGet(d.from) : -1;
    const t = ctx.nodeIndexGet ? ctx.nodeIndexGet(d.to) : -1;
    if (s < 0 || t < 0) continue;
    const maxDelayMs = Math.min(d.maxDelayMs ?? C.maxDelayMs, C.maxDelayMs);
    const maxHops = Math.min(d.maxHops ?? C.maxHops, C.maxHops);
    const rr = router.route(s, t, d.rateKbps || 0, { maxDelayMs, maxHops });
    if (rr.rejected || rr.grantedKbps <= 1e-9) continue;
    const marginal = router.marginalCost(rr.nodes, rr.edges);
    if (spent + res.spent + marginal > budget + 1e-9) continue;
    router.commit(rr.nodes, rr.edges, rr.grantedKbps);
    res.spent += marginal;
    res.newlyRouted++;
    res.extraGrantedKbps += rr.grantedKbps;
    const full = rr.grantedKbps >= (d.rateKbps || 0) - 1e-9;
    routeResults[k] = {
      status: full ? 'ROUTED' : 'PARTIAL',
      reason: full ? 'OK' : 'CAPACITY_SHORTAGE',
      grantedKbps: round(rr.grantedKbps, 3),
      deficitKbps: round(Math.max(0, (d.rateKbps || 0) - rr.grantedKbps), 3),
      delayMs: round(rr.delayMs, 3),
      hops: rr.hops,
      marginDb: round(rr.marginDb, 2),
      availabilityPct: round(rr.availability * 100, 3),
      utilMaxPct: round(rr.utilMaxPct, 1),
      pathNodes: rr.nodes,
      pathEdges: rr.edges,
      cost: round(marginal, 2),
      upgraded: true,
      protected: !!d.protected,
    };
  }
  if (res.newlyRouted + res.upgradedToFull > 0)
    issues.push({ sev: 'info', code: 'IMPROVEMENTS_APPLIED', obj: { kind: 'net' }, title: 'Применены улучшения за остаток бюджета', text: `Модернизировано каналов: ${res.applied.length}, дополнительно обслужено заявок: ${res.newlyRouted + res.upgradedToFull}, стоимость ${round(res.spent, 2)} у.е.` });
  return res;
}

/** Итоговые узкие места: только те заявки, которые ДЕЙСТВИТЕЛЬНО недообслужены. */
function collectCapacityRelaxations(demands, routeResults) {
  const out = [];
  for (let k = 0; k < demands.length; k++) {
    const r = routeResults[k];
    if (!r) continue;
    const deficit = r.deficitKbps || 0;
    if (deficit <= 1e-9) continue;
    if (r.status === 'PARTIAL' && r.pathEdges) {
      for (const e of r.pathEdges) out.push({ edge: e, deficitKbps: deficit });
    } else if (r.status === 'REJECTED' && r.reason === 'CAPACITY_SHORTAGE' && r.bestEffort && r.bestEffort.bottleneckEdge >= 0) {
      out.push({ edge: r.bestEffort.bottleneckEdge, deficitKbps: deficit || demands[k].rateKbps || 0 });
    }
  }
  return out;
}

function ctx2Knap(improvements) {
  if (!improvements) return 'not_run';
  if (improvements.skipped) return `skipped:${improvements.skipped}`;
  return improvements.knapsack.exact ? 'exact_dp' : 'greedy_improved';
}

/**
 * Перекрёстная проверка построенного плана полным ядром ENG6:
 * из результата планирования собирается сценарий ENG6 и прогоняется через
 * analyzeScenario (профиль рельефа, цепочка моделей, РЭБ, живучесть, рейтинг).
 * Выполняется только на малых экземплярах — см. SIZE_GUARD.
 */
function runEng6CrossCheck({ g, edgeActive, orig, table, linkOrigin, nodes, nt, env, request, E, quality }) {
  const activeNodes = [];
  const keepNode = new Uint8Array(nodes.length);
  for (let i = 0; i < g.m; i++) {
    if (!edgeActive[i]) continue;
    keepNode[g.edgeFrom[i]] = 1;
    keepNode[g.edgeTo[i]] = 1;
  }
  for (let i = 0; i < nodes.length; i++) if (keepNode[i]) activeNodes.push(i);
  const idxMap = new Map(activeNodes.map((i, k) => [i, k]));
  const scNodes = activeNodes.map((i) => ({
    id: nodes[i].id,
    type: nodes[i].type,
    name: nodes[i].name || nodes[i].id,
    lat: round6(nt.lat[i]),
    lng: round6(nt.lng[i]),
    status: 'active',
    txPowerDbm: nt.txPowerDbm[i],
    txGainDbi: nt.txGainDbi[i],
    rxGainDbi: nt.rxGainDbi[i],
    rxSensDbm: nt.rxSensDbm[i],
    freqMhz: nodes[i].freqMhz || 150,
    antHeightM: nt.antHeightM[i],
    feederLossDb: nt.feederLossDb[i],
    noiseFigureDb: nt.noiseFigureDb[i],
    mobility: nodes[i].mobility || 'static',
    parentId: nodes[i].parentId ?? null,
    tags: [],
    note: '',
  }));
  const scLinks = [];
  for (let i = 0; i < g.m; i++) {
    if (!edgeActive[i]) continue;
    const e = orig[i];
    const src = linkOrigin[e] || {};
    scLinks.push({
      id: src.id || `l${e}`,
      from: nodes[g.edgeFrom[i]].id,
      to: nodes[g.edgeTo[i]].id,
      type: src.type || Object.keys(LINK_TYPE_IX)[table.typeIdx[e]] || 'VHF',
      status: 'active',
      priority: src.priority ?? 2,
      isBackup: false,
      tags: [],
      reqSnrDb: src.reqSnrDb ?? '',
      fhss: src.fhss ?? 0,
      dsss: src.dsss ?? 0,
      freqMhz: table.freqMhz[e] || src.freqMhz || 150,
      bwKhz: table.bwKhz[e] || src.bwKhz || 25,
      mod: src.mod || 'QPSK',
      fec: src.fec || 'none',
    });
  }
  // ВАЖНО: метаданные времени фиксируются. ENG6.analyzeScenario берёт час из
  // meta.datetime и подставляет его в состояние ионосферы — без фиксации два
  // идентичных запроса в разные минуты давали бы разные ответы (нарушение
  // NFR-DET-01, обнаружено тестом «Детерминированность»).
  const CROSSCHECK_EPOCH = '2026-01-16T00:00:00.000Z';
  const crossDatetime = (request.meta && typeof request.meta.datetime === 'string' && request.meta.datetime) || '2026-01-16T12:00';
  const scenario = {
    meta: { id: 'sc-eng6-crosscheck', name: (request.meta && request.meta.name) || 'ENG6-PLAN', version: '1.0.0', createdAt: CROSSCHECK_EPOCH, updatedAt: CROSSCHECK_EPOCH, author: (request.meta && request.meta.author) || '', description: 'Автоматическая перекрёстная проверка плана ENG6-PLAN', datetime: crossDatetime, counters: { node: scNodes.length, link: scLinks.length, threat: (request.threats || []).length } },
    environment: env,
    nodes: scNodes,
    links: scLinks,
    threats: request.threats || [],
  };
  const eng6Issues = E.validateScenario(scenario);
  const A = E.analyzeScenario(scenario, { quality });
  const cmp = {
    connectivityPct: { planner: null, eng6: round(A.network.connectivityPct, 3), deltaPp: null },
    activeLinks: { planner: scLinks.length, eng6: A.network.activeLinks, delta: scLinks.length - A.network.activeLinks },
    grade: A.ratings.grade,
    overall: round(A.ratings.overall, 2),
  };
  return {
    skipped: false,
    quality,
    comparison: cmp,
    verdict: cmp.activeLinks.eng6 === 0 ? 'REJECTED_BY_ENG6' : cmp.activeLinks.delta > 0 ? 'DEGRADED' : 'CONSISTENT',
    validationIssues: eng6Issues.slice(0, 20),
    ratings: A.ratings,
    network: {
      connectivityPct: round(A.network.connectivityPct, 3),
      componentsCount: A.network.componentsCount,
      activeLinks: A.network.activeLinks,
      avgPathLen: round(A.network.avgPathLen ?? 0, 3),
      diameter: A.network.diameter,
      practicalTotalKbps: round(A.network.practicalTotalKbps ?? 0, 2),
      minCutKbps: A.network.minCutKbps,
      spof: A.network.singlePointsOfFailure.length,
      bridges: A.network.bridges.length,
      overloaded: (A.network.routes && A.network.routes.overloaded ? A.network.routes.overloaded.length : 0),
    },
    issues: (A.issues || []).slice(0, 40).map((i) => ({ sev: i.sev, title: i.title, text: i.text, obj: i.obj })),
    recommendations: (A.recs || []).slice(0, 20).map((r) => ({ id: r.id, title: r.title, text: r.text })),
    // первая строка assumptions у ENG6 содержит локальную метку времени —
    // заменяется стабильной, иначе ответ недетерминирован
    assumptions: (A.assumptions || []).map((a, i) => (i === 0 ? 'Расчёт выполнен: метка времени нормализована (NFR-DET-01)' : a)),
    models: A.models,
  };
}

/** Публичная convenience-обёртка. */
export function planScenario(request) {
  return plan(request);
}

export { conformance, REJECT, REJECT_TEXT };
