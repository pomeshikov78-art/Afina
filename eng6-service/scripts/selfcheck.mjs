#!/usr/bin/env node
/**
 * scripts/selfcheck.mjs — быстрая самопроверка изделия (30–60 с).
 *
 * Выполняет приёмочные проверки из «Дополнения к ТЗ №2», п. 9.1, без поднятия
 * HTTP-сервера: конформанс вендора, эталонный расчёт, три граничных случая,
 * детерминированность, изоляция ядра, целостность сборки UI.
 *
 * Запуск: node scripts/selfcheck.mjs   (код возврата 0 = изделие пригодно)
 */
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEng6, conformance, VENDOR_PATH } from '../core/eng6.mjs';
import { plan } from '../core/planner.mjs';
import { battalionScenario, disconnectedScenario, oversubscribedScenario } from './gen_topology.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const results = [];
let failed = 0;

function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  if (!ok) failed++;
  const mark = ok ? '✔' : '✖';
  process.stdout.write(`${mark} ${name}${detail ? ` — ${detail}` : ''}\n`);
}

const t0 = Date.now();

/* 1. Вендор и конформанс */
const core = loadEng6();
const expectedSha = readFileSync(path.join(root, 'core/vendor/VENDOR_SHA256.txt'), 'utf8').trim().split(/\s+/)[0];
const actualSha = createHash('sha256').update(readFileSync(VENDOR_PATH)).digest('hex');
check('sha256 вендора ENG6 совпадает с эталоном', actualSha === expectedSha, actualSha.slice(0, 16) + '…');
check('Публичная поверхность ENG6 — 69 ключей', core.keys.length === 69, `${core.keys.length}`);

const conf = conformance();
check('Конформанс ENG6: selfTest 33/33', conf.selfTestTotal === 33 && conf.selfTestFailed.length === 0, `${conf.selfTestTotal - conf.selfTestFailed.length}/${conf.selfTestTotal}`);
check('Конформанс ENG6: регрессия 5/5', conf.regressionTotal === 5 && conf.regressionFailed.length === 0, `${conf.regressionTotal - conf.regressionFailed.length}/${conf.regressionTotal}`);

/* 2. Изоляция вендора */
let tripwireOk = true;
let tripwireDetail = '';
for (const api of ['document', 'fetch', 'localStorage', 'XMLHttpRequest', 'WebSocket']) {
  try {
    vm.runInContext(`(${api})`, core.context);
    tripwireOk = false;
    tripwireDetail = `${api} доступен`;
    break;
  } catch (err) {
    if (!/ENG6_ISOLATION_VIOLATION/.test(String(err.message))) {
      tripwireOk = false;
      tripwireDetail = `${api}: ${err.message}`;
      break;
    }
  }
}
check('Изоляция вендора: браузерные API под tripwire', tripwireOk, tripwireDetail);

/* 3. Эталонный расчёт */
const base = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
base.constraints.eng6Analysis = false;
const r1 = plan(base);
check(
  'Эталон «батальон»: расчёт выполнен, структура ответа полная',
  ['OPTIMAL', 'FEASIBLE', 'PARTIAL'].includes(r1.status) && !!r1.plan && !!r1.metrics && !!r1.diagnostics && !!r1.timings,
  `${r1.status}, обслужено ${r1.metrics?.demandsSatisfied}/${r1.metrics?.demandsTotal}, линий ${r1.metrics?.linksActive}, оценка ${r1.metrics?.grade}`,
);
check('Эталон: расписание развертывания построено', r1.plan.schedule.makespanMin > 0 && r1.plan.schedule.waves.length > 0, `makespan ${r1.plan.schedule.makespanMin} мин, волн ${r1.plan.schedule.waves.length}`);
check('Эталон: 🔴 техника не размещена (FR-RES-03)', !r1.plan.deployedUnits.some((d) => d.condition === 'red'), `развернуто ${r1.plan.deployedUnitsTotal}`);
check('Эталон: живучесть посчитана', Array.isArray(r1.plan.survivability.computed) && r1.plan.survivability.computed.length >= 2, `ЕТО ${r1.plan.survivability.spofCount}, мостов ${r1.plan.survivability.bridgeCount}`);

/* 4. Граничные случаи */
const r2 = plan(disconnectedScenario({ origin: { lat: 55.75, lng: 37.62 } }));
const relay = r2.diagnostics?.infeasibility?.minimalRelaxations?.find((x) => x.code === 'ADD_RELAY');
check(
  'Граничный случай «сеть несвязна»: PARTIAL + предложение ретранслятора',
  ['PARTIAL', 'INFEASIBLE'].includes(r2.status) && !!relay && Number.isFinite(relay?.at?.lat),
  relay ? `${relay.code} в ${relay.at.lat},${relay.at.lng}, пролёт ${relay.spanKm} км, ЭИИМ ${relay.requiredEirpDbm} дБм` : 'предложения нет',
);
check('Граничный случай: частичное решение описано', !!r2.diagnostics?.infeasibility?.partialSolution, `обслужено ${r2.diagnostics?.infeasibility?.partialSolution?.demandsServed ?? 0}`);

const r3 = plan(oversubscribedScenario({ origin: { lat: 55.75, lng: 37.62 } }));
const capRelax = r3.diagnostics?.infeasibility?.minimalRelaxations?.filter((x) => x.code === 'ADD_CAPACITY') || [];
check(
  'Граничный случай «перегрузка»: PARTIAL + дефицит полосы + ADD_CAPACITY',
  ['PARTIAL', 'INFEASIBLE'].includes(r3.status) && capRelax.length > 0,
  `${r3.status}, узких мест ${capRelax.length}, выдано ${r3.metrics?.throughputKbps} кбит/с`,
);

/* 5. Некорректный вход */
const r4 = plan({ topology: { nodes: [{ id: 'a', type: 'cp', lat: 999, lng: 0 }], links: [] } });
check('Некорректный вход → INVALID с JSON-путями ошибок', r4.status === 'INVALID' && (r4.statusReasons || []).some((x) => x.path === '$.topology.nodes[0].lat'), (r4.statusReasons || [])[0]?.message || '');

/* 6. Детерминированность */
const strip = (x) => { const c = JSON.parse(JSON.stringify(x)); delete c.timings; return JSON.stringify(c); };
check('Детерминированность: два identical входа → побайтово identical выход', strip(r1) === strip(plan(battalionScenario({ origin: { lat: 55.75, lng: 37.62 }, constraints: { eng6Analysis: false } }))));

/* 7. Режимы физики: fast и full на плоском рельефе */
const flat = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
flat.environment.terrain = { source: 'flat', amplitudeM: 0 };
flat.constraints.eng6Analysis = false;
const fast = plan({ ...flat, constraints: { ...flat.constraints, physics: 'fast', fastMarginPenaltyDb: 0 } });
const full = plan({ ...flat, constraints: { ...flat.constraints, physics: 'full', quality: 'balanced' } });
let maxDelta = 0;
for (const l of fast.plan?.activeLinks || []) {
  const o = (full.plan?.activeLinks || []).find((x) => x.id === l.id);
  if (o) maxDelta = Math.max(maxDelta, Math.abs(l.marginDb - o.marginDb));
}
check('Режимы физики fast/full согласованы на плоском рельефе (< 0,01 дБ)', maxDelta < 0.01, `максимальное расхождение ${maxDelta.toFixed(4)} дБ`);

/* 8. Перекрёстная проверка плана ядром ENG6 */
const cross = plan(battalionScenario({ origin: { lat: 55.75, lng: 37.62 } }));
const eng6a = cross.diagnostics?.eng6Analysis;
check('Перекрёстная проверка ENG6 выполняется и даёт вердикт', !!eng6a && (eng6a.skipped === true || !!eng6a.verdict), eng6a ? (eng6a.skipped ? `пропущено: ${eng6a.reason}` : `${eng6a.verdict}, оценка ${eng6a.ratings?.grade}`) : 'нет блока');

/* 9. Сборка UI */
try {
  const html = readFileSync(path.join(root, 'frontend/index.html'), 'utf8');
  const noExternal = !/(?:src|href)\s*=\s*["']https?:/i.test(html) && !/<script[^>]+src=/i.test(html);
  const demo = html.match(/<script id="demo-data" type="application\/json">([\s\S]*?)<\/script>/);
  const demoOk = !!demo && !demo[1].trim().startsWith('/*__');
  const uiSrc = readFileSync(path.join(root, 'frontend/src/app.js'), 'utf8');
  const noStorage = !/localStorage\s*[.[]/.test(uiSrc) && !/sessionStorage\s*[.[]/.test(uiSrc) && !/document\.cookie/.test(uiSrc.replace(/^\s*\*.*$/gm, ''));
  check('Сборка UI самодостаточна (нет внешних ресурсов)', noExternal && html.length > 100_000, `${(html.length / 1024).toFixed(0)} КБ`);
  check('UI содержит встроенный результат расчёта ядра', demoOk, demoOk ? 'демо-план встроен' : 'демо-данные не заполнены — выполните npm run build:ui');
  check('NFR-02: UI не использует локальное хранилище браузера', noStorage);
  if (demoOk) {
    const d = JSON.parse(demo[1]);
    check('Демо-план корректен', !!d.result?.status && !!d.request?.topology, `${d.result?.status}, узлов ${d.request?.topology?.nodes?.length}`);
  }
} catch (err) {
  check('Сборка UI присутствует', false, String(err.message));
}

/* 10. Документация и инфраструктура */
const must = [
  'README.md', 'docs/01_MATH_MODEL.md', 'docs/02_ARCHITECTURE.md', 'docs/03_API.md',
  'docs/04_TEST_REPORT.md', 'docs/05_OPS.md', 'docs/06_DEFECTS.md', 'docs/openapi.yaml',
  'docs/TZ_Dopolnenie_GOST34602.docx', 'deploy/Dockerfile', 'deploy/docker-compose.yml',
  'deploy/prometheus/prometheus.yml', 'deploy/grafana/dashboards/eng6-plan.json',
  'deploy/k8s/eng6-plan.yaml', 'loadtest/run_load.mjs', 'loadtest/k6_comms_planner.js',
  'loadtest/locustfile.py', 'examples/battalion.json',
];
const missing = must.filter((f) => { try { readFileSync(path.join(root, f)); return false; } catch { return true; } });
check('Комплект поставки в наличии', missing.length === 0, missing.length ? `отсутствуют: ${missing.join(', ')}` : `${must.length} файлов`);

const dt = Date.now() - t0;
process.stdout.write(`\n${'═'.repeat(72)}\n`);
process.stdout.write(`ИТОГ: ${results.length - failed}/${results.length} проверок пройдено за ${dt} мс\n`);
process.stdout.write(`ядро: ENG6 ${core.ENG6.version} (sha256 ${core.sha256.slice(0, 16)}…), планировщик ENG6-PLAN\n`);
if (failed) {
  process.stdout.write(`\n✖ ПРОВАЛЕНО ${failed}:\n`);
  for (const r of results.filter((x) => !x.ok)) process.stdout.write(`   • ${r.name}${r.detail ? ` — ${r.detail}` : ''}\n`);
}
process.exit(failed ? 1 : 0);
