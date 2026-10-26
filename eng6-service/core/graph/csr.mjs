/**
 * core/graph/csr.mjs — граф в формате CSR (Compressed Sparse Row).
 *
 * Единственное представление графа в ядре ENG6-PLAN. Причины (см. ADR-002):
 *  1) предсказуемая память: 4·(n+1) + (4+4+8+8+4)·2m байт;
 *  2) кэш-локальность при обходе соседей (последовательные чтения);
 *  3) отсутствие миллионов мелких объектов → нет деградации GC на 10^5 узлов;
 *  4) возможность пересчитывать только веса (Float64Array) без перестройки
 *     структуры на каждой итерации многопродуктовой маршрутизации;
 *  5) явный массив twin[] (парная дуга) — обязателен для корректного Tarjan
 *     на мультиграфе: «обратной» считается именно парная дуга, а не любая
 *     дуга в того же ребро (иначе параллельные рёбра ломают поиск мостов).
 *
 * Граф неориентированный по топологии (радионаправление двустороннее),
 * каждое ребро хранится двумя дугами с общим edgeId — ёмкость считается
 * «на ребро», а не «на дугу».
 */
export class CsrGraph {
  /**
   * @param {number} n число узлов
   * @param {Int32Array} edgeFrom длина m
   * @param {Int32Array} edgeTo   длина m
   * @param {Float64Array|null} edgeW длина m — вес
   * @param {boolean} directed
   * @param {Int32Array|null} edgeIds длина m — внешние id рёбер (по умолчанию 0..m-1)
   */
  constructor(n, edgeFrom, edgeTo, edgeW = null, directed = false, edgeIds = null) {
    const m = edgeFrom.length;
    this.n = n;
    this.m = m;
    this.directed = directed;
    this.edgeIds = edgeIds ? Int32Array.from(edgeIds) : Int32Array.from({ length: m }, (_, i) => i);
    this.edgeFrom = Int32Array.from(edgeFrom);
    this.edgeTo = Int32Array.from(edgeTo);
    this.deg = new Int32Array(n);
    for (let e = 0; e < m; e++) {
      this.deg[edgeFrom[e]]++;
      if (!directed) this.deg[edgeTo[e]]++;
    }
    this.offset = new Int32Array(n + 1);
    for (let i = 0; i < n; i++) this.offset[i + 1] = this.offset[i] + this.deg[i];
    const arcs = this.offset[n];
    this.to = new Int32Array(arcs);
    this.edgeId = new Int32Array(arcs); // индекс ребра (0..m-1)
    this.extId = new Int32Array(arcs); // внешний id ребра
    this.twin = new Int32Array(arcs).fill(-1);
    this.w = new Float64Array(arcs); // вес (пересчитываемый)
    this.cap = new Float64Array(arcs); // пропускная способность, кбит/с (0 = ∞)
    const cur = Int32Array.from(this.offset.subarray(0, n));
    for (let e = 0; e < m; e++) {
      const u = edgeFrom[e];
      const v = edgeTo[e];
      const w = edgeW ? edgeW[e] : 1;
      const p = cur[u]++;
      this.to[p] = v;
      this.edgeId[p] = e;
      this.extId[p] = this.edgeIds[e];
      this.w[p] = w;
      if (!directed) {
        const q = cur[v]++;
        this.to[q] = u;
        this.edgeId[q] = e;
        this.extId[q] = this.edgeIds[e];
        this.w[q] = w;
        this.twin[p] = q;
        this.twin[q] = p;
      }
    }
  }

  get arcs() {
    return this.to.length;
  }

  /** Первая дуга ребра e (из edgeFrom[e]). */
  arcOf(e) {
    const u = this.edgeFrom[e];
    for (let a = this.offset[u], end = this.offset[u + 1]; a < end; a++) if (this.edgeId[a] === e) return a;
    return -1;
  }

  /** Хвост дуги a (узел, из которого она исходит). */
  tail(a) {
    return this.directed ? this._tailScan(a) : this.to[this.twin[a]];
  }

  _tailScan(a) {
    for (let u = 0; u < this.n; u++) if (a >= this.offset[u] && a < this.offset[u + 1]) return u;
    return -1;
  }

  /** Пересчитать веса всех дуг: weightFn(edgeIndex, arcIndex) → number. */
  reweight(weightFn) {
    const { edgeId, w } = this;
    for (let a = 0; a < w.length; a++) w[a] = weightFn(edgeId[a], a);
    return this;
  }

  /** Ёмкость ребра (кбит/с); 0 трактуется как «не ограничена». */
  setEdgeCapacity(e, capKbps) {
    const a = this.arcOf(e);
    if (a < 0) return this;
    this.cap[a] = capKbps;
    if (this.twin[a] >= 0) this.cap[this.twin[a]] = capKbps;
    return this;
  }

  degree(u) {
    return this.offset[u + 1] - this.offset[u];
  }

  /** Подграф по маске рёбер; внешние id сохраняются. */
  subgraph(mask) {
    const idx = [];
    for (let e = 0; e < this.m; e++) if (mask[e]) idx.push(e);
    const from = new Int32Array(idx.length);
    const to = new Int32Array(idx.length);
    const ids = new Int32Array(idx.length);
    const w = new Float64Array(idx.length);
    for (let i = 0; i < idx.length; i++) {
      const e = idx[i];
      from[i] = this.edgeFrom[e];
      to[i] = this.edgeTo[e];
      ids[i] = this.edgeIds[e];
      w[i] = this.w[this.arcOf(e)];
    }
    const sub = new CsrGraph(this.n, from, to, w, this.directed, ids);
    sub.subIndex = Int32Array.from(idx);
    return sub;
  }

  /** Проверка структурных инвариантов (unit-тесты, ENG6_STRICT=1). */
  selfCheck() {
    const problems = [];
    if (this.offset[0] !== 0) problems.push('offset[0] != 0');
    if (this.offset[this.n] !== this.to.length) problems.push('offset[n] != arcs');
    for (let u = 0; u < this.n; u++) {
      if (this.offset[u + 1] < this.offset[u]) problems.push(`offset не монотонен на ${u}`);
      for (let a = this.offset[u]; a < this.offset[u + 1]; a++) {
        if (this.to[a] < 0 || this.to[a] >= this.n) problems.push(`дуга ${a} ведёт вне графа`);
        if (!Number.isFinite(this.w[a])) problems.push(`дуга ${a}: неконечный вес`);
        if (!this.directed) {
          const t = this.twin[a];
          if (t < 0 || this.to[t] !== u || this.edgeId[t] !== this.edgeId[a])
            problems.push(`дуга ${a}: некорректная парная дуга`);
        }
      }
    }
    let arcs = 0;
    for (let u = 0; u < this.n; u++) arcs += this.offset[u + 1] - this.offset[u];
    if (arcs !== this.to.length) problems.push('сумма степеней != числу дуг');
    if (!this.directed && arcs !== 2 * this.m) problems.push('неориентированный граф: arcs != 2m');
    return problems;
  }
}

/** Построить CSR из списка рёбер [{from,to,w}] или [[u,v,w]]. */
export function csrFromEdgeList(n, edges, directed = false) {
  const m = edges.length;
  const f = new Int32Array(m);
  const t = new Int32Array(m);
  const w = new Float64Array(m);
  for (let e = 0; e < m; e++) {
    const ed = Array.isArray(edges[e]) ? { from: edges[e][0], to: edges[e][1], w: edges[e][2] } : edges[e];
    f[e] = ed.from;
    t[e] = ed.to;
    w[e] = ed.w === undefined ? 1 : ed.w;
  }
  return new CsrGraph(n, f, t, w, directed);
}

/** Union-Find с ранговой эвристикой и сжатием пути, O(α(n)) амортизированно. */
export class UnionFind {
  constructor(n) {
    this.p = new Int32Array(n);
    this.r = new Uint8Array(n);
    for (let i = 0; i < n; i++) this.p[i] = i;
    this.count = n;
  }

  find(x) {
    let root = x;
    while (this.p[root] !== root) root = this.p[root];
    while (this.p[x] !== root) {
      const nxt = this.p[x];
      this.p[x] = root;
      x = nxt;
    }
    return root;
  }

  union(a, b) {
    const ra = this.find(a);
    const rb = this.find(b);
    if (ra === rb) return false;
    if (this.r[ra] < this.r[rb]) this.p[ra] = rb;
    else if (this.r[ra] > this.r[rb]) this.p[rb] = ra;
    else {
      this.p[rb] = ra;
      this.r[ra]++;
    }
    this.count--;
    return true;
  }

  connected(a, b) {
    return this.find(a) === this.find(b);
  }
}
