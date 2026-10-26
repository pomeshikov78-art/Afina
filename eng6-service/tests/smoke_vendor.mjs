import { loadEng6, conformance } from '../core/eng6.mjs';

const L = loadEng6();
console.log('loaded keys', L.keys.length, 'sha256', L.sha256.slice(0, 16), 'loadMs', L.loadMs.toFixed(1));
const t0 = Date.now();
const c = conformance();
console.log('conformance ms', Date.now() - t0);
console.log(JSON.stringify(c, null, 1).slice(0, 2500));
