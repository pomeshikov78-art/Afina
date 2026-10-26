import { loadEng6 } from '../core/eng6.mjs';
import util from 'node:util';

const { ENG6: E } = loadEng6();
const sc = E.defaultScenario('probe');
const t = E.buildTemplate('mixed', { lat: 55.75, lng: 37.62 });
sc.nodes = t.nodes; sc.links = t.links; sc.threats = t.threats;
sc.meta.counters = { node: sc.nodes.length, link: sc.links.length, threat: sc.threats.length };

const A = E.analyzeScenario(sc, { quality: 'fast' });
const first = Object.values(A.links)[0];
console.log('LINK RESULT KEYS:', Object.keys(first).join(', '));
console.log(util.inspect(first, { depth: 3, breakLength: 200 }).slice(0, 2600));
console.log('--- network keys:', Object.keys(A.network).join(', '));
console.log('--- network (без routes):', util.inspect({ ...A.network, routes: '<…>', criticalNodes: A.network.criticalNodes.slice(0, 2), bottlenecks: '<…>' }, { depth: 3, breakLength: 200 }).slice(0, 2200));
console.log('--- ratings:', JSON.stringify(A.ratings));
console.log('--- node res:', util.inspect(Object.values(A.nodes)[0]));
console.log('--- routes:', util.inspect(A.network.routes, { depth: 3, breakLength: 200 }).slice(0, 900));
console.log('--- types of links:', sc.links.map((l) => l.type).join(','));
