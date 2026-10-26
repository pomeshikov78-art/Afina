import { CsrGraph, csrFromEdgeList, dijkstra, pathFromPrev, kruskalMst, steinerApprox, bridgesAndArticulations, components, connectivityPct, maxFlowUndirected, twoEdgeDisjointPaths, minCutUndirected } from '../core/graph/index.mjs';

const g = csrFromEdgeList(6, [
  { from: 0, to: 1, w: 4 },
  { from: 0, to: 2, w: 3 },
  { from: 1, to: 2, w: 1 },
  { from: 1, to: 3, w: 2 },
  { from: 2, to: 3, w: 4 },
  { from: 3, to: 4, w: 2 },
  { from: 4, to: 5, w: 3 },
]);
console.log('selfCheck:', g.selfCheck());
const { dist, prevArc } = dijkstra(g, 0);
console.log('dist from 0:', Array.from(dist));
console.log('path 0->5:', pathFromPrev(g, prevArc, 0, 5));
console.log('mst:', kruskalMst(g));
console.log('steiner [0,5,3]:', steinerApprox(g, [0, 5, 3]));
console.log('bridges:', bridgesAndArticulations(g));
console.log('components:', components(g));
console.log('conn%:', connectivityPct(g));
const cap = new Float64Array(g.m).fill(10);
cap[0] = 3;
console.log('maxflow 0->5:', maxFlowUndirected(g, 0, 5, cap));
console.log('mincut 0->5:', minCutUndirected(g, 0, 5, cap));
// два непересекающихся пути: 0-1-3-4-5 и 0-2-3-4-5 пересекаются по 3-4,4-5 → null
console.log('disjoint 0->5:', twoEdgeDisjointPaths(g, 0, 5));
// кольцо: есть два непересекающихся пути
const ring = csrFromEdgeList(5, [
  { from: 0, to: 1, w: 1 },
  { from: 1, to: 2, w: 1 },
  { from: 2, to: 3, w: 1 },
  { from: 3, to: 4, w: 1 },
  { from: 4, to: 0, w: 1 },
]);
console.log('ring disjoint 0->2:', twoEdgeDisjointPaths(ring, 0, 2));
console.log('ring bridges:', bridgesAndArticulations(ring));
// большой граф — проверка стека/производительности
const N = 100000;
const f = new Int32Array(N * 3);
const t = new Int32Array(N * 3);
let k = 0;
for (let i = 0; i < N - 1; i++) { f[k] = i; t[k] = i + 1; k++; }
for (let i = 0; i < N; i += 7) { f[k] = i; t[k] = (i + 101) % N; k++; }
const big = new CsrGraph(N, f.slice(0, k), t.slice(0, k), null, false);
let t0 = Date.now();
const d2 = dijkstra(big, 0, { target: N - 1 });
console.log('big dijkstra ms', Date.now() - t0, 'dist', d2.dist[N - 1]);
t0 = Date.now();
const b2 = bridgesAndArticulations(big);
console.log('big bridges ms', Date.now() - t0, 'count', b2.bridgeEdges.length, 'art', b2.articulations.length);
t0 = Date.now();
const c2 = components(big);
console.log('big components ms', Date.now() - t0, c2.count, 'conn%', connectivityPct(big).toFixed(2));
console.log('big selfCheck problems:', big.selfCheck().length);
console.log('heap MB', (process.memoryUsage().heapUsed / 1048576).toFixed(1), 'rss MB', (process.memoryUsage().rss / 1048576).toFixed(1));
