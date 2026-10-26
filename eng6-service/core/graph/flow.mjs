/**
 * core/graph/flow.mjs — максимальный поток (Dinic), минимальный разрез,
 * реберно-непересекающиеся пути.
 *
 * Применение в ENG6-PLAN:
 *  • maxFlowUndirected   — пропускная способность минимального разреза между КП
 *                          (метрика живучести; сверяется с ENG6.NET.maxFlow);
 *  • twoEdgeDisjointPaths — резервирование защищённых заявок. По теореме Менгера
 *                          два реберно-непересекающихся пути существуют ⇔ поток
 *                          величины 2 при единичных ёмкостях.
 *
 * Особенности реализации:
 *  1) дуги хранятся парами (2i, 2i+1) — обратная дуга = a^1, O(1);
 *  2) DFS блокирующего потока — ИТЕРАТИВНЫЙ (глубина может достигать n=10^5,
 *     рекурсия пробила бы стек V8);
 *  3) неориентированное ребро с ёмкостью c моделируется subdivision-узлом:
 *     u → w_e → v, обе дуги ёмкостью c. Это гарантирует, что суммарный поток
 *     через ребро ≤ c в обе стороны (две встречные дуги такой гарантии не дают);
 *  4) все ёмкости — Float64 (кбит/с), сравнение с ε = 1e-9.
 */
const EPS = 1e-9;

export class DinicFlow {
  /**
   * @param {number} n число узлов
   * @param {{from:number,to:number,cap:number}[]} edges ориентированные дуги
   */
  constructor(n, edges) {
    this.n = n;
    this.m = edges.length;
    const M = 2 * this.m;
    this.to = new Int32Array(M);
    this.cap = new Float64Array(M); // остаточная ёмкость (мутируется)
    this.next = new Int32Array(M);
    this.head = new Int32Array(n).fill(-1);
    this.srcCap = new Float64Array(M); // исходные ёмкости (для отчёта)
    for (let i = 0; i < this.m; i++) {
      const e = edges[i];
      const a = 2 * i;
      const b = a + 1;
      this.to[a] = e.to;
      this.cap[a] = e.cap;
      this.to[b] = e.from;
      this.cap[b] = 0;
      this.srcCap[a] = e.cap;
      this.srcCap[b] = 0;
      this.next[a] = this.head[e.from];
      this.head[e.from] = a;
      this.next[b] = this.head[e.to];
      this.head[e.to] = b;
    }
    this.level = new Int32Array(n);
    this.ptr = new Int32Array(n);
    this.total = 0;
  }

  _bfs(s, t) {
    const { level, to, cap, next, head, n } = this;
    level.fill(-1);
    const q = new Int32Array(n);
    let h = 0;
    let tl = 0;
    level[s] = 0;
    q[tl++] = s;
    while (h < tl) {
      const u = q[h++];
      for (let a = head[u]; a !== -1; a = next[a]) {
        if (cap[a] > EPS && level[to[a]] < 0) {
          level[to[a]] = level[u] + 1;
          q[tl++] = to[a];
        }
      }
    }
    return level[t] >= 0;
  }

  /**
   * Итеративный DFS блокирующего потока (Dinic).
   *
   * Инварианты:
   *   stackNode.length === stackArc.length + 1;
   *   stackArc[i] — дуга из stackNode[i] в stackNode[i+1];
   *   ptr[u] — первая непроверенная дуга узла u (current-arc heuristic).
   * Откат при насыщении дуги выполняется к её хвосту, при тупике — к
   * предыдущему узлу с одновременным исключением узла из слоистой сети
   * (level[u] = -1), что гарантирует конечность: каждая итерация либо
   * проталкивает поток, либо необратимо продвигает ptr/level.
   */
  _blockingFlow(s, t) {
    const { to, cap, next, level, ptr } = this;
    const stackArc = [];
    const stackNode = [s];
    let u = s;
    let pushed = 0;

    for (;;) {
      if (u === t) {
        let f = Infinity;
        let cut = -1;
        for (let i = 0; i < stackArc.length; i++) {
          const a = stackArc[i];
          if (cap[a] < f) {
            f = cap[a];
            cut = i;
          }
        }
        if (!Number.isFinite(f) || f <= EPS || cut < 0) break;
        for (let i = 0; i < stackArc.length; i++) {
          const a = stackArc[i];
          cap[a] -= f;
          cap[a ^ 1] += f;
        }
        pushed += f;
        stackArc.length = cut;
        stackNode.length = cut + 1;
        u = stackNode[cut];
        continue;
      }
      let a = ptr[u];
      while (a !== -1 && !(cap[a] > EPS && level[to[a]] === level[u] + 1)) a = next[a];
      ptr[u] = a;
      if (a === -1) {
        level[u] = -1; // узел больше не участвует в слоистой сети
        if (stackArc.length === 0) break;
        const pa = stackArc.pop();
        stackNode.pop();
        const pu = stackNode[stackNode.length - 1];
        ptr[pu] = next[pa];
        u = pu;
        continue;
      }
      const v = to[a];
      stackArc.push(a);
      stackNode.push(v);
      u = v;
    }
    return pushed;
  }

  maxFlow(s, t) {
    if (s === t) return 0;
    this.total = 0;
    while (this._bfs(s, t)) {
      for (let i = 0; i < this.n; i++) this.ptr[i] = this.head[i];
      this.total += this._blockingFlow(s, t);
    }
    return this.total;
  }

  /** Поток по дуге i (0..m-1). */
  flowOf(i) {
    return this.srcCap[2 * i] - this.cap[2 * i];
  }

  /** Достижимость из s в остаточной сети — для выделения минимального разреза. */
  minCutSide(s) {
    const seen = new Uint8Array(this.n);
    const q = [s];
    seen[s] = 1;
    while (q.length) {
      const u = q.shift();
      for (let a = this.head[u]; a !== -1; a = this.next[a]) {
        const v = this.to[a];
        if (this.cap[a] > EPS && !seen[v]) {
          seen[v] = 1;
          q.push(v);
        }
      }
    }
    return seen;
  }

  /** Разложение потока на s-t пути. @returns {{arcs:number[], nodes:number[], flow:number}[]} */
  decompose(s, t, maxPaths = 8) {
    const residual = Float64Array.from(this.cap);
    const paths = [];
    for (let p = 0; p < maxPaths; p++) {
      const prevArc = new Int32Array(this.n).fill(-1);
      const seen = new Uint8Array(this.n);
      const q = [s];
      seen[s] = 1;
      let found = false;
      while (q.length && !found) {
        const u = q.shift();
        for (let a = this.head[u]; a !== -1; a = this.next[a]) {
          const v = this.to[a];
          if (seen[v]) continue;
          if ((a & 1) === 1) continue; // только прямые дуги
          if (this.srcCap[a] - residual[a] > EPS) {
            seen[v] = 1;
            prevArc[v] = a;
            if (v === t) {
              found = true;
              break;
            }
            q.push(v);
          }
        }
      }
      if (!found) break;
      const arcs = [];
      let cur = t;
      let bottleneck = Infinity;
      let guard = 0;
      while (cur !== s && guard++ < this.n + 1) {
        const a = prevArc[cur];
        if (a < 0) break;
        arcs.push(a);
        bottleneck = Math.min(bottleneck, this.srcCap[a] - residual[a]);
        cur = this.to[a ^ 1];
      }
      if (cur !== s || arcs.length === 0) break;
      arcs.reverse();
      for (const a of arcs) residual[a] += bottleneck;
      const nodes = [s];
      for (const a of arcs) nodes.push(this.to[a]);
      paths.push({ arcs, nodes, flow: bottleneck });
    }
    return paths;
  }
}

/**
 * Построить потоковую сеть для НЕОРИЕНТИРОВАННОГО графа с subdivision-узлами.
 * Узлы 0..n-1 — исходные, узлы n..n+m-1 — «середины» рёбер.
 * @returns {{dinicEdges:{from:number,to:number,cap:number}[], edgeArcIndex:Int32Array, totalNodes:number}}
 */
export function buildUndirectedNetwork(g, capPerEdge) {
  const n = g.n;
  const m = g.m;
  const edges = [];
  const edgeArcIndex = new Int32Array(m);
  for (let e = 0; e < m; e++) {
    const u = g.edgeFrom[e];
    const v = g.edgeTo[e];
    const w = n + e;
    const c = capPerEdge ? capPerEdge[e] : g.cap[g.arcOf(e)] || 1;
    edgeArcIndex[e] = edges.length;
    edges.push({ from: u, to: w, cap: c });
    edges.push({ from: w, to: v, cap: c });
    edges.push({ from: v, to: w, cap: c });
    edges.push({ from: w, to: u, cap: c });
  }
  return { dinicEdges: edges, edgeArcIndex, totalNodes: n + m };
}

/**
 * Максимальный поток между s и t в неориентированном графе (ёмкости — кбит/с).
 */
export function maxFlowUndirected(g, s, t, capPerEdge) {
  const { dinicEdges, totalNodes } = buildUndirectedNetwork(g, capPerEdge);
  const d = new DinicFlow(totalNodes, dinicEdges);
  return d.maxFlow(s, t);
}

/**
 * Минимальный разрез: величина и множество рёбер разреза.
 */
export function minCutUndirected(g, s, t, capPerEdge) {
  const { dinicEdges, totalNodes } = buildUndirectedNetwork(g, capPerEdge);
  const d = new DinicFlow(totalNodes, dinicEdges);
  const value = d.maxFlow(s, t);
  const side = d.minCutSide(s);
  const cutEdges = [];
  for (let e = 0; e < g.m; e++) {
    const u = g.edgeFrom[e];
    const v = g.edgeTo[e];
    if (side[u] !== side[v]) cutEdges.push(e);
  }
  return { value, cutEdges: Int32Array.from(cutEdges) };
}

/**
 * Два реберно-непересекающихся пути между s и t (единичные ёмкости).
 * Точный алгоритм (поток = 2 ⇔ существуют, теорема Менгера).
 * @returns {{edgePaths:Int32Array[], nodePaths:Int32Array[]}|null}
 */
export function twoEdgeDisjointPaths(g, s, t) {
  const cap = new Float64Array(g.m).fill(1);
  const { dinicEdges, edgeArcIndex, totalNodes } = buildUndirectedNetwork(g, cap);
  const d = new DinicFlow(totalNodes, dinicEdges);
  const f = d.maxFlow(s, t);
  if (f < 2 - 1e-6) return null;
  const paths = d.decompose(s, t, 4);
  const n = g.n;
  const out = [];
  for (const p of paths) {
    const edgeList = [];
    const nodeList = [];
    for (const node of p.nodes) {
      if (node < n) nodeList.push(node);
      else {
        const e = node - n;
        if (edgeList[edgeList.length - 1] !== e) edgeList.push(e);
      }
    }
    out.push({ edges: Int32Array.from(edgeList), nodes: Int32Array.from(nodeList) });
  }
  if (out.length < 2) return null;
  // верификация непересекаемости (защита от ошибок разложения потока)
  const seen = new Set();
  for (const p of out.slice(0, 2)) for (const e of p.edges) if (seen.has(e)) return null; else seen.add(e);
  return { edgePaths: [out[0].edges, out[1].edges], nodePaths: [out[0].nodes, out[1].nodes] };
}
