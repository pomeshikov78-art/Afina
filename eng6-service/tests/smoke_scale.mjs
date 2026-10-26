import { plan } from '../core/planner.mjs';
import { battalionScenario, randomTopology, fleetFor } from '../scripts/gen_topology.mjs';

const b = plan(battalionScenario());
console.log('battalion metrics:', JSON.stringify(b.metrics, null, 1));
console.log('battalion timings:', JSON.stringify(b.timings));

for (const n of [1000, 10000]) {
  const req = randomTopology({ n, seed: 7, areaKm: n > 5000 ? 600 : 200, avgDegree: 4, demandRatio: 0.02 });
  req.fleet = fleetFor(req.topology.nodes, { seed: 5 });
  const t0 = Date.now();
  const r = plan(req);
  const dt = Date.now() - t0;
  console.log(
    `\nn=${n}: status=${r.status} wall=${dt}ms timings=${JSON.stringify(r.timings)}\n  metrics: nodes=${r.metrics.nodesTotal} linksCand=${r.metrics.linksCandidate} feas=${r.metrics.linksFeasible} active=${r.metrics.linksActive} demands=${r.metrics.demandsTotal} sat=${r.metrics.demandsSatisfied} part=${r.metrics.demandsPartial} rej=${r.metrics.demandsRejected} thr=${r.metrics.throughputKbps}k util=${r.metrics.utilMaxPct}% conn=${r.metrics.connectivityPct}% cost=${r.metrics.totalCost} obj=${r.metrics.objectiveValue} grade=${r.metrics.grade}\n  rss=${(process.memoryUsage().rss / 1048576).toFixed(1)}MB heap=${(process.memoryUsage().heapUsed / 1048576).toFixed(1)}MB`,
  );
}
