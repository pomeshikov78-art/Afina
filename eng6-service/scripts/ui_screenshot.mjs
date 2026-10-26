/**
 * scripts/ui_screenshot.mjs — проверка UI в настоящем headless-браузере.
 *
 * Зачем: jsdom не умеет Canvas2D, поэтому до этого момента интерфейс
 * проверялся только на отсутствие исключений. Здесь UI открывается в Chrome:
 *   1) снимается скриншот рабочего пространства (демо-сценарий + результат ядра);
 *   2) выполняется ЖИВОЙ расчёт через API (сервис поднимается этим же скриптом);
 *   3) снимается скриншот карточки объекта (3 вкладки по FR-CTX-02);
 *   4) проверяется NFR-01: добавление 1000 тактических знаков и замер фактического FPS;
 *   5) собираются все ошибки консоли (их быть не должно).
 *
 * Запуск: node scripts/ui_screenshot.mjs [--out=var/shots] [--signs=1000]
 * Требует: puppeteer (npm i --no-save puppeteer) — только для разработки.
 */
import { spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = Object.fromEntries(process.argv.slice(2).map((a) => { const m = a.match(/^--([^=]+)=(.*)$/); return m ? [m[1], m[2]] : [a.replace(/^--/, ''), 'true']; }));
const OUT = path.resolve(root, String(args.out || 'var/shots'));
const SIGNS = Number(args.signs || 1000);
const PORT = Number(args.port || 8099);
mkdirSync(OUT, { recursive: true });

const log = (...a) => process.stdout.write(a.join(' ') + '\n');

let puppeteer;
try {
  puppeteer = (await import('puppeteer')).default;
} catch {
  log('✖ puppeteer не установлен: npm install --no-save puppeteer');
  process.exit(2);
}

/* ── поднимаем сервис ──────────────────────────────────────────────────── */
const srv = spawn(process.execPath, [path.join(root, 'api/main.mjs')], {
  cwd: root,
  env: { ...process.env, ENG6_PORT: String(PORT), ENG6_HOST: '127.0.0.1', ENG6_WORKERS: '2', ENG6_AUDIT_FILE: path.join(root, 'var/audit-ui.ndjson'), ENG6_RL_REQUESTS: '100000' },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let srvOut = '';
srv.stdout.on('data', (d) => { srvOut += d.toString(); });
srv.stderr.on('data', (d) => { srvOut += d.toString(); });

async function waitServer(timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/readyz`);
      if (r.ok || r.status === 503) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error('сервис не стартовал:\n' + srvOut.slice(-1500));
}

const shot = async (page, name, opts = {}) => {
  const f = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: f, ...opts });
  log(`  📸 ${f}`);
  return f;
};

let exitCode = 0;
try {
  await waitServer();
  log(`✔ сервис поднят на http://127.0.0.1:${PORT}`);

  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--font-render-hinting=none', '--force-device-scale-factor=1'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 900, deviceScaleFactor: 1 });

  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) consoleErrors.push(`[${m.type()}] ${m.text().slice(0, 200)}`); });
  page.on('pageerror', (e) => pageErrors.push(String(e.message).slice(0, 300)));
  page.on('requestfailed', (r) => {
    const u = r.url();
    if (!u.startsWith(`http://127.0.0.1:${PORT}`)) consoleErrors.push(`[net] ${u} → ${r.failure()?.errorText}`);
  });

  log('\n═══ 1. Открытие UI и соединение с ядром ═══');
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'networkidle2', timeout: 45000 });
  await new Promise((r) => setTimeout(r, 2500));
  const boot = await page.evaluate(() => ({
    ui: !!window.ENG6UI,
    objects: window.ENG6UI?.state?.objects?.length ?? -1,
    lines: window.ENG6UI?.state?.lines?.length ?? -1,
    demands: window.ENG6UI?.state?.demands?.length ?? -1,
    fleet: window.ENG6UI?.state?.fleet?.length ?? -1,
    treeRows: document.querySelectorAll('#oshTree .trow').length,
    conn: document.querySelector('#connState')?.textContent?.trim(),
    perf: document.querySelector('#perfChip')?.textContent?.trim(),
    gen: document.querySelector('#sbGen')?.textContent?.trim(),
    coords: document.querySelector('#sbCoords')?.textContent?.trim(),
    status: document.querySelector('#resSummary .badge')?.textContent?.trim(),
    grade: document.querySelector('#resSummary .grade')?.textContent?.trim(),
    routes: document.querySelectorAll('#resRoutes tbody tr').length,
    waves: document.querySelectorAll('#resSchedule .wave').length,
    legend: (document.querySelector('#legend')?.textContent || '').length,
    canvasSize: (() => { const c = document.querySelector('#map'); return `${c.width}×${c.height}`; })(),
  }));
  log(`  UI: объектов ${boot.objects}, линий ${boot.lines}, заявок ${boot.demands}, техники ${boot.fleet}`);
  log(`  дерево ОШС: строк ${boot.treeRows} | ядро: «${boot.conn}» | ${boot.perf}`);
  log(`  карта: canvas ${boot.canvasSize} | ${boot.gen}`);
  log(`  встроенный результат: статус ${boot.status}, оценка ${boot.grade}, маршрутов ${boot.routes}, волн ${boot.waves}`);
  await shot(page, '01_workspace_demo');

  log('\n═══ 2. Живой расчёт через API (FR-INT-02/03) ═══');
  await page.click('#btnCalc');
  await new Promise((r) => setTimeout(r, 4000));
  const live = await page.evaluate(() => ({
    conn: document.querySelector('#connState')?.textContent?.trim(),
    status: document.querySelector('#resSummary .badge')?.textContent?.trim(),
    grade: document.querySelector('#resSummary .grade')?.textContent?.trim(),
    metrics: document.querySelector('#resMetrics .kv')?.textContent?.replace(/\s+/g, ' ').slice(0, 300),
    routes: document.querySelectorAll('#resRoutes tbody tr').length,
    waves: document.querySelectorAll('#resSchedule .wave').length,
    issues: document.querySelectorAll('#resIssues .issue').length,
    log: document.querySelectorAll('#logList .logline').length,
  }));
  log(`  соединение: «${live.conn}» → статус ${live.status}, оценка ${live.grade}`);
  log(`  маршрутов ${live.routes}, волн ${live.waves}, замечаний ${live.issues}, записей журнала ${live.log}`);
  await shot(page, '02_live_calculation');

  log('\n═══ 3. Карточка объекта: 3 вкладки (FR-CTX-02) ═══');
  const tabsInfo = await page.evaluate(() => {
    const row = document.querySelector('#oshTree .trow[data-unit]');
    if (!row) return null;
    row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 400, clientY: 300 }));
    const item = Array.from(document.querySelectorAll('#ctxmenu .ctx-i')).find((i) => i.textContent.includes('Карточка'));
    if (item) item.click();
    return {
      title: document.querySelector('#modalTitle')?.textContent,
      tabs: Array.from(document.querySelectorAll('#modalTabs button')).map((b) => b.textContent),
    };
  });
  log(`  ${tabsInfo?.title} → вкладки: ${(tabsInfo?.tabs || []).join(' | ')}`);
  await shot(page, '03_card_equipment');
  await page.evaluate(() => {
    const t = Array.from(document.querySelectorAll('#modalTabs button')).find((b) => b.textContent.includes('Личный состав'));
    t?.click();
  });
  await new Promise((r) => setTimeout(r, 400));
  await shot(page, '04_card_crew');
  await page.evaluate(() => {
    const t = Array.from(document.querySelectorAll('#modalTabs button')).find((b) => b.textContent.includes('Техническое'));
    t?.click();
  });
  await new Promise((r) => setTimeout(r, 400));
  await shot(page, '05_card_techstate');
  await page.evaluate(() => document.querySelector('#modalClose')?.click());

  log('\n═══ 4. Генерализация знаков по масштабу (FR-MAP-07) ═══');
  const gen = {};
  for (const [label, zoomBtns] of [['detail', 3], ['node', 0], ['unit', -4]]) {
    await page.evaluate((n) => {
      for (let i = 0; i < Math.abs(n); i++) document.querySelector(`#zoomCtl button[data-z="${n > 0 ? 1 : -1}"]`)?.click();
    }, zoomBtns);
    await new Promise((r) => setTimeout(r, 700));
    gen[label] = await page.evaluate(() => ({
      level: document.querySelector('#sbGen')?.textContent?.trim(),
      zoom: document.querySelector('#sbZoom')?.textContent?.trim(),
    }));
    log(`  ${label.padEnd(7)} → ${gen[label].zoom} | ${gen[label].level}`);
  }
  await shot(page, '06_generalization_unit');
  // возврат к рабочему масштабу
  await page.evaluate(() => document.querySelector('#zoomCtl button[data-z="0"]')?.click());
  await new Promise((r) => setTimeout(r, 800));
  await shot(page, '07_fit_all');

  log(`\n═══ 5. NFR-01: ${SIGNS} тактических знаков, замер фактического FPS ═══`);
  const fpsRes = await page.evaluate(async (N) => {
    const S = window.ENG6UI.state;
    const base = S.objects.length;
    const lat0 = 55.75, lng0 = 37.62;
    const types = ['cp', 'relay', 'radio', 'sat', 'observer', 'ew_jam', 'ew_rer'];
    for (let i = 0; i < N; i++) {
      const row = Math.floor(i / 40), col = i % 40;
      S.objects.push({
        id: `perf-${i}`, kind: 'asset', nodeType: types[i % types.length], name: `Объект ${i}`,
        lat: lat0 + (row - 15) * 0.012, lng: lng0 + (col - 20) * 0.016,
        condition: ['green', 'green', 'yellow', 'red'][i % 4], spec: {}, status: 'active',
      });
    }
    S.dirty = true;
    // вписываем все объекты, чтобы знаки реально попали в кадр
    document.querySelector('#zoomCtl button[data-z="0"]')?.click();
    await new Promise((r) => setTimeout(r, 2600));
    const chip = document.querySelector('#perfChip')?.textContent?.trim() || '';
    const m = chip.match(/^(\d+)\s*FPS/);
    const signs = (chip.match(/(\d+)\s*зн\./) || [])[1];
    const ms = (chip.match(/([\d.,]+)\s*мс/) || [])[1];
    return { fps: m ? Number(m[1]) : null, signsOnScreen: signs ? Number(signs) : null, frameMs: ms, objectsTotal: S.objects.length, added: S.objects.length - base, chip };
  }, SIGNS);
  log(`  объектов на карте: ${fpsRes.objectsTotal} (добавлено ${fpsRes.added})`);
  log(`  счётчик UI: ${fpsRes.chip}`);
  log(`  FPS: ${fpsRes.fps} | знаков в кадре: ${fpsRes.signsOnScreen} | кадр: ${fpsRes.frameMs} мс`);
  const nfr01 = fpsRes.fps !== null && fpsRes.fps >= 30;
  log(`  ${nfr01 ? '✔' : '✖'} NFR-01 (≥30 FPS при 1000 знаков): ${nfr01 ? 'ВЫПОЛНЕНО' : 'НЕ ВЫПОЛНЕНО'}`);
  if (!nfr01) exitCode = 1;
  await shot(page, '08_nfr01_1000_signs');

  // детальный масштаб при 1000 знаков — худший случай для отрисовки
  await page.evaluate(async () => {
    for (let i = 0; i < 6; i++) document.querySelector('#zoomCtl button[data-z="1"]')?.click();
    await new Promise((r) => setTimeout(r, 2500));
  });
  const fpsDetail = await page.evaluate(() => document.querySelector('#perfChip')?.textContent?.trim());
  log(`  крупный масштаб (все знаки раздельно): ${fpsDetail}`);
  await shot(page, '09_nfr01_detail_zoom');

  log('\n═══ 6. Ошибки консоли ═══');
  const realErrors = pageErrors.concat(consoleErrors.filter((e) => !/Failed to load resource|net::ERR|404|favicon/i.test(e)));
  if (realErrors.length === 0) log('  ✔ ошибок и исключений в консоли нет');
  else {
    log(`  ✖ найдено ${realErrors.length}:`);
    for (const e of realErrors.slice(0, 12)) log('    • ' + e);
    exitCode = 1;
  }

  await browser.close();
} catch (err) {
  log(`✖ ОШИБКА: ${err.message}`);
  if (srvOut) log(srvOut.slice(-800));
  exitCode = 1;
} finally {
  try { srv.kill('SIGKILL'); } catch {}
}
log(`\nскриншоты: ${OUT}`);
process.exit(exitCode);
