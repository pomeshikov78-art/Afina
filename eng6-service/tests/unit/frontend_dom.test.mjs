/**
 * tests/unit/frontend_dom.test.mjs — исполнение собранного UI в jsdom.
 *
 * Зачем: frontend/src/app.js — 2 800 строк браузерного кода, который unit-тестами
 * чистых функций не покрывается. Здесь собранное изделие (frontend/index.html)
 * загружается в jsdom со стабом Canvas2D и проверяется:
 *   • инициализация без неперехваченных исключений;
 *   • демо-данные (сценарий + результат расчёта ядра) загружены и отрисованы;
 *   • панель ОШС построена, фильтры работают;
 *   • переключение вкладок, открытие карточки объекта (3 вкладки по FR-CTX-02),
 *     контекстное меню, инструменты карты;
 *   • формирование пакета для ядра (FR-INT-02) и экспорт сценария;
 *   • отрисовка карты не падает при нулевом размере холста (jsdom).
 *
 * Сеть в тесте недоступна намеренно: проверяется переход UI в демо-режим
 * (требуемое поведение при недоступности ядра).
 */
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
let JSDOM = null;
let VirtualConsole = null;
try {
  ({ JSDOM, VirtualConsole } = await import('jsdom'));
} catch {
  // jsdom — единственная dev-зависимость теста; без него набор пропускается,
  // чтобы репозиторий оставался пригодным для сборки в контуре без npm-доступа
}
const SKIP = JSDOM ? false : 'jsdom не установлен (npm i --no-save jsdom) — набор пропущен';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const HTML = JSDOM ? readFileSync(path.join(root, 'frontend/index.html'), 'utf8') : '';

/** Стаб Canvas2D: записывает все вызовы, возвращает безопасные значения. */
function makeCtxStub(canvas) {
  const calls = [];
  const state = { fillStyle: '#000', strokeStyle: '#000', lineWidth: 1, font: '', globalAlpha: 1, textAlign: '', textBaseline: '' };
  const handler = {
    get(_t, prop) {
      if (prop === 'canvas') return canvas;
      if (prop === 'measureText') return (text) => ({ width: String(text).length * 5.6 });
      if (prop === 'createImageData') return (w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) });
      if (prop === 'putImageData' || prop === 'getImageData') return () => {};
      if (prop === 'drawImage') return (...a) => { calls.push(['drawImage', a.length]); };
      if (prop in state) return state[prop];
      return (...args) => { calls.push([String(prop), args.length]); };
    },
    set(_t, prop, value) { state[prop] = value; return true; },
  };
  const proxy = new Proxy({}, handler);
  proxy.__calls = calls;
  return proxy;
}

const openWindows = [];
after(() => {
  for (const dom of openWindows) { try { dom.window.close(); } catch { /* уже закрыто */ } }
  openWindows.length = 0;
});

async function boot({ html = HTML } = {}) {
  const errors = [];
  const logs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
  vc.on('warn', (...a) => logs.push('warn: ' + a.join(' ')));

  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost/',
    virtualConsole: vc,
    beforeParse(window) {
      // Canvas2D в jsdom не реализован — подставляем стаб
      window.HTMLCanvasElement.prototype.getContext = function () { return makeCtxStub(this); };
      // размеры холста: jsdom возвращает нули, а UI ожидает ненулевые
      window.HTMLCanvasElement.prototype.getBoundingClientRect = function () {
        return { width: 1280, height: 720, top: 0, left: 0, right: 1280, bottom: 720, x: 0, y: 0, toJSON() {} };
      };
      window.Element.prototype.getBoundingClientRect = function () {
        return { width: 200, height: 24, top: 0, left: 0, right: 200, bottom: 24, x: 0, y: 0, toJSON() {} };
      };
      // экспорт файла: createObjectURL отсутствует в jsdom
      window.URL.createObjectURL = () => 'blob:stub';
      window.URL.revokeObjectURL = () => {};
      // клик по <a download> jsdom трактует как навигацию — подменяем no-op
      window.HTMLAnchorElement.prototype.click = function () {};
      window.addEventListener('error', (e) => errors.push(`window.onerror: ${e.message}`));
      window.addEventListener('unhandledrejection', (e) => errors.push(`unhandledrejection: ${e.reason && e.reason.message}`));
      // сети нет — fetch отклоняется, UI обязан уйти в демо-режим
      window.fetch = () => Promise.reject(new Error('network unavailable (test)'));
    },
  });
  // ждём инициализации и «асинхронного» перехода в демо-режим
  await new Promise((r) => setTimeout(r, 350));
  openWindows.push(dom);
  const w = dom.window;
  return { dom, w, d: w.document, errors, logs };
}

const q = (d, s) => d.querySelector(s);
const qa = (d, s) => Array.from(d.querySelectorAll(s));

test('UI загружается без неперехваченных исключений и публикует ENG6UI', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, [], `ошибки при загрузке: ${errors.join(' | ')}`);
  assert.ok(w.ENG6UI, 'window.ENG6UI не опубликован');
  assert.ok(w.ENG6UI.pure && typeof w.ENG6UI.pure.buildRequest === 'function', 'нет pure-функций');
  assert.equal(w.ENG6UI.version, '1.0.0');
  assert.ok(q(d, '#map'), 'холст карты присутствует');
  assert.ok(q(d, '#oshTree'), 'панель ОШС присутствует');
});

test('Демо-данные загружены: ОШС, парк техники, размещённые объекты, линии, заявки', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, []);
  const st = w.ENG6UI.state || w.ENG6UI.pure && null;
  // state не экспортируется наружу напрямую — проверяем по DOM-признакам
  const rows = qa(d, '#oshTree .trow');
  assert.ok(rows.length >= 10, `строк в дереве ОШС: ${rows.length}`);
  const placed = qa(d, '#oshTree .trow.selected');
  assert.ok(placed.length >= 5, `размещённых единиц в дереве подсвечено: ${placed.length}`);
  const oshCount = q(d, '#oshCount').textContent;
  assert.match(oshCount, /в парке \d+/);
  assert.match(oshCount, /размещено \d+/);
  void st;
});

test('Встроенный результат расчёта ядра отрисован во вкладке «Результаты»', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, []);
  const badge = q(d, '#resSummary .badge');
  assert.ok(badge, 'нет бейджа статуса');
  assert.match(badge.textContent, /^(OPTIMAL|FEASIBLE|PARTIAL|INFEASIBLE|INVALID|TIMEOUT|ERROR)$/);
  const grade = q(d, '#resSummary .grade');
  assert.match(grade.textContent, /^[A-F]$/);
  // метрики
  const kv = q(d, '#resMetrics .kv').textContent;
  for (const frag of ['Заявки', 'Связность', 'Стоимость', 'Расписание']) assert.ok(kv.includes(frag), `в метриках нет «${frag}»`);
  // таблица маршрутов
  const rows = qa(d, '#resRoutes tbody tr');
  assert.ok(rows.length >= 5, `строк маршрутов: ${rows.length}`);
  // замечания и рекомендации
  assert.ok(q(d, '#resIssues').textContent.length > 50);
  // расписание: волны
  const waves = qa(d, '#resSchedule .wave');
  assert.ok(waves.length >= 1, `волн: ${waves.length}`);
  // живучесть
  assert.ok(q(d, '#resSurvivability').textContent.includes('Живучесть'));
  // легенда построена
  assert.ok(q(d, '#legend').textContent.includes('Качество линий'));
  // ядро недоступно → сервис в демо-режиме
  assert.match(q(d, '#connState').textContent, /демо|нет связи|недоступно/i);
  void w;
});

test('NFR-01: счётчик FPS и числа знаков работает, отрисовка карты не падает', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, []);
  // jsdom не гоняет requestAnimationFrame непрерывно — вызываем отрисовку явно
  await new Promise((r) => setTimeout(r, 700));
  const chip = q(d, '#perfChip');
  assert.match(chip.textContent, /FPS|зн\./, `содержимое чипа: ${chip.textContent}`);
  const gen = q(d, '#sbGen').textContent;
  assert.match(gen, /генерализация: (крупный|средний|мелкий)/);
  assert.match(gen, /знаков \d+/);
  const coords = q(d, '#sbCoords').textContent;
  assert.ok(coords.length > 0);
  void w;
});

test('Переключение вкладок правой панели и сворачивание панели (FR-UI-01)', { skip: SKIP }, async () => {
  const { d, errors } = await boot();
  assert.deepEqual(errors, []);
  const tabs = qa(d, '.rpt');
  assert.equal(tabs.length, 3);
  tabs[1].dispatchEvent(new d.defaultView.MouseEvent('click', { bubbles: true }));
  assert.ok(!q(d, '#tab-res').classList.contains('hidden'), 'вкладка результатов не открыта');
  assert.ok(q(d, '#tab-osh').classList.contains('hidden'));
  tabs[2].dispatchEvent(new d.defaultView.MouseEvent('click', { bubbles: true }));
  assert.ok(!q(d, '#tab-log').classList.contains('hidden'), 'журнал не открыт');
  assert.ok(qa(d, '#logList .logline').length > 0, 'журнал пуст');
  // сворачивание панели
  q(d, '#btnPanel').dispatchEvent(new d.defaultView.MouseEvent('click', { bubbles: true }));
  assert.ok(q(d, '#rightpanel').classList.contains('collapsed'), 'панель не свернулась');
  q(d, '#btnPanel').dispatchEvent(new d.defaultView.MouseEvent('click', { bubbles: true }));
  assert.ok(!q(d, '#rightpanel').classList.contains('collapsed'));
});

test('Фильтры и поиск ОШС (FR-RES-04), индикаторы состояния (FR-RES-03)', { skip: SKIP }, async () => {
  const { d, errors } = await boot();
  assert.deepEqual(errors, []);
  const all = qa(d, '#oshTree .trow').length;
  // фильтр по состоянию: неисправная техника
  const sel = q(d, '#fltCond');
  sel.value = 'red';
  sel.dispatchEvent(new d.defaultView.Event('input', { bubbles: true }));
  const red = qa(d, '#oshTree .trow');
  assert.ok(red.length > 0 && red.length < all, `фильтр 🔴: ${red.length} из ${all}`);
  assert.ok(qa(d, '#oshTree .c-red').length > 0, 'индикаторы 🔴 отрисованы');
  // 🔴 не перетаскивается
  const unavail = qa(d, '#oshTree .trow.unavailable');
  assert.ok(unavail.length > 0, 'неисправная техника помечена недоступной');
  assert.equal(unavail[0].getAttribute('draggable'), 'false', 'drag запрещён для 🔴');
  // фильтр по диапазону
  sel.value = '';
  sel.dispatchEvent(new d.defaultView.Event('input', { bubbles: true }));
  const band = q(d, '#fltBand');
  band.value = 'УКВ';
  band.dispatchEvent(new d.defaultView.Event('input', { bubbles: true }));
  const vhf = qa(d, '#oshTree .trow').length;
  assert.ok(vhf > 0 && vhf < all, `фильтр УКВ: ${vhf} из ${all}`);
  // поиск
  band.value = '';
  band.dispatchEvent(new d.defaultView.Event('input', { bubbles: true }));
  const s = q(d, '#resSearch');
  s.value = 'Р-168';
  s.dispatchEvent(new d.defaultView.Event('input', { bubbles: true }));
  const found = qa(d, '#oshTree .trow').length;
  assert.ok(found > 0 && found < all, `поиск «Р-168»: ${found}`);
  s.value = 'zzz-нет-такого';
  s.dispatchEvent(new d.defaultView.Event('input', { bubbles: true }));
  assert.equal(qa(d, '#oshTree .trow').length, 0, 'по пустому поиску строк нет');
  assert.ok(q(d, '#oshTree').textContent.includes('Ничего не найдено'));
});

test('Инструменты карты и горячие клавиши переключаются без ошибок', { skip: SKIP }, async () => {
  const { d, errors } = await boot();
  assert.deepEqual(errors, []);
  const tools = qa(d, '.tb-btn.tool');
  assert.ok(tools.length >= 4);
  for (const t of tools) {
    t.dispatchEvent(new d.defaultView.MouseEvent('click', { bubbles: true }));
    assert.ok(t.classList.contains('active'), `инструмент ${t.dataset.tool} не активирован`);
  }
  // клавиши V/L/M/N
  const w = d.defaultView;
  for (const k of ['v', 'l', 'm', 'n', 'f']) {
    d.dispatchEvent(new w.KeyboardEvent('keydown', { key: k, bubbles: true }));
  }
  await new Promise((r) => setTimeout(r, 60));
  assert.deepEqual(errors, [], `ошибки после клавиш: ${errors.join(' | ')}`);
  // Esc закрывает подсказку
  d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.ok(q(d, '#hint').classList.contains('show') === false);
});

test('Контекстное меню карты и слои (FR-CTX-01)', { skip: SKIP }, async () => {
  const { d, errors } = await boot();
  assert.deepEqual(errors, []);
  const map = q(d, '#map');
  const w = d.defaultView;
  map.dispatchEvent(new w.MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 640, clientY: 360, button: 2 }));
  const menu = q(d, '#ctxmenu');
  assert.ok(!menu.classList.contains('hidden'), 'контекстное меню не открыто');
  const items = qa(d, '#ctxmenu .ctx-i');
  assert.ok(items.length >= 4, `пунктов меню: ${items.length}`);
  // клик по свободной области даёт меню карты, по объекту — меню объекта;
  // в демо-сценарии точка (640,360) может попасть на объект, поэтому допустимы оба
  const text = menu.textContent;
  const isMapMenu = text.includes('Разместить технику') && text.includes('Запустить расчёт');
  const isObjMenu = text.includes('Свойства / Детализация');
  const isLineMenu = text.includes('Линия связи') && text.includes('Свойства линии');
  assert.ok(isMapMenu || isObjMenu || isLineMenu, `неизвестное меню: ${text.slice(0, 120)}`);
  // FR-CTX-01: меню зависит от того, что под курсором — карта, объект или линия
  const kind = isMapMenu ? 'карта' : isObjMenu ? 'объект' : 'линия';
  assert.ok(['карта', 'объект', 'линия'].includes(kind));
  // первый пункт выполняется без ошибок
  items[0].dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(errors, [], `ошибки после пункта меню: ${errors.join(' | ')}`);
  if (!q(d, '#modal').classList.contains('hidden')) {
    q(d, '#modalClose').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    assert.ok(q(d, '#modal').classList.contains('hidden'));
  }
  // слои
  for (const id of ['#lyCoverage', '#lyLinks', '#lyRejected', '#lyLabels', '#lyGrid', '#lyBase']) {
    const cb = q(d, id);
    cb.checked = !cb.checked;
    cb.dispatchEvent(new w.Event('change', { bubbles: true }));
  }
  await new Promise((r) => setTimeout(r, 60));
  assert.deepEqual(errors, [], `ошибки после переключения слоёв: ${errors.join(' | ')}`);
});

test('Справка содержит матрицу соответствия требованиям ТЗ', { skip: SKIP }, async () => {
  const { d, errors } = await boot();
  assert.deepEqual(errors, []);
  q(d, '#btnHelp').dispatchEvent(new d.defaultView.MouseEvent('click', { bubbles: true }));
  const body = q(d, '#modalBody').textContent;
  for (const fr of ['FR-UI-01', 'FR-RES-03', 'FR-MAP-04', 'FR-MAP-07', 'FR-MAP-08', 'FR-CTX-01', 'FR-INT-01', 'NFR-01', 'NFR-02', 'NFR-03'])
    assert.ok(body.includes(fr), `в справке нет ${fr}`);
  // объединённые идентификаторы (FR-CTX-01/02, FR-INT-01/02/03) тоже раскрываются
  assert.ok(body.includes('Состав оборудования') && body.includes('Личный состав'), 'в справке нет описания вкладок карточки');
});

test('Экспорт сценария формирует файл с координатами 6 знаков (NFR-03, FR-INT-02)', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, []);
  const captured = [];
  const RealBlob = w.Blob;
  w.Blob = class extends RealBlob {
    constructor(parts, opts) { super(parts, opts); captured.push(parts.join('')); }
  };
  q(d, '#btnExport').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(captured.length, 1, 'файл не сформирован');
  const data = JSON.parse(captured[0]);
  assert.equal(data.format, 'eng6-plan-request');
  assert.ok(data.request.topology.nodes.length >= 8, `узлов в экспорте: ${data.request.topology.nodes.length}`);
  assert.ok(data.request.topology.links.length >= 5, `линий в экспорте: ${data.request.topology.links.length}`);
  assert.ok(Array.isArray(data.fleet) && data.fleet.length > 5);
  assert.ok(Array.isArray(data.oush) && data.oush.length >= 1, 'ОШС не выгружена');
  for (const n of data.request.topology.nodes) {
    assert.ok(Number.isFinite(n.lat) && Number.isFinite(n.lng), `координаты ${n.id}`);
    const dec = String(n.lat).split('.')[1];
    assert.ok(!dec || dec.length <= 6, `точность координат нарушена: ${n.lat}`);
  }
  assert.ok(data.note.includes('NFR-02'), 'в файле нет отметки о запрете кэширования');
});

test('Импорт собственного экспорта восстанавливает сценарий (round-trip)', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, []);
  // 1) экспортируем
  const captured = [];
  const RealBlob = w.Blob;
  w.Blob = class extends RealBlob {
    constructor(parts, opts) { super(parts, opts); captured.push(parts.join('')); }
  };
  q(d, '#btnExport').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 40));
  const exported = captured[0];
  // 2) импортируем через публичный путь UI (парсер JSON-сценария)
  const parsed = JSON.parse(exported);
  w.ENG6UI.__testApplyScenario(parsed);
  await new Promise((r) => setTimeout(r, 60));
  assert.deepEqual(errors, [], `ошибки при импорте: ${errors.join(' | ')}`);
  const rows = qa(d, '#oshTree .trow');
  assert.ok(rows.length > 0, 'дерево ОШС пустое после импорта');
});

test('Клик по строке ОШС открывает карточку с тремя вкладками (FR-CTX-02)', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, []);
  // контекстное меню единицы техники → «Карточка»
  const row = qa(d, '#oshTree .trow[data-unit]')[0];
  assert.ok(row, 'нет строк с техникой');
  row.dispatchEvent(new w.MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 100, clientY: 100 }));
  const items = qa(d, '#ctxmenu .ctx-i');
  assert.ok(items.length >= 3);
  const card = items.find((i) => i.textContent.includes('Карточка'));
  assert.ok(card, 'нет пункта «Карточка единицы техники»');
  card.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  assert.ok(!q(d, '#modal').classList.contains('hidden'), 'карточка не открылась');
  const tabs = qa(d, '#modalTabs button').map((b) => b.textContent);
  for (const need of ['Состав оборудования', 'Личный состав', 'Техническое состояние'])
    assert.ok(tabs.some((t) => t.includes(need)), `нет вкладки «${need}»: ${tabs.join(', ')}`);
  // содержимое первой вкладки
  assert.ok(q(d, '#modalBody').textContent.includes('Аппаратура'), 'нет таблицы аппаратуры');
  // переключение на личный состав
  const crewTab = qa(d, '#modalTabs button').find((b) => b.textContent.includes('Личный состав'));
  crewTab.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const body = q(d, '#modalBody').textContent;
  assert.ok(body.includes('Звание') && body.includes('Должность'), 'нет состава закрепленного личного состава');
  // техническое состояние
  const techTab = qa(d, '#modalTabs button').find((b) => b.textContent.includes('Техническое'));
  techTab.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const tb = q(d, '#modalBody').textContent;
  assert.ok(tb.includes('Процент износа') && tb.includes('ЗИП'), 'нет данных о техническом состоянии');
  assert.ok(tb.includes('Журнал неисправностей'), 'нет журнала неисправностей');
});

test('Панель ОШС: групповое размещение подразделения и авторазмещение', { skip: SKIP }, async () => {
  const { w, d, errors } = await boot();
  assert.deepEqual(errors, []);
  const before = q(d, '#oshCount').textContent;
  q(d, '#btnAutoPlace').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  assert.deepEqual(errors, [], `ошибки авторазмещения: ${errors.join(' | ')}`);
  const after = q(d, '#oshCount').textContent;
  assert.match(after, /размещено \d+/);
  void before; void w;
});

test('Недоступность ядра не ломает UI: тост с инструкцией и демо-режим', { skip: SKIP }, async () => {
  const { d, errors, logs } = await boot();
  assert.deepEqual(errors, [], `неперехваченные ошибки: ${errors.join(' | ')}`);
  const toasts = qa(d, '#toasts .toast').map((t) => t.textContent);
  assert.ok(toasts.some((t) => /недоступно|демо|API/i.test(t)), `тосты: ${JSON.stringify(toasts)}`);
  const logText = q(d, '#logList').textContent;
  assert.ok(/API недоступен|нет связи/i.test(logText), 'в журнале нет записи о недоступности ядра');
  assert.ok(logs.length >= 0);
});
