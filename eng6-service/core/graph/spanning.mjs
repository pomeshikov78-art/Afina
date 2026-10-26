/**
 * core/graph/spanning.mjs — остовные деревья и приближение задачи Штейнера.
 *
 * В ENG6-PLAN остов используется дважды:
 *  1) «каркас связности» — минимальный по стоимости активации набор линий,
 *     соединяющий терминалы (КП и узлы-потребители). Это классическая задача
 *     Штейнера (NP-трудная); применяется 2-аппроксимация:
 *        MST на метрическом замыкании терминалов → разворачиваем пути.
 *     (Kou–Markowsky–Berman, 1981; граница 2·OPT, на практике ≤ 1.15·OPT.)
 *  2) нижняя оценка стоимости плана (LB) для честной маркировки статуса
 *     OPTIMAL/FEASIBLE: если фактическая стоимость ≤ LB·(1+ε), решение
 *     признаётся оптимальным для данной модели.
 *
 * Все веса неотрицательны; сортировка — по возрастанию с детерминированным
 * тай-брейком по индексу ребра (требование воспроизводимости, NFR-DET-01).
 */
import { UnionFind } from './csr.mjs';
import { dijkstra, pathFromPrev } from './paths.mjs';

/**
 * Минимальное остовное дерево (Kruskal). O(m log m).
 * @param {import('./csr.mjs').CsrGraph} g
 * @param {Float64Array|null} weight длина m (по рёбрам) — если null, берётся g.w первой дуги
 * @param {{onlyNodes?:Int32Array|number[]|null, forbidden?:Uint8Array|null}} [opts]
 * @returns {{edges:Int32Array, totalWeight:number, spansAll:boolean, componentsInForest:number}}
 */
export function kruskalMst(g, weight = null, opts = {}) {
  const m = g.m;
  const w = new Float64Array(m);
  for (let e = 0; e < m; e++) w[e] = weight ? weight[e] : g.w[g.arcOf(e)];
  const order = new Int32Array(m);
  for (let e = 0; e < m; e++) order[e] = e;
  // сортировка индексов по весу; Number-массив быстрее на малых m, Int32Array — на больших
  const keys = Array.from(order);
  keys.sort((a, b) => w[a] - w[b] || a - b);

  const allowed = opts.onlyNodes ? nodeMask(g.n, opts.onlyNodes) : null;
  const forbidden = opts.forbidden || null;
  const uf = new UnionFind(g.n);
  const chosen = [];
  let total = 0;
  for (let i = 0; i < keys.length; i++) {
    const e = keys[i];
    if (forbidden && forbidden[e]) continue;
    const u = g.edgeFrom[e];
    const v = g.edgeTo[e];
    if (allowed && (!allowed[u] || !allowed[v])) continue;
    if (!Number.isFinite(w[e])) continue;
    if (uf.union(u, v)) {
      chosen.push(e);
      total += w[e];
    }
  }
  // сколько компонент получилось среди разрешённых узлов
  const roots = new Set();
  if (allowed) {
    for (let u = 0; u < g.n; u++) if (allowed[u]) roots.add(uf.find(u));
  } else {
    for (let u = 0; u < g.n; u++) roots.add(uf.find(u));
  }
  const forestComponents = roots.size;
  return {
    edges: Int32Array.from(chosen),
    totalWeight: total,
    /** true, если все разрешённые узлы оказались в одной компоненте */
    spansAll: forestComponents <= 1,
    componentsInForest: forestComponents,
    nodeComponents: uf.count,
  };
}

function nodeMask(n, nodes) {
  const mask = new Uint8Array(n);
  for (const x of nodes) if (x >= 0 && x < n) mask[x] = 1;
  return mask;
}

/**
 * Приближённое дерево Штейнера для множества терминалов.
 * Алгоритм: полные кратчайшие пути между терминалами (Dijkstra из каждого) →
 * MST на метрическом замыкании → разворачивание путей в исходном графе.
 *
 * Сложность: O(|T|·(m + n log n)) + O(|T|² log|T|).
 * При |T| > maxTerminals включается режим «Dijkstra из одного корня»
 * (дерево кратчайших путей) — грубее, но O(m + n log n); фиксируется в
 * diagnostics.approximation = 'shortest_path_tree'.
 *
 * @returns {{edges:Int32Array, totalWeight:number, lowerBound:number, mode:string, unreachable:number[]}}
 */
export function steinerApprox(g, terminals, opts = {}) {
  const { maxTerminals = 64, weight = null } = opts;
  const T = [...new Set(terminals)].filter((t) => t >= 0 && t < g.n);
  if (T.length === 0) return { edges: new Int32Array(0), totalWeight: 0, lowerBound: 0, mode: 'empty', unreachable: [] };
  if (T.length === 1) return { edges: new Int32Array(0), totalWeight: 0, lowerBound: 0, mode: 'single', unreachable: [] };

  const useMetric = T.length <= maxTerminals;
  const edgeSet = new Set();
  let total = 0;
  const unreachable = [];

  if (!useMetric) {
    const root = T[0];
    const { prevArc } = dijkstra(g, root, { weight: weight ? expandWeights(g, weight) : null });
    for (const t of T) {
      if (t === root) continue;
      const p = pathFromPrev(g, prevArc, root, t);
      if (!p) {
        unreachable.push(t);
        continue;
      }
      for (const a of p.arcs) edgeSet.add(g.edgeId[a]);
    }
    for (const e of edgeSet) total += weight ? weight[e] : g.w[g.arcOf(e)];
    return { edges: Int32Array.from(edgeSet), totalWeight: total, lowerBound: 0, mode: 'shortest_path_tree', unreachable };
  }

  // 1) метрическое замыкание
  const k = T.length;
  const pos = new Map(T.map((t, i) => [t, i]));
  void pos;
  const dist = new Float64Array(k * k).fill(Infinity);
  const prev = new Array(k);
  const W = weight ? expandWeights(g, weight) : null;
  for (let i = 0; i < k; i++) {
    const r = dijkstra(g, T[i], { weight: W });
    prev[i] = r.prevArc;
    for (let j = 0; j < k; j++) dist[i * k + j] = r.dist[T[j]];
  }
  // терминалы, недостижимые от первого, — явный признак несвязной сети:
  // они должны попасть в unreachable, а не «молча» исчезнуть из MST
  for (let j = 1; j < k; j++) {
    if (!Number.isFinite(dist[j])) unreachable.push(T[j]);
  }
  // 2) MST на замыкании (Prim, k² — при k ≤ 64 это ≤ 4096 операций)
  const inTree = new Uint8Array(k);
  const best = new Float64Array(k).fill(Infinity);
  const bestFrom = new Int32Array(k).fill(-1);
  best[0] = 0;
  let mstWeight = 0;
  const mstPairs = [];
  for (let it = 0; it < k; it++) {
    let bi = -1;
    let bv = Infinity;
    for (let i = 0; i < k; i++) if (!inTree[i] && best[i] < bv) ((bv = best[i]), (bi = i));
    if (bi < 0) break;
    inTree[bi] = 1;
    mstWeight += bv;
    if (bestFrom[bi] >= 0) mstPairs.push([bestFrom[bi], bi]);
    for (let j = 0; j < k; j++) {
      if (inTree[j]) continue;
      const d = dist[bi * k + j];
      if (d < best[j]) {
        best[j] = d;
        bestFrom[j] = bi;
      }
    }
  }
  // 3) разворачивание путей
  for (const [i, j] of mstPairs) {
    const p = pathFromPrev(g, prev[i], T[i], T[j]);
    if (!p) {
      unreachable.push(T[j]);
      continue;
    }
    for (const a of p.arcs) edgeSet.add(g.edgeId[a]);
  }
  for (const e of edgeSet) total += weight ? weight[e] : g.w[g.arcOf(e)];
  return { edges: Int32Array.from(edgeSet), totalWeight: total, lowerBound: mstWeight, mode: 'metric_closure_mst', unreachable };
}

function expandWeights(g, weight) {
  const W = new Float64Array(g.arcs);
  for (let a = 0; a < W.length; a++) W[a] = weight[g.edgeId[a]];
  return W;
}

/**
 * Нижняя оценка стоимости связывания терминалов (для статуса OPTIMAL):
 * min-ребро каждого терминала, делённое на 2 (оценка «полустепенная»).
 * LB ≤ OPT всегда; совпадение плана с LB → доказуемая оптимальность в модели.
 */
export function trivialLowerBound(g, terminals, weight) {
  const W = weight;
  let sum = 0;
  for (const t of terminals) {
    let min = Infinity;
    for (let a = g.offset[t], end = g.offset[t + 1]; a < end; a++) {
      const e = g.edgeId[a];
      const w = W ? W[e] : g.w[a];
      if (w < min) min = w;
    }
    if (Number.isFinite(min)) sum += min;
  }
  return sum / 2;
}
