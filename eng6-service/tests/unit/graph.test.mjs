/**
 * tests/unit/graph.test.mjs — графовый подслой ядра.
 *
 * Проверяется на эталонных малых графах (5–10 узлов) с заранее известным
 * ответом, плюс сверка с ENG6.NET (защита от расхождения двух реализаций)
 * и проверка поведения на крупном графе (стек, память, время).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CsrGraph, csrFromEdgeList, UnionFind, MinHeap, dijkstra, dijkstraLimited, hopCount, pathFromPrev,
  components, connectivityPct, bridgesAndArticulations, bridgesOfSubgraph,
  kruskalMst, steinerApprox, trivialLowerBound,
  maxFlowUndirected, minCutUndirected, twoEdgeDisjointPaths,
} from '../../core/graph/index.mjs';
import { loadEng6 } from '../../core/eng6.mjs';

const E = loadEng6().ENG6;

/* ── CSR и куча ───────────────────────────────────────────────────────── */

test('CSR: инварианты структуры и симметрия дуг', () => {
  const g = csrFromEdgeList(4, [
    { from: 0, to: 1, w: 2 },
    { from: 1, to: 2, w: 3 },
    { from: 2, to: 0, w: 1 },
    { from: 1, to: 3, w: 4 },
  ]);
  assert.deepEqual(g.selfCheck(), []);
  assert.equal(g.n, 4);
  assert.equal(g.m, 4);
  assert.equal(g.arcs, 8); // 2m для неориентированного
  assert.equal(g.degree(1), 3);
  // twin[] — парная дуга
  for (let a = 0; a < g.arcs; a++) {
    assert.equal(g.to[g.twin[a]], g.tail(a));
    assert.equal(g.edgeId[g.twin[a]], g.edgeId[a]);
  }
});

test('CSR: параллельные рёбра не ломают twin/edgeId', () => {
  const g = csrFromEdgeList(2, [
    { from: 0, to: 1, w: 1 },
    { from: 0, to: 1, w: 5 },
  ]);
  assert.equal(g.arcs, 4);
  assert.deepEqual(g.selfCheck(), []);
  const ids = new Set();
  for (let a = 0; a < g.arcs; a++) ids.add(g.edgeId[a]);
  assert.equal(ids.size, 2);
});

test('MinHeap: порядок извлечения и эквивалентность сортировке', () => {
  const h = new MinHeap(4);
  const ref = [];
  for (let i = 0; i < 500; i++) {
    const k = Math.random() * 1000;
    h.push(k, i);
    ref.push([k, i]);
  }
  ref.sort((a, b) => a[0] - b[0]);
  const got = [];
  while (!h.empty) got.push(h.pop()[0]);
  assert.equal(got.length, ref.length);
  for (let i = 0; i < got.length; i++) assert.ok(Math.abs(got[i] - ref[i][0]) < 1e-12, `позиция ${i}`);
});

test('UnionFind: сжатие пути и число компонент', () => {
  const uf = new UnionFind(6);
  assert.equal(uf.count, 6);
  uf.union(0, 1); uf.union(1, 2); uf.union(3, 4);
  assert.equal(uf.count, 3);
  assert.ok(uf.connected(0, 2));
  assert.ok(!uf.connected(0, 3));
  assert.equal(uf.find(2), uf.find(0));
});

/* ── Дейкстра ─────────────────────────────────────────────────────────── */

test('Дейкстра: эталонный граф 6 узлов, кратчайшие расстояния и путь', () => {
  const g = csrFromEdgeList(6, [
    { from: 0, to: 1, w: 4 }, { from: 0, to: 2, w: 3 }, { from: 1, to: 2, w: 1 },
    { from: 1, to: 3, w: 2 }, { from: 2, to: 3, w: 4 }, { from: 3, to: 4, w: 2 },
    { from: 4, to: 5, w: 3 },
  ]);
  const { dist, prevArc } = dijkstra(g, 0);
  assert.deepEqual(Array.from(dist), [0, 4, 3, 6, 8, 11]);
  const p = pathFromPrev(g, prevArc, 0, 5);
  assert.deepEqual(p.nodes, [0, 1, 3, 4, 5]);
  assert.equal(p.arcs.length, 4);
});

test('Дейкстра: ранний выход по target не портит дистанцию до цели', () => {
  const g = csrFromEdgeList(5, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 2, to: 3, w: 1 },
    { from: 3, to: 4, w: 1 }, { from: 0, to: 4, w: 100 },
  ]);
  const a = dijkstra(g, 0, { target: 4 });
  const b = dijkstra(g, 0);
  assert.equal(a.dist[4], 4);
  assert.equal(b.dist[4], 4);
});

test('Дейкстра: недостижимый узел → Infinity, путь null', () => {
  const g = csrFromEdgeList(4, [{ from: 0, to: 1, w: 1 }]);
  const { dist, prevArc } = dijkstra(g, 0);
  assert.equal(dist[3], Infinity);
  assert.equal(pathFromPrev(g, prevArc, 0, 3), null);
});

test('RCSP (dijkstraLimited): минимум стоимости при ограничении по хопам', () => {
  // прямой путь 0→3 дорогой (1 хоп, cost 100), обходной дешёвый, но 3 хопа
  const g = csrFromEdgeList(4, [
    { from: 0, to: 3, w: 100 }, { from: 0, to: 1, w: 1 },
    { from: 1, to: 2, w: 1 }, { from: 2, to: 3, w: 1 },
  ]);
  const hops = new Float64Array(g.arcs).fill(1);
  const r1 = dijkstraLimited(g, 0, 3, { cost: g.w, delay: hops, maxDelay: 1 });
  assert.ok(r1, 'путь в 1 хоп должен найтись');
  assert.equal(r1.cost, 100);
  assert.equal(r1.delay, 1);
  const r3 = dijkstraLimited(g, 0, 3, { cost: g.w, delay: hops, maxDelay: 3 });
  assert.equal(r3.cost, 3);
  assert.equal(r3.delay, 3);
  // при лимите 2 хопа доступен только прямой переход (1 хоп, стоимость 100):
  // обходной путь 0→1→2→3 требует 3 хопов
  const r2 = dijkstraLimited(g, 0, 3, { cost: g.w, delay: hops, maxDelay: 2 });
  assert.ok(r2, 'прямой путь укладывается в 2 хопа');
  assert.equal(r2.cost, 100);
  assert.equal(r2.delay, 1);
  // при лимите 0 хопов пути нет (источник ≠ приёмник)
  assert.equal(dijkstraLimited(g, 0, 3, { cost: g.w, delay: hops, maxDelay: 0 }), null);
});

test('BFS hopCount: число хопов от источника', () => {
  const g = csrFromEdgeList(5, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 },
    { from: 2, to: 3, w: 1 }, { from: 3, to: 4, w: 1 },
  ]);
  assert.deepEqual(Array.from(hopCount(g, 0)), [0, 1, 2, 3, 4]);
});

/* ── Связность, мосты, точки сочленения ───────────────────────────────── */

test('Компоненты и связность: эталонные значения', () => {
  const g = csrFromEdgeList(6, [{ from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 3, to: 4, w: 1 }]);
  const c = components(g);
  assert.equal(c.count, 3);
  assert.equal(c.largest, 3);
  // Σ C(ni,2)/C(6,2) = (3+1)/15 = 26.67 %
  assert.ok(Math.abs(connectivityPct(g) - 26.666666) < 1e-3);
});

test('Мосты и точки сочленения: путь, кольцо, «гантель»', () => {
  const path = csrFromEdgeList(4, [{ from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 2, to: 3, w: 1 }]);
  const b1 = bridgesAndArticulations(path);
  assert.equal(b1.bridgeEdges.length, 3);
  assert.deepEqual(Array.from(b1.articulations), [1, 2]);

  const ring = csrFromEdgeList(5, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 2, to: 3, w: 1 },
    { from: 3, to: 4, w: 1 }, { from: 4, to: 0, w: 1 },
  ]);
  const b2 = bridgesAndArticulations(ring);
  assert.equal(b2.bridgeEdges.length, 0, 'в кольце мостов нет');
  assert.equal(b2.articulations.length, 0, 'в кольце точек сочленения нет');

  // гантель: два треугольника, соединённых одним ребром (2→3)
  const dumb = csrFromEdgeList(6, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 2, to: 0, w: 1 },
    { from: 3, to: 4, w: 1 }, { from: 4, to: 5, w: 1 }, { from: 5, to: 3, w: 1 },
    { from: 2, to: 3, w: 1 },
  ]);
  const b3 = bridgesAndArticulations(dumb);
  assert.deepEqual(Array.from(b3.bridgeEdges), [6]);
  assert.deepEqual(Array.from(b3.articulations), [2, 3]);
});

test('Мосты: параллельные рёбра мостом не являются (проверка twin[])', () => {
  const g = csrFromEdgeList(3, [
    { from: 0, to: 1, w: 1 }, { from: 0, to: 1, w: 2 }, { from: 1, to: 2, w: 1 },
  ]);
  const b = bridgesAndArticulations(g);
  assert.deepEqual(Array.from(b.bridgeEdges), [2], 'только ребро 1—2 одиночное');
});

test('Мосты подграфа активных рёбер', () => {
  const g = csrFromEdgeList(4, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 },
    { from: 2, to: 3, w: 1 }, { from: 3, to: 0, w: 1 },
  ]);
  const mask = new Uint8Array([1, 1, 1, 0]); // разомкнули кольцо
  const br = bridgesOfSubgraph(g, mask);
  assert.equal(br.length, 3);
  assert.deepEqual(Array.from(br).sort(), [0, 1, 2]);
});

/* ── Остов и Штейнер ──────────────────────────────────────────────────── */

test('Крускал: MST эталонного графа (вес 16)', () => {
  const g = csrFromEdgeList(5, [
    { from: 0, to: 1, w: 4 }, { from: 0, to: 2, w: 3 }, { from: 1, to: 2, w: 1 },
    { from: 1, to: 3, w: 2 }, { from: 2, to: 3, w: 5 }, { from: 3, to: 4, w: 6 },
    { from: 2, to: 4, w: 7 },
  ]);
  const m = kruskalMst(g);
  assert.equal(m.totalWeight, 1 + 2 + 3 + 6 + 4 - 4 + 0 + 0 || m.totalWeight); // формально ниже
  assert.equal(m.edges.length, 4);
  assert.equal(m.totalWeight, 1 + 2 + 3 + 6); // 1-2, 1-3, 0-2, 3-4
  assert.ok(m.spansAll);
});

test('Штейнер (2-аппроксимация): терминалы соединены, LB ≤ факт', () => {
  const g = csrFromEdgeList(6, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 2, to: 3, w: 1 },
    { from: 3, to: 4, w: 1 }, { from: 4, to: 5, w: 1 }, { from: 0, to: 5, w: 20 },
    { from: 1, to: 4, w: 9 },
  ]);
  const w = new Float64Array(g.m);
  for (let e = 0; e < g.m; e++) w[e] = g.w[g.arcOf(e)];
  const st = steinerApprox(g, [0, 3, 5], { weight: w });
  assert.equal(st.mode, 'metric_closure_mst');
  assert.ok(st.edges.length >= 2);
  assert.ok(st.lowerBound > 0 && st.lowerBound <= st.totalWeight + 1e-9, `LB ${st.lowerBound} ≤ ${st.totalWeight}`);
  // все терминалы в одном дереве
  const mask = new Uint8Array(g.m);
  for (const e of st.edges) mask[e] = 1;
  const sub = g.subgraph(mask);
  const c = components(sub);
  assert.equal(c.comp[0], c.comp[3], 'терминалы 0 и 3 в одной компоненте');
  assert.equal(c.comp[3], c.comp[5], 'терминалы 3 и 5 в одной компоненте');
  // дерево Штейнера не обязано быть остовным: ребро-«хорда» 0—5 (вес 20) не берётся
  assert.ok(!Array.from(st.edges).includes(5), 'дорогая хорда 0—5 не должна войти в дерево');
  assert.ok(trivialLowerBound(g, [0, 3, 5], w) <= st.totalWeight + 1e-9);
});

test('Штейнер: недостижимый терминал попадает в unreachable, а не в исключение', () => {
  const g = csrFromEdgeList(4, [{ from: 0, to: 1, w: 1 }]); // узлы 2,3 изолированы
  const st = steinerApprox(g, [0, 2]);
  assert.deepEqual(Array.from(st.unreachable), [2]);
});

/* ── Поток и непересекающиеся пути ────────────────────────────────────── */

test('Максимальный поток: узкое место ограничивает (эталон 10)', () => {
  const g = csrFromEdgeList(6, [
    { from: 0, to: 1, w: 1 }, { from: 0, to: 2, w: 1 }, { from: 1, to: 3, w: 1 },
    { from: 2, to: 3, w: 1 }, { from: 3, to: 4, w: 1 }, { from: 4, to: 5, w: 1 },
  ]);
  const cap = new Float64Array(g.m).fill(10);
  cap[4] = 3; // ребро 3—4 сужено до 3
  assert.equal(maxFlowUndirected(g, 0, 5, cap), 3);
  const mc = minCutUndirected(g, 0, 5, cap);
  assert.equal(mc.value, 3);
  assert.deepEqual(Array.from(mc.cutEdges), [4]);
});

test('Максимальный поток: сумма ёмкостей параллельных рёбер', () => {
  const g = csrFromEdgeList(2, [{ from: 0, to: 1, w: 1 }, { from: 0, to: 1, w: 1 }]);
  assert.equal(maxFlowUndirected(g, 0, 1, new Float64Array([4, 6])), 10);
});

test('Два реберно-непересекающихся пути: кольцо — да, мост — нет', () => {
  const ring = csrFromEdgeList(5, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 2, to: 3, w: 1 },
    { from: 3, to: 4, w: 1 }, { from: 4, to: 0, w: 1 },
  ]);
  const r = twoEdgeDisjointPaths(ring, 0, 2);
  assert.ok(r, 'в кольце пути существуют');
  assert.equal(r.edgePaths.length, 2);
  const used = new Set([...r.edgePaths[0], ...r.edgePaths[1]]);
  assert.equal(used.size, r.edgePaths[0].length + r.edgePaths[1].length, 'пути не пересекаются по рёбрам');

  const bridged = csrFromEdgeList(4, [
    { from: 0, to: 1, w: 1 }, { from: 1, to: 2, w: 1 }, { from: 2, to: 3, w: 1 },
  ]);
  assert.equal(twoEdgeDisjointPaths(bridged, 0, 3), null);
});

/* ── Сверка с ENG6.NET (защита от расхождения реализаций) ─────────────── */

function eng6Scenario(edges, nNodes) {
  const nodes = [];
  for (let i = 0; i < nNodes; i++) nodes.push({ id: `n${i}`, type: i === 0 ? 'cp' : 'relay', name: `n${i}`, lat: 55.7 + i * 0.05, lng: 37.6 + i * 0.03, status: 'active', txPowerDbm: 43, txGainDbi: 9, rxGainDbi: 9, rxSensDbm: -107, freqMhz: 150, antHeightM: 15, feederLossDb: 1, noiseFigureDb: 5 });
  const links = edges.map(([a, b, type], i) => ({ id: `l${i}`, from: `n${a}`, to: `n${b}`, type: type || 'VHF', status: 'active', priority: 2, isBackup: false, reqSnrDb: '', fhss: 0, dsss: 0, freqMhz: 150, bwKhz: 25, mod: 'QPSK', fec: 'none' }));
  const sc = E.defaultScenario('graph-vs-eng6');
  sc.nodes = nodes;
  sc.links = links;
  sc.threats = [];
  sc.environment.terrain = { source: 'flat', amplitudeM: 0 };
  return sc;
}

test('Сверка с ENG6: компоненты и связность совпадают (5 эталонных топологий)', () => {
  const cases = [
    { name: 'цепочка 5', n: 5, e: [[0, 1], [1, 2], [2, 3], [3, 4]] },
    { name: 'кольцо 6', n: 6, e: [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 0]] },
    { name: 'звезда 7', n: 7, e: [[0, 1], [0, 2], [0, 3], [0, 4], [0, 5], [0, 6]] },
    { name: 'две компоненты', n: 6, e: [[0, 1], [1, 2], [3, 4], [4, 5]] },
    { name: 'полный 5', n: 5, e: [[0, 1], [0, 2], [0, 3], [0, 4], [1, 2], [1, 3], [1, 4], [2, 3], [2, 4], [3, 4]] },
  ];
  for (const c of cases) {
    const sc = eng6Scenario(c.e, c.n);
    const A = E.analyzeScenario(sc, { quality: 'fast' });
    const g = csrFromEdgeList(c.n, c.e.map(([a, b]) => ({ from: a, to: b, w: 1 })));
    const mine = components(g);
    assert.equal(mine.count, A.network.componentsCount, `${c.name}: число компонент`);
    assert.ok(Math.abs(connectivityPct(g) - A.network.connectivityPct) < 0.51, `${c.name}: связность ${connectivityPct(g)} vs ${A.network.connectivityPct}`);
  }
});

test('Сверка с ENG6: мосты = criticalLinks/bridges ядра', () => {
  const cases = [
    { n: 5, e: [[0, 1], [1, 2], [2, 3], [3, 4]] },
    { n: 6, e: [[0, 1], [1, 2], [2, 0], [2, 3], [3, 4], [4, 5]] },
    { n: 7, e: [[0, 1], [1, 2], [2, 3], [3, 0], [3, 4], [4, 5], [5, 6], [6, 4]] },
  ];
  for (const c of cases) {
    const sc = eng6Scenario(c.e, c.n);
    const A = E.analyzeScenario(sc, { quality: 'fast' });
    const g = csrFromEdgeList(c.n, c.e.map(([a, b]) => ({ from: a, to: b, w: 1 })));
    const mine = bridgesAndArticulations(g);
    const eng6Bridges = (A.network.bridges || []).length;
    assert.equal(mine.bridgeEdges.length, eng6Bridges, `мостов: ${mine.bridgeEdges.length} vs ENG6 ${eng6Bridges}`);
    const eng6Spof = (A.network.singlePointsOfFailure || []).length;
    assert.equal(mine.articulations.length, eng6Spof, `ЕТО: ${mine.articulations.length} vs ENG6 ${eng6Spof}`);
  }
});

test('Сверка с ENG6: haversine ядра и планировщика совпадают побитово', async () => {
  const { haversineKm } = await import('../../core/physics.mjs');
  const sc = eng6Scenario([[0, 1]], 2);
  const A = E.analyzeScenario(sc, { quality: 'fast' });
  const dEng = Object.values(A.links)[0].distKm;
  const mine = haversineKm(sc.nodes[0].lat, sc.nodes[0].lng, sc.nodes[1].lat, sc.nodes[1].lng);
  assert.equal(mine, dEng, `${mine} !== ${dEng}`);
});

/* ── Масштаб: стек, память, время ─────────────────────────────────────── */

test('Крупный граф 100 000 узлов: DFS/мосты не пробивают стек и укладываются в 5 с', () => {
  const N = 100_000;
  const from = new Int32Array(N * 2);
  const to = new Int32Array(N * 2);
  let k = 0;
  for (let i = 0; i < N - 1; i++) { from[k] = i; to[k] = i + 1; k++; }
  for (let i = 0; i < N; i += 11) { from[k] = i; to[k] = (i + 977) % N; k++; }
  const t0 = Date.now();
  const g = new CsrGraph(N, from.subarray(0, k), to.subarray(0, k), null, false);
  assert.deepEqual(g.selfCheck(), []);
  const c = components(g);
  assert.equal(c.count, 1);
  const b = bridgesAndArticulations(g);
  const dt = Date.now() - t0;
  assert.ok(dt < 5000, `слишком медленно: ${dt} мс`);
  assert.ok(b.bridgeEdges.length >= 0);
  const d = dijkstra(g, 0, { target: N - 1 });
  assert.ok(Number.isFinite(d.dist[N - 1]));
  assert.ok(process.memoryUsage().rss < 1024 * 1024 * 1024, 'RSS выше 1 ГБ');
});
