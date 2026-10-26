/**
 * scripts/build_frontend.mjs — сборка single-file UI.
 *
 * Делает три вещи:
 *   1) собирает демо-сценарий (ОШС роты связи, парк техники, топология, заявки);
 *   2) ПРОГОНЯЕТ ЕГО ЧЕРЕЗ ЯДРО ENG6-PLAN и вкладывает результат в HTML —
 *      поэтому интерфейс открывается уже с настоящей визуализацией расчёта
 *      (зоны покрытия, цвета линий по запасу, маршруты, расписание, живучесть)
 *      даже без запущенного сервера и без сети;
 *   3) инлайнит CSS и JS в frontend/index.html (NFR-02: ноль внешних ресурсов).
 *
 * Запуск: node scripts/build_frontend.mjs
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { plan } from '../core/planner.mjs';
import { buildDemoScenario } from '../frontend/src/demo_scenario.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const src = path.join(root, 'frontend', 'src');
const outDir = path.join(root, 'frontend');
mkdirSync(outDir, { recursive: true });

const template = readFileSync(path.join(src, 'index.template.html'), 'utf8');
const css = readFileSync(path.join(src, 'styles.css'), 'utf8');
const js = readFileSync(path.join(src, 'app.js'), 'utf8');

const { request, fleet, oush } = buildDemoScenario();
const t0 = Date.now();
const result = plan(request);
const dt = Date.now() - t0;

// демо-результат урезаем до того, что реально нужно UI (иначе HTML распухнет)
const slim = {
  status: result.status,
  statusReasons: result.statusReasons,
  metrics: result.metrics,
  timings: result.timings,
  core: result.core,
  plan: {
    deployedUnits: result.plan?.deployedUnits || [],
    deployedUnitsTotal: result.plan?.deployedUnitsTotal || 0,
    activeLinks: result.plan?.activeLinks || [],
    activeLinksTotal: result.plan?.activeLinksTotal || 0,
    routes: result.plan?.routes || [],
    routesTotal: result.plan?.routesTotal || 0,
    schedule: result.plan?.schedule ? { ...result.plan.schedule, jobs: (result.plan.schedule.jobs || []).slice(0, 200) } : null,
    rejectedLinks: result.plan?.rejectedLinks || [],
    rejectedLinksHistogram: result.plan?.rejectedLinksHistogram || {},
    survivability: result.plan?.survivability || null,
    truncation: result.plan?.truncation || null,
    redundancyAdded: result.plan?.redundancyAdded || 0,
  },
  diagnostics: {
    issues: (result.diagnostics?.issues || []).slice(0, 60),
    recommendations: (result.diagnostics?.recommendations || []).slice(0, 20),
    infeasibility: result.diagnostics?.infeasibility || null,
    approximation: result.diagnostics?.approximation || null,
    lowerBoundCost: result.diagnostics?.lowerBoundCost ?? null,
    eng6Analysis: result.diagnostics?.eng6Analysis
      ? { skipped: result.diagnostics.eng6Analysis.skipped, reason: result.diagnostics.eng6Analysis.reason, verdict: result.diagnostics.eng6Analysis.verdict, quality: result.diagnostics.eng6Analysis.quality, ratings: result.diagnostics.eng6Analysis.ratings, comparison: result.diagnostics.eng6Analysis.comparison, network: result.diagnostics.eng6Analysis.network }
      : null,
  },
};

const demo = { request, fleet, oush, result: slim };
const demoJson = JSON.stringify(demo).replace(/<\/script/gi, '<\\/script');

let html = template
  .replace('/*__CSS__*/', () => css)
  .replace('/*__JS__*/', () => js)
  .replace('/*__DEMO_DATA__*/', () => demoJson);

const outFile = path.join(outDir, 'index.html');
writeFileSync(outFile, html, 'utf8');

const kb = (Buffer.byteLength(html) / 1024).toFixed(0);
console.log(`✔ собрано ${path.relative(root, outFile)} — ${kb} КБ`);
console.log(`  демо-расчёт ядра: status=${slim.status} за ${dt} мс (compute ${result.timings?.totalMs} мс)`);
console.log(`  узлов ${request.topology.nodes.length}, линий ${request.topology.links.length}, заявок ${request.demands.length}, техники ${fleet.length}`);
console.log(`  метрики: активных линий ${slim.metrics?.linksActive}, развёрнуто узлов ${slim.metrics?.nodesDeployed}, стоимость ${slim.metrics?.totalCost}, оценка ${slim.metrics?.grade}`);
console.log(`  размер встроенных демо-данных: ${(Buffer.byteLength(demoJson) / 1024).toFixed(0)} КБ`);
