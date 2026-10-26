/**
 * core/route.mjs — многопродуктовая маршрутизация заявок (multi-commodity flow).
 *
 * Постановка (docs/01_MATH_MODEL.md, п. 5.3):
 *   для каждой заявки k = (s_k, t_k, r_k, L_k, H_k) найти путь P_k в графе
 *   активных рёбер, минимизирующий
 *       Σ_{e∈P_k} (τ_e + τ_hop + ψ·u_e)
 *   при ограничениях
 *       Σ_k f_{k,e} ≤ C_e          (ёмкость ребра, кбит/с),
 *       Σ_{e∈P_k} τ_e ≤ L_k        (задержка),
 *       |P_k| − 1 ≤ H_k            (число хопов).
 * Точная задача — NP-трудная (несжимаемый многопродуктовый поток с целочисл.
 * активациями). Применяется последовательная жадная маршрутизация в порядке
 * приоритета с учётом остаточной ёмкости (successive shortest paths) —
 * стандартный промышленный подход; деградация фиксируется в статусе PARTIAL.
 *
 * Производительность: веса дуг пересчитываются НЕ на каждой заявке, а только
 * для рёбер, чья утилизация изменилась (дельта-обновление) — иначе на графе
 * 10^5 узлов × 2·10^4 заявок стоимость перевзвешивания превысила бы сам поиск.
 */
import { dijkstra, dijkstraLimited, pathFromPrev } from './graph/index.mjs';

/**
 * Размер кадра для оценки задержки сериализации, кбит.
 * По умолчанию 1 кбит ≈ 125 байт — типовой кадр тактической передачи данных.
 * Настраивается через constraints.frameKbit: на узкополосных каналах (2,4 кбит/с)
 * кадр в 1 кбит даёт 417 мс сериализации, что физически верно, но требует
 * либо большего лимита задержки, либо меньшего кадра.
 */
const DEFAULT_FRAME_KBIT = 1;

export class Router {
  /**
   * @param {import('./graph/csr.mjs').CsrGraph} g граф ДОПУСТИМЫХ рёбер
   * @param {object} p
   * @param {Float64Array} p.delayMs   задержка ребра, мс
   * @param {Float64Array} p.capKbps    ёмкость ребра (с учётом резерва), кбит/с
   * @param {Float64Array} p.marginDb   запас по уровню на ребре, дБ
   * @param {Float64Array} p.availability доступность ребра, 0..1
   * @param {Float64Array} p.cost       стоимость активации ребра, у.е.
   * @param {Float64Array} p.nodeCost   стоимость развертывания узла, у.е.
   * @param {Uint8Array}   p.edgeActive ребро уже активировано
   * @param {Uint8Array}   p.nodeActive узел уже развернут
   * @param {number} p.hopProcessingMs
   * @param {number} p.utilPenaltyMs штраф за утилизацию, мс на 100 %
   */
  constructor(g, p) {
    this.g = g;
    this.p = p;
    this.load = new Float64Array(g.m); // текущая нагрузка, кбит/с
    this.arcW = new Float64Array(g.arcs);
    this.edgeW = new Float64Array(g.m);
    this.activeArcsOfEdge = buildArcIndex(g);
    for (let e = 0; e < g.m; e++) this.recomputeEdge(e);
  }

  recomputeEdge(e) {
    const { capKbps, delayMs } = this.p;
    const cap = capKbps[e];
    const residual = cap - this.load[e];
    let w;
    if (residual <= 1e-9) w = Infinity;
    else {
      const util = cap > 0 ? this.load[e] / cap : 0;
      w = delayMs[e] + this.p.hopProcessingMs + this.p.utilPenaltyMs * util * util;
    }
    this.edgeW[e] = w;
    const arcs = this.activeArcsOfEdge[e];
    if (arcs) for (const a of arcs) this.arcW[a] = w;
  }

  /** Остаточная ёмкость ребра. */
  residual(e) {
    return this.p.capKbps[e] - this.load[e];
  }

  /**
   * Маршрутизация одной заявки.
   * @param {number} s
   * @param {number} t
   * @param {number} rateKbps
   * @param {{maxDelayMs:number, maxHops:number}} lim
   * @returns {null | {edges:Int32Array, nodes:Int32Array, delayMs:number, hops:number, grantedKbps:number,
   *                    bottleneckEdge:number, marginDb:number, availability:number, utilMaxPct:number,
   *                    bestEffort:{delayMs:number,hops:number}|null, reason:string}}
   */
  route(s, t, rateKbps, lim) {
    const g = this.g;
    if (s === t) return { edges: new Int32Array(0), nodes: Int32Array.from([s]), delayMs: 0, hops: 0, grantedKbps: rateKbps, bottleneckEdge: -1, marginDb: Infinity, availability: 1, utilMaxPct: 0, bestEffort: null, reason: 'SELF' };
    const { maxDelayMs, maxHops } = lim;

    // проход 1: минимизация задержки с учётом остаточной ёмкости
    const r1 = dijkstra(g, s, { target: t, weight: this.arcW });
    let p = Number.isFinite(r1.dist[t]) ? pathFromPrev(g, r1.prevArc, s, t) : null;
    let reason = p ? 'OK' : 'NO_PATH_CAPACITY';
    let bestEffort = null;

    if (!p) {
      // проход 2 (best-effort): ёмкость игнорируется — нужно понять, чем именно заблокирована заявка.
      // Веса без учёта ёмкости не зависят от нагрузки → кэшируются (иначе на 10^5 рёбер
      // и 10^3 отклонённых заявок выделили бы 10^8 Float64).
      const wNoCap = this.arcWNoCapacity();
      const r2 = dijkstra(g, s, { target: t, weight: wNoCap });
      if (Number.isFinite(r2.dist[t])) {
        const bp = pathFromPrev(g, r2.prevArc, s, t);
        if (bp) {
          const be = this.evaluatePath(bp.arcs, rateKbps, false);
          bestEffort = { delayMs: be.delayMs, hops: be.hops, deficitKbps: be.deficitKbps, bottleneckEdge: be.bottleneckEdge };
          reason = 'CAPACITY_SHORTAGE';
        }
      } else reason = 'NO_PATH';
    }

    if (p) {
      const ev = this.evaluatePath(p.arcs, rateKbps, true);
      if (ev.hops > maxHops || ev.delayMs > maxDelayMs) {
        // проход 3: ограничение по хопам (RCSP), минимизация взвешенной задержки
        const hopsArr = new Float64Array(g.arcs).fill(1);
        const limited = dijkstraLimited(g, s, t, { cost: this.arcW, delay: hopsArr, maxDelay: maxHops, maxLabels: 32 });
        if (limited) {
          const lp = { arcs: limited.arcs };
          const lev = this.evaluatePath(lp.arcs, rateKbps, true);
          if (lev.hops <= maxHops && lev.delayMs <= maxDelayMs) {
            p = { arcs: limited.arcs, nodes: nodesFromArcs(g, limited.arcs, s) };
            const finalReason = lev.grantedKbps < rateKbps - 1e-9 ? 'CAPACITY_SHORTAGE' : 'OK';
            return this.finish(s, t, p, rateKbps, finalReason, bestEffort, lev);
          }
          bestEffort = bestEffort || { delayMs: lev.delayMs, hops: lev.hops, deficitKbps: lev.deficitKbps, bottleneckEdge: lev.bottleneckEdge };
        }
        const evOk = ev;
        reason = evOk.hops > maxHops ? 'HOP_LIMIT' : 'DELAY_EXCEEDED';
        return {
          edges: edgesOfArcs(g, p.arcs),
          nodes: Int32Array.from(p.nodes),
          delayMs: evOk.delayMs,
          hops: evOk.hops,
          grantedKbps: 0,
          bottleneckEdge: evOk.bottleneckEdge,
          marginDb: evOk.marginDb,
          availability: evOk.availability,
          utilMaxPct: evOk.utilMaxPct,
          bestEffort,
          reason,
          rejected: true,
        };
      }
      const ev2 = this.evaluatePath(p.arcs, rateKbps, true);
      const finalReason = ev2.grantedKbps < rateKbps - 1e-9 ? 'CAPACITY_SHORTAGE' : 'OK';
      return this.finish(s, t, p, rateKbps, finalReason, bestEffort, ev2);
    }

    return { edges: new Int32Array(0), nodes: Int32Array.from([s, t]), delayMs: Infinity, hops: 0, grantedKbps: 0, bottleneckEdge: -1, marginDb: -Infinity, availability: 0, utilMaxPct: 0, bestEffort, reason, rejected: true };
  }

  finish(s, t, p, rateKbps, reason, bestEffort, precomputed = null) {
    const g = this.g;
    const ev = precomputed || this.evaluatePath(p.arcs, rateKbps, true);
    const edges = edgesOfArcs(g, p.arcs);
    return {
      edges,
      nodes: Int32Array.from(p.nodes),
      delayMs: ev.delayMs,
      hops: ev.hops,
      grantedKbps: ev.grantedKbps,
      deficitKbps: ev.deficitKbps,
      bottleneckEdge: ev.bottleneckEdge,
      marginDb: ev.marginDb,
      availability: ev.availability,
      utilMaxPct: ev.utilMaxPct,
      bestEffort,
      reason,
      rejected: ev.grantedKbps <= 1e-9,
    };
  }

  /**
   * Стоимость активации пути: сумма стоимостей ещё не активных рёбер и узлов.
   * Используется для контроля бюджета «на лету» (см. planner.mjs, фаза 5).
   */
  marginalCost(nodes, edges) {
    const { cost, nodeCost, edgeActive, nodeActive } = this.p;
    let c = 0;
    for (const e of edges) if (!edgeActive[e]) c += cost[e];
    for (const v of nodes) if (!nodeActive[v]) c += nodeCost[v];
    return c;
  }

  /** Зафиксировать заявку: выделить полосу, активировать рёбра/узлы. */
  commit(nodes, edges, grantedKbps) {
    const { edgeActive, nodeActive } = this.p;
    for (const e of edges) {
      this.load[e] += grantedKbps;
      if (!edgeActive[e]) {
        edgeActive[e] = 1;
      }
      this.recomputeEdge(e);
    }
    for (const v of nodes) if (!nodeActive[v]) nodeActive[v] = 1;
  }

  /** Отменить фиксацию (используется при откате из-за бюджета). */
  rollback(nodes, edges, grantedKbps) {
    for (const e of edges) {
      this.load[e] = Math.max(0, this.load[e] - grantedKbps);
      this.recomputeEdge(e);
    }
    void nodes;
  }

  arcWNoCapacity() {
    if (this._wNoCap) return this._wNoCap;
    const g = this.g;
    const W = new Float64Array(g.arcs);
    for (let e = 0; e < g.m; e++) {
      const w = this.p.delayMs[e] + this.p.hopProcessingMs;
      const arcs = this.activeArcsOfEdge[e];
      if (arcs) for (const a of arcs) W[a] = w;
    }
    this._wNoCap = W;
    return W;
  }

  /**
   * Оценка пути.
   *
   * Модель задержки (docs/01_MATH_MODEL.md, п. 3.4):
   *   T = Σ_{e∈P} (τ_e^prop + τ_hop) + T_ser + T_queue,
   *   T_ser   = frameKbit / R_granted · 1000   (один раз на маршрут),
   *   T_queue = Σ ρ_e/(1−ρ_e)·T_ser,e          (только при queueing='mm1').
   * M/M/1 по умолчанию ВЫКЛЮЧЕН: на узкополосных УКВ-каналах (32 кбит/с)
   * ρ→1 даёт десятки секунд «очереди», что физически означает не задержку,
   * а отказ обслуживания — это уже учтено ограничением ёмкости и статусом PARTIAL.
   *
   * @param {Int32Array|number[]} arcs
   * @param {number} rateKbps
   * @param {boolean|{enforceCapacity?:boolean, queueing?:'none'|'mm1'}} [opts]
   */
  evaluatePath(arcs, rateKbps, opts = true) {
    const g = this.g;
    const o = typeof opts === 'object' ? opts : { enforceCapacity: !!opts };
    const enforceCapacity = o.enforceCapacity !== false;
    const queueing = o.queueing || 'none';
    const { delayMs, capKbps, marginDb, availability, hopProcessingMs } = this.p;
    let prop = 0;
    let avail = 1;
    let margin = Infinity;
    let bottleneck = -1;
    let minResidual = Infinity;
    let utilMax = 0;
    let queue = 0;
    const edges = [];
    const seen = new Set();
    for (const a of arcs) {
      const e = g.edgeId[a];
      if (seen.has(e)) continue; // параллельные дуги одного ребра
      seen.add(e);
      edges.push(e);
      const cap = capKbps[e];
      const residual = cap - this.load[e];
      if (residual < minResidual) {
        minResidual = residual;
        bottleneck = e;
      }
      prop += delayMs[e] + hopProcessingMs;
      avail *= availability[e];
      if (marginDb[e] < margin) margin = marginDb[e];
    }
    const granted = enforceCapacity ? Math.max(0, Math.min(rateKbps, minResidual === Infinity ? rateKbps : minResidual)) : Math.max(0, rateKbps);
    // сериализация считается от ЗАПРОШЕННОЙ скорости: иначе при почти исчерпанной
    // ёмкости (granted → 0) задержка формально стремится к бесконечности и заявка
    // получает неверный диагноз DELAY_EXCEEDED вместо CAPACITY_SHORTAGE
    const frameKbit = this.p.frameKbit > 0 ? this.p.frameKbit : DEFAULT_FRAME_KBIT;
    const serialRate = Math.max(rateKbps, granted);
    if (granted > 0) {
      const serial = serialRate > 0 ? (frameKbit / serialRate) * 1000 : 0;
      if (queueing === 'mm1') {
        for (const e of edges) {
          const cap = capKbps[e];
          if (cap <= 0) continue;
          const rho = Math.min(0.95, (this.load[e] + granted) / cap);
          queue += (rho / (1 - rho)) * ((frameKbit / cap) * 1000);
        }
      }
      for (const e of edges) {
        const cap = capKbps[e];
        if (cap > 0) {
          const u = (this.load[e] + granted) / cap;
          if (u > utilMax) utilMax = u;
        }
      }
      return {
        delayMs: prop + serial + queue,
        serializationMs: serial,
        queueMs: queue,
        hops: edges.length,
        availability: avail,
        marginDb: margin === Infinity ? 0 : margin,
        bottleneckEdge: bottleneck,
        utilMaxPct: utilMax * 100,
        grantedKbps: granted,
        deficitKbps: Math.max(0, rateKbps - granted),
      };
    }
    return {
      delayMs: prop,
      serializationMs: 0,
      queueMs: 0,
      hops: edges.length,
      availability: avail,
      marginDb: margin === Infinity ? 0 : margin,
      bottleneckEdge: bottleneck,
      utilMaxPct: utilMax * 100,
      grantedKbps: 0,
      deficitKbps: Math.max(0, rateKbps),
    };
  }
}

function buildArcIndex(g) {
  const idx = new Array(g.m);
  for (let a = 0; a < g.arcs; a++) {
    const e = g.edgeId[a];
    if (!idx[e]) idx[e] = [];
    idx[e].push(a);
  }
  return idx;
}

function edgesOfArcs(g, arcs) {
  const out = [];
  const seen = new Set();
  for (const a of arcs) {
    const e = g.edgeId[a];
    if (!seen.has(e)) {
      seen.add(e);
      out.push(e);
    }
  }
  return Int32Array.from(out);
}

function nodesFromArcs(g, arcs, s) {
  const nodes = [s];
  for (const a of arcs) nodes.push(g.to[a]);
  return nodes;
}
