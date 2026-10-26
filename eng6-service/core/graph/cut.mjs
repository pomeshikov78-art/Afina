/**
 * core/graph/cut.mjs — компоненты связности, мосты, точки сочленения.
 *
 * Все обходы итеративные (без рекурсии): на графе 10^5 узлов рекурсивный DFS
 * пробивает стек V8 (~10 000 кадров).
 *
 * Назначение в ENG6-PLAN:
 *  • components      — диагностика «сеть несвязна» (требование Критика);
 *  • bridges         — линии-мосты: единственное соединение, SPOF по каналу;
 *  • articulations   — узлы, отказ которых разрывает сеть, SPOF по узлу.
 * Результаты сверяются с ENG6.NET.criticalLinks / criticalNodes в
 * tests/unit/graph_vs_eng6.test.mjs — защита от расхождения двух реализаций.
 */

/**
 * Компоненты связности (итеративный DFS).
 * @returns {{count:number, comp:Int32Array, sizes:Int32Array, largest:number}}
 */
export function components(g) {
  const n = g.n;
  const comp = new Int32Array(n).fill(-1);
  const stack = new Int32Array(n);
  const sizes = [];
  let c = 0;
  let largest = 0;
  for (let s = 0; s < n; s++) {
    if (comp[s] >= 0) continue;
    let size = 0;
    let top = 0;
    stack[top++] = s;
    comp[s] = c;
    while (top > 0) {
      const u = stack[--top];
      size++;
      for (let a = g.offset[u], end = g.offset[u + 1]; a < end; a++) {
        const v = g.to[a];
        if (comp[v] < 0) {
          comp[v] = c;
          stack[top++] = v;
        }
      }
    }
    sizes.push(size);
    if (size > largest) largest = size;
    c++;
  }
  return { count: c, comp, sizes: Int32Array.from(sizes), largest };
}

/**
 * Доля попарно достижимых узлов, %. Та же метрика, что connectivityPct в ENG6:
 *   Σ C(|S_i|,2) / C(|V|,2) · 100
 */
export function connectivityPct(g) {
  if (g.n <= 1) return g.n === 1 ? 100 : 0;
  const { sizes } = components(g);
  let pairs = 0;
  for (let i = 0; i < sizes.length; i++) pairs += (sizes[i] * (sizes[i] - 1)) / 2;
  const total = (g.n * (g.n - 1)) / 2;
  return total ? (pairs / total) * 100 : 0;
}

/**
 * Мосты и точки сочленения — Tarjan, итеративно.
 * Корректность на мультиграфе обеспечивает twin[]: обратной считается только
 * парная дуга, поэтому два параллельных ребра между узлами мостом не являются.
 *
 * @param {import('./csr.mjs').CsrGraph} g неориентированный граф
 * @returns {{bridgeEdges:Int32Array, articulations:Int32Array, low:Int32Array, disc:Int32Array}}
 *   bridgeEdges — индексы рёбер (0..m-1) в терминах g.
 */
export function bridgesAndArticulations(g) {
  if (g.directed) throw new Error('bridgesAndArticulations: только неориентированный граф');
  const n = g.n;
  const disc = new Int32Array(n).fill(-1);
  const low = new Int32Array(n);
  const parentArc = new Int32Array(n).fill(-1);
  const isArt = new Uint8Array(n);
  const bridgeEdges = [];
  let time = 0;

  const stNode = new Int32Array(n + 1);
  const stArc = new Int32Array(n + 1);

  for (let root = 0; root < n; root++) {
    if (disc[root] >= 0) continue;
    disc[root] = low[root] = time++;
    let rootChildren = 0;
    let sp = 0;
    stNode[sp] = root;
    stArc[sp] = g.offset[root];
    sp++;
    while (sp > 0) {
      const u = stNode[sp - 1];
      const a = stArc[sp - 1];
      if (a < g.offset[u + 1]) {
        stArc[sp - 1] = a + 1;
        const v = g.to[a];
        if (disc[v] < 0) {
          parentArc[v] = a;
          disc[v] = low[v] = time++;
          if (u === root) rootChildren++;
          stNode[sp] = v;
          stArc[sp] = g.offset[v];
          sp++;
        } else if (a !== g.twin[parentArc[u]]) {
          // обратная дуга (не парная к родительской)
          if (disc[v] < low[u]) low[u] = disc[v];
        }
      } else {
        sp--;
        if (sp > 0) {
          const p = stNode[sp - 1];
          const pa = parentArc[u];
          if (low[u] < low[p]) low[p] = low[u];
          if (low[u] > disc[p]) bridgeEdges.push(g.edgeId[pa]);
          if (p !== root && low[u] >= disc[p]) isArt[p] = 1;
        }
      }
    }
    if (rootChildren > 1) isArt[root] = 1;
  }

  const art = [];
  for (let i = 0; i < n; i++) if (isArt[i]) art.push(i);
  return {
    bridgeEdges: Int32Array.from(bridgeEdges).sort(),
    articulations: Int32Array.from(art),
    low,
    disc,
  };
}

/**
 * Мосты подграфа активных рёбер.
 * @param {import('./csr.mjs').CsrGraph} g
 * @param {Uint8Array|boolean[]} activeEdgeMask длина m
 * @returns {Int32Array} индексы рёбер-мостов в терминах g
 */
export function bridgesOfSubgraph(g, activeEdgeMask) {
  const sub = g.subgraph(activeEdgeMask);
  const { bridgeEdges } = bridgesAndArticulations(sub);
  // sub.edgeIds[i] = внешний id; sub.subIndex[i] = индекс ребра в g
  const out = new Int32Array(bridgeEdges.length);
  for (let i = 0; i < bridgeEdges.length; i++) out[i] = sub.subIndex[bridgeEdges[i]];
  return out;
}

/**
 * Доля узлов, теряющих связь при отказе узла u (%), — аналог lostPct из
 * ENG6.NET.criticalNodes, но O(V+E) суммарно через блоки двусвязности.
 * Для планировщика достаточно приближения по компонентам подграфа G−u;
 * при n > guard возвращается null (тяжёлый расчёт запрещён).
 */
export function nodeRemovalLoss(g, u, guard = 20000) {
  if (g.n > guard) return null;
  const n = g.n;
  const mask = new Uint8Array(g.m);
  for (let e = 0; e < g.m; e++) mask[e] = g.edgeFrom[e] !== u && g.edgeTo[e] !== u ? 1 : 0;
  const sub = g.subgraph(mask);
  const { sizes } = components(sub);
  let pairs = 0;
  for (let i = 0; i < sizes.length; i++) pairs += (sizes[i] * (sizes[i] - 1)) / 2;
  const nn = n - 1;
  const total = nn > 1 ? (nn * (nn - 1)) / 2 : 1;
  const connected = nn > 1 ? (pairs / total) * 100 : 100;
  return Math.max(0, Math.min(100, 100 - connected));
}
