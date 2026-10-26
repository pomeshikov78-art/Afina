import { plan, Eng6Planner } from '../core/planner.mjs';
import { battalionScenario, disconnectedScenario, oversubscribedScenario } from '../scripts/gen_topology.mjs';
import util from 'node:util';

const show = (label, r) => {
  console.log('\n════════', label, '════════');
  console.log('status:', r.status, '| reasons:', (r.statusReasons || []).map((x) => x.code).join(', '));
  console.log('timings:', JSON.stringify(r.timings));
  if (r.metrics) console.log('metrics:', util.inspect(r.metrics, { depth: 2, breakLength: 200 }));
  if (r.plan) {
    console.log('activeLinks:', r.plan.activeLinksTotal, 'deployed:', r.plan.deployedUnitsTotal, 'routes:', r.plan.routes.length);
    console.log('routes:', r.plan.routes.map((x) => `${x.demandId}:${x.status}/${x.grantedKbps}k/${x.delayMs}ms/hops=${x.hops}/${x.reason}${x.redundant ? '/RED' : ''}`).join('  '));
    console.log('schedule: makespan', r.plan.schedule.makespanMin, 'min, waves', r.plan.schedule.waves.length, 'crews', r.plan.schedule.crews);
    console.log('survivability:', JSON.stringify(r.plan.survivability));
  }
  if (r.diagnostics) {
    console.log('issues:', (r.diagnostics.issues || []).slice(0, 8).map((i) => `${i.sev}/${i.code}`).join(', '));
    console.log('approx:', JSON.stringify(r.diagnostics.approximation));
    if (r.diagnostics.infeasibility) console.log('infeas:', JSON.stringify(r.diagnostics.infeasibility.causes), '\n  relax:', JSON.stringify(r.diagnostics.infeasibility.minimalRelaxations).slice(0, 600));
    if (r.diagnostics.eng6Analysis && !r.diagnostics.eng6Analysis.skipped) console.log('eng6 ratings:', JSON.stringify(r.diagnostics.eng6Analysis.ratings), 'network:', JSON.stringify(r.diagnostics.eng6Analysis.network));
    else console.log('eng6:', JSON.stringify(r.diagnostics.eng6Analysis ?? null).slice(0, 200));
  }
};

const p = new Eng6Planner();
show('БАТАЛЬОН (7 узлов, fast)', p.plan(battalionScenario()));
show('БАТАЛЬОН (full physics)', p.plan(battalionScenario({ constraints: { physics: 'full', quality: 'balanced' } })));
show('НЕСВЯЗНАЯ СЕТЬ', p.plan(disconnectedScenario()));
show('ПЕРЕГРУЗКА', p.plan(oversubscribedScenario()));
show('INVALID', p.plan({ topology: { nodes: [{ id: 'a', type: 'cp', lat: 999, lng: 0 }], links: [] } }));
