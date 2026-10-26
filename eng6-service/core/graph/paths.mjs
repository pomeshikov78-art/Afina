/**
 * core/graph/paths.mjs — кратчайшие пути.
 *
 * Реализовано:
 *  • dijkstra            — SSSP с ранним выходом (target), возврат dist[] и prev[];
 *  • dijkstraLimited     — Дейкстра с ограничением по «второму» ресурсу
 *                          (R-constrained shortest path, алгоритм Хэндлера–Занга
 *                          в упрощённой label-setting версии: метка (cost, delay));
 *  • hopCount            — BFS по числу хопов;
 *  • pathFromPrev        — восстановление пути.
 *
 * Все функции чистые: не мутируют граф, возвращают новые типизированные массивы.
 */
import { MinHeap } from './heap.mjs';

export const INF = Infinity;

/**
 * @param {import('./csr.mjs').CsrGraph} g
 * @param {number} src
 * @param {{target?:number, maxDist?:number, weight?:Float64Array|null}} [opts]
 * @returns {{dist: Float64Array, prevArc: Int32Array}}
 */
export function dijkstra(g, src, opts = {}) {
  const n = g.n;
  const dist = new Float64Array(n).fill(INF);
  const prevArc = new Int32Array(n).fill(-1);
  const done = new Uint8Array(n);
  const heap = new MinHeap(1024);
  const W = opts.weight || g.w;
  const target = opts.target === undefined ? -1 : opts.target;
  const maxDist = opts.maxDist === undefined ? INF : opts.maxDist;
  dist[src] = 0;
  heap.push(0, src);
  while (!heap.empty) {
    const [d, u] = heap.pop();
    if (done[u]) continue;
    done[u] = 1;
    if (u === target) break;
    if (d > maxDist) break;
    for (let a = g.offset[u], end = g.offset[u + 1]; a < end; a++) {
      const v = g.to[a];
      if (done[v]) continue;
      const nd = d + W[a];
      if (nd < dist[v]) {
        dist[v] = nd;
        prevArc[v] = a;
        heap.push(nd, v);
      }
    }
  }
  return { dist, prevArc };
}

/** Восстановить путь (список узлов) и список дуг по prevArc. */
export function pathFromPrev(g, prevArc, src, dst) {
  if (src === dst) return { nodes: [src], arcs: [] };
  if (prevArc[dst] < 0) return null;
  const arcs = [];
  const nodes = [dst];
  let cur = dst;
  let guard = 0;
  while (cur !== src) {
    const a = prevArc[cur];
    if (a < 0) return null;
    arcs.push(a);
    // дуга a входит в cur; её хвост — сосед узла cur в обратном направлении
    const tail = arcTail(g, a, cur);
    nodes.push(tail);
    cur = tail;
    if (++guard > g.n + 1) return null; // защита от цикла при повреждённых данных
  }
  nodes.reverse();
  arcs.reverse();
  return { nodes, arcs };
}

/**
 * Хвост дуги a при известной голове v.
 * Для неориентированного CSR — O(1) через twin[]; для ориентированного —
 * линейный поиск (используется только в тестах).
 */
export function arcTail(g, a, v) {
  if (!g.directed && g.twin) return g.to[g.twin[a]];
  const eid = g.edgeId[a];
  for (let b = g.offset[v], end = g.offset[v + 1]; b < end; b++) {
    if (g.edgeId[b] === eid && g.to[b] !== v) return g.to[b];
  }
  for (let u = 0; u < g.n; u++) for (let b = g.offset[u]; b < g.offset[u + 1]; b++) if (b === a) return u;
  return -1;
}

/**
 * Ограниченный по ресурсу кратчайший путь (задержка ≤ maxDelay) с минимизацией
 * стоимости. Label-setting с меткой (cost, delay) и доминированием:
 * метка L1 доминирует L2, если cost1 ≤ cost2 и delay1 ≤ delay2.
 *
 * Это приближение RCSP (не гарантирует оптимальность при сильной корреляции
 * ресурсов, но детерминировано и линейно по числу недоминированных меток).
 * Ограничение maxLabels на узел защищает от экспоненциального разрастания —
 * при его достижении возвращается статус LABEL_LIMIT (см. planner.mjs).
 *
 * @returns {{dist: Float64Array, delay: Float64Array, prevArc: Int32Array, labels: number, truncated: boolean}|null}
 */
export function dijkstraLimited(g, src, dst, opts) {
  const { cost, delay, maxDelay, maxLabels = 32 } = opts;
  const n = g.n;
  const STRIDE = 64; // ≥ maxLabels; кодируем (node·STRIDE + labelIdx) в Int32 значении кучи
  if (maxLabels > STRIDE) throw new Error('dijkstraLimited: maxLabels > STRIDE(64)');
  const labels = new Array(n);
  const heap = new MinHeap(1024);
  let totalLabels = 1;
  let truncated = false;
  labels[src] = [{ cost: 0, delay: 0, prevArc: -1, prevNode: -1, prevLabel: -1 }];
  heap.push(0, src * STRIDE);
  while (!heap.empty) {
    const [, code] = heap.pop();
    const u = (code / STRIDE) | 0;
    const li = code % STRIDE;
    const list = labels[u];
    if (!list) continue;
    const lab = list[li];
    if (!lab) continue; // метка была удалена как доминируемая
    if (u === dst) {
      const arcs = [];
      let node = u;
      let idx = li;
      let guard = 0;
      while (node >= 0 && idx >= 0 && guard++ <= totalLabels + 1) {
        const L = labels[node] && labels[node][idx];
        if (!L) break;
        if (L.prevArc >= 0) arcs.push(L.prevArc);
        node = L.prevNode;
        idx = L.prevLabel;
      }
      arcs.reverse();
      return { cost: lab.cost, delay: lab.delay, arcs, labels: totalLabels, truncated };
    }
    for (let a = g.offset[u], end = g.offset[u + 1]; a < end; a++) {
      const v = g.to[a];
      const nc = lab.cost + cost[a];
      const nd = lab.delay + delay[a];
      if (!Number.isFinite(nc) || nd > maxDelay) continue;
      const vl = labels[v] || (labels[v] = []);
      let dominated = false;
      for (let i = 0; i < vl.length; i++) {
        const L = vl[i];
        if (!L) continue;
        if (L.cost <= nc + 1e-12 && L.delay <= nd + 1e-12) {
          dominated = true;
          break;
        }
        if (nc <= L.cost - 1e-12 && nd <= L.delay - 1e-12) vl[i] = null;
      }
      if (dominated) continue;
      // ищем свободный слот (на месте удалённых доминированием меток)
      let newIdx = -1;
      let alive = 0;
      for (let i = 0; i < vl.length; i++) {
        if (!vl[i]) {
          if (newIdx < 0) newIdx = i;
        } else alive++;
      }
      if (alive >= maxLabels) {
        truncated = true;
        continue;
      }
      if (newIdx < 0) {
        if (vl.length >= STRIDE) {
          truncated = true;
          continue;
        }
        newIdx = vl.length;
      }
      vl[newIdx] = { cost: nc, delay: nd, prevArc: a, prevNode: u, prevLabel: li };
      totalLabels++;
      heap.push(nc, v * STRIDE + newIdx);
    }
  }
  return null;
}

/** BFS: число хопов от src до всех узлов (-1 = недостижим). */
export function hopCount(g, src) {
  const n = g.n;
  const hops = new Int32Array(n).fill(-1);
  const q = new Int32Array(n);
  let head = 0;
  let tail = 0;
  hops[src] = 0;
  q[tail++] = src;
  while (head < tail) {
    const u = q[head++];
    for (let a = g.offset[u], end = g.offset[u + 1]; a < end; a++) {
      const v = g.to[a];
      if (hops[v] < 0) {
        hops[v] = hops[u] + 1;
        q[tail++] = v;
      }
    }
  }
  return hops;
}
