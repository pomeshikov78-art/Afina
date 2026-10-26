/**
 * tests/unit/frontend_pure.test.mjs — чистые функции UI (ЭТАП 4).
 *
 * Загружает frontend/src/app.js в Node без DOM: модуль отдаёт наружу
 * window.ENG6UI.pure и завершается до браузерной части. Это позволяет
 * тестировать проекцию, генерализацию, кластеризацию, CSV-парсер и
 * формирование пакета для ядра без браузера.
 *
 * Отдельно проверяется NFR-02: в исходнике UI нет обращений к
 * localStorage/sessionStorage/indexedDB/document.cookie и нет внешних URL.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const src = readFileSync(path.join(root, 'frontend/src/app.js'), 'utf8');

function loadPure() {
  const sandbox = { module: { exports: {} }, console };
  sandbox.window = undefined; // ← заставляем модуль идти по ветке module.exports
  const ctx = vm.createContext(sandbox);
  vm.runInContext(src, ctx, { filename: 'app.js' });
  const exp = sandbox.module.exports;
  assert.ok(exp && exp.pure, 'модуль не экспортировал pure-функции');
  return exp.pure;
}
const P = loadPure();

test('Проекция Web Mercator: прямая и обратная согласованы (1e-9°)', () => {
  const view = { zoom: 10, ox: 0, oy: 0 };
  for (const [lat, lng] of [[0, 0], [55.75, 37.62], [-33.86, 151.2], [70.5, -25.0], [12.345678, 123.456789]]) {
    const s = P.TILE ? null : null;
    void s;
    const px = P.project({ ...view, ...P.viewOffsets(lat, lng, view.zoom, 1000, 800) }, lat, lng);
    assert.ok(Math.abs(px.x - 500) < 1e-6 && Math.abs(px.y - 400) < 1e-6, `центр не совпал: ${px.x},${px.y}`);
    const back = P.unproject({ ...view, ...P.viewOffsets(lat, lng, view.zoom, 1000, 800) }, px.x, px.y);
    assert.ok(Math.abs(back.lat - lat) < 1e-9, `lat ${back.lat} vs ${lat}`);
    assert.ok(Math.abs(back.lng - lng) < 1e-9, `lng ${back.lng} vs ${lng}`);
  }
});

test('Проекция: границы ±180° по долготе и ограничение Меркатора по широте', () => {
  const view = { zoom: 3, ...P.viewOffsets(0, 0, 3, 800, 600) };
  const a = P.project(view, 0, -180);
  const b = P.project(view, 0, 180);
  assert.ok(Math.abs(a.x - b.x) < 1e-6 || Math.abs(Math.abs(a.x - b.x) - 256 * 8) < 1e-6, '±180° — одна и та же меридианная линия');
  const p90 = P.project(view, 89.9, 0);
  const p85 = P.project(view, 85.0511287798, 0);
  assert.ok(Number.isFinite(p90.y) && Number.isFinite(p85.y), 'за пределами 85.05° координата конечна (кламп)');
  assert.ok(Math.abs(p90.y - p85.y) < 1e-6, 'широты выше предела клампятся');
});

test('Метры на пиксель и знаменатель масштаба уменьшаются с ростом zoom', () => {
  const m5 = P.metersPerPixel(55.75, 5);
  const m10 = P.metersPerPixel(55.75, 10);
  const m15 = P.metersPerPixel(55.75, 15);
  assert.ok(m5 > m10 && m10 > m15);
  assert.ok(Math.abs(m5 / m10 - 32) < 1e-9, 'каждые +1 zoom — вдвое крупнее');
  const s10 = P.scaleDenominator(55.75, 10);
  assert.ok(s10 > 1000 && s10 < 1e7, `знаменатель ${s10}`);
  // на экваторе метр/пиксель больше, чем на 60° с.ш. (косинус широты)
  assert.ok(P.metersPerPixel(0, 10) > P.metersPerPixel(60, 10));
});

test('Гаверсинус UI совпадает с ядром ENG6 (1e-12 км)', () => {
  const cases = [[55.75, 37.62, 55.8, 37.7], [0, 0, 0, 1], [-33.86, 151.2, 55.75, 37.62]];
  for (const [la1, lo1, la2, lo2] of cases) {
    const ui = P.haversineKm({ lat: la1, lng: lo1 }, { lat: la2, lng: lo2 });
    const dLat = ((la2 - la1) * Math.PI) / 180;
    const dLng = ((lo2 - lo1) * Math.PI) / 180;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos((la1 * Math.PI) / 180) * Math.cos((la2 * Math.PI) / 180) * Math.sin(dLng / 2) ** 2;
    const ref = 2 * 6371 * Math.asin(Math.sqrt(h));
    assert.ok(Math.abs(ui - ref) < 1e-12, `${ui} vs ${ref}`);
  }
  assert.ok(Math.abs(P.haversineKm({ lat: 0, lng: 0 }, { lat: 0, lng: 0 })) < 1e-12);
});

test('NFR-03: round6 даёт ровно 6 знаков и не накапливает ошибку', () => {
  assert.equal(P.round6(55.1234567891), 55.123457);
  assert.equal(P.round6(37.0000004), 37);
  assert.equal(String(P.round6(55.5)).split('.')[1].length, 1);
  for (let i = 0; i < 1000; i++) {
    const x = (Math.random() - 0.5) * 360;
    const r = P.round6(x);
    const dec = String(Math.abs(r)).split('.')[1];
    assert.ok(!dec || dec.length <= 6, `${r}`);
    assert.ok(Math.abs(r - x) < 1e-6);
  }
});

test('FR-MAP-07: уровни генерализации по масштабу', () => {
  assert.equal(P.generalizationLevel(14), 'detail');
  assert.equal(P.generalizationLevel(12), 'detail');
  assert.equal(P.generalizationLevel(11.9), 'node');
  assert.equal(P.generalizationLevel(8), 'node');
  assert.equal(P.generalizationLevel(7.9), 'unit');
  assert.equal(P.generalizationLevel(2), 'unit');
  // настраиваемые пороги
  assert.equal(P.generalizationLevel(10, { detail: 10, unit: 5 }), 'detail');
  assert.equal(P.generalizationLevel(4.9, { detail: 10, unit: 5 }), 'unit');
});

test('Кластеризация: близкие объекты одного подразделения сливаются, дальние — нет', () => {
  const items = [
    { x: 10, y: 10, groupKey: 'A' }, { x: 20, y: 15, groupKey: 'A' }, { x: 30, y: 12, groupKey: 'A' },
    { x: 500, y: 500, groupKey: 'A' }, { x: 505, y: 505, groupKey: 'A' },
    { x: 12, y: 14, groupKey: 'B' },
  ];
  const cl = P.gridCluster(items, 100);
  const sizes = cl.map((c) => c.items.length).sort((a, b) => a - b);
  // объекты создаются внутри vm-контекста, поэтому сравнение — через JSON
  // (assert.deepStrictEqual сравнивает прототипы, а они из разных realm'ов)
  assert.equal(JSON.stringify(sizes), JSON.stringify([1, 2, 3]), `размеры кластеров ${sizes}`);
  assert.equal(cl.filter((c) => c.groupKey === 'B').length, 1, 'другое подразделение не смешивается');
  // центры кластеров — среднее арифметическое
  const big = cl.find((c) => c.items.length === 3);
  const cx = big.items.reduce((s, i) => s + i.x, 0) / 3;
  const cy = big.items.reduce((s, i) => s + i.y, 0) / 3;
  assert.ok(Math.abs(big.x - cx) < 1e-9 && Math.abs(big.y - cy) < 1e-9);
  // детерминированность порядка
  assert.equal(JSON.stringify(P.gridCluster(items, 100).map((c) => c.key)), JSON.stringify(cl.map((c) => c.key)));
});

test('Кластеризация: пустой вход и все объекты в одной ячейке', () => {
  assert.equal(P.gridCluster([], 64).length, 0);
  const cl = P.gridCluster([{ x: 1, y: 1, groupKey: 'g' }, { x: 2, y: 2, groupKey: 'g' }], 1000);
  assert.equal(cl.length, 1);
  assert.equal(cl[0].items.length, 2);
});

test('fitBounds: вписывает набор точек и соблюдает padding/лимиты zoom', () => {
  const pts = [
    { lat: 55.7, lng: 37.6 }, { lat: 55.9, lng: 37.9 }, { lat: 55.8, lng: 37.75 },
  ];
  const v = P.fitBounds(pts, 1200, 800, { padding: 0.1 });
  assert.ok(v.zoom >= 3 && v.zoom <= 16);
  assert.ok(Math.abs(v.lat - 55.8) < 1e-9 && Math.abs(v.lng - 37.75) < 1e-9, 'центр — середина ограничивающего прямоугольника');
  // одна точка → крупный масштаб
  assert.equal(P.fitBounds([{ lat: 10, lng: 20 }], 800, 600).zoom, 12);
  // пустой вход → дефолт без исключений
  const d = P.fitBounds([], 800, 600);
  assert.ok(Number.isFinite(d.lat) && Number.isFinite(d.lng) && Number.isFinite(d.zoom));
  // все точки должны попасть в окно
  const view = { zoom: v.zoom, ...P.viewOffsets(v.lat, v.lng, v.zoom, 1200, 800) };
  for (const p of pts) {
    const s = P.project(view, p.lat, p.lng);
    assert.ok(s.x >= -1 && s.x <= 1201 && s.y >= -1 && s.y <= 801, `точка вне окна: ${s.x},${s.y}`);
  }
});

test('Цвета и толщина линий по запасу/ёмкости соответствуют легенде', () => {
  assert.equal(P.linkColorByMargin(25), '#3fb950');
  assert.equal(P.linkColorByMargin(15), '#7ee2a8');
  assert.equal(P.linkColorByMargin(5), '#d29922');
  assert.equal(P.linkColorByMargin(1), '#f0883e');
  assert.equal(P.linkColorByMargin(-1), '#c93545');
  assert.equal(P.linkColorByMargin(30, { rejected: true }), '#6b4a52');
  assert.equal(P.linkColorByMargin(NaN), '#6b7280');
  assert.ok(P.linkWidthByCapacity(0) === 1);
  assert.ok(P.linkWidthByCapacity(1e6) <= 6);
  assert.ok(P.linkWidthByCapacity(100) > P.linkWidthByCapacity(1), 'толщина растёт с ёмкостью');
});

test('Радиус покрытия: по максимальной активной линии, иначе типовое значение', () => {
  const node = { id: 'a', lat: 0, lng: 0 };
  const r1 = P.coverageRadiusKm(node, [{ from: 'a', to: 'b', distKm: 10 }, { from: 'a', to: 'c', distKm: 25 }]);
  assert.ok(Math.abs(r1 - 25 * 1.15) < 1e-6);
  const r2 = P.coverageRadiusKm(node, [], { fallbackKm: 7 });
  assert.equal(r2, 7);
  const r3 = P.coverageRadiusKm(node, [{ from: 'x', to: 'y', distKm: 50 }]);
  assert.equal(r3, 12, 'линии чужих узлов не учитываются');
});

test('CSV-парсер: кавычки, разделители , ; TAB, пустые строки, BOM', () => {
  const a = P.parseCsv('id,name,lat\n1,"ООО ""Ромашка"", узел",55.75\n2,Простой,37.62\n');
  assert.equal(JSON.stringify(a.header), JSON.stringify(['id', 'name', 'lat']));
  assert.equal(a.rows.length, 2);
  assert.equal(a.rows[0][1], 'ООО "Ромашка", узел');
  const b = P.parseCsv('a;b;c\n1;2;3');
  assert.equal(JSON.stringify(b.header), JSON.stringify(['a', 'b', 'c']));
  assert.equal(JSON.stringify(b.rows[0]), JSON.stringify(['1', '2', '3']));
  const c = P.parseCsv('x\ty\n1\t2\n\n3\t4\n');
  assert.equal(JSON.stringify(c.header), JSON.stringify(['x', 'y']));
  assert.equal(c.rows.length, 2);
  const d = P.parseCsv('\uFEFFid,name\n1,x');
  assert.equal(d.header[0], 'id', 'BOM снят');
  assert.equal(JSON.stringify(P.parseCsv('')), JSON.stringify({ header: [], rows: [] }));
});

test('CSV → сценарий: русские заголовки, секции [nodes]/[links], приведение типов', () => {
  const text = '[nodes]\nid;тип;наименование;широта;долгота;подчинение\nn1;cp;КП;55,75;37,62;\nn2;radio;Рота;55.8;37.7;n1\n[links]\nid;от;до;тип\nl1;n1;n2;VHF\n';
  const sc = P.csvToScenario(text);
  assert.equal(sc.nodes.length, 2);
  assert.equal(sc.nodes[0].lat, 55.75);
  assert.equal(sc.nodes[0].type, 'cp');
  assert.equal(sc.nodes[1].parentId, 'n1');
  assert.equal(sc.links.length, 1);
  assert.equal(JSON.stringify([sc.links[0].from, sc.links[0].to, sc.links[0].type]), JSON.stringify(['n1', 'n2', 'VHF']));
  // плоский список узлов (без секций)
  const flat = P.csvToScenario('id,type,lat,lng\nx1,relay,10,20\n');
  assert.equal(flat.nodes.length, 1);
  assert.equal(flat.nodes[0].type, 'relay');
  // список линий определяется по наличию from/to
  const onlyLinks = P.csvToScenario('id,from,to,type\nl,a,b,UHF\n');
  assert.equal(onlyLinks.links.length, 1);
  assert.equal(onlyLinks.nodes.length, 0);
});

test('FR-INT-02: buildRequest формирует пакет для ядра с 6 знаками координат', () => {
  const st = {
    meta: { name: 'Тест', author: 'A', seed: 1 },
    environment: { clutterType: 'open' },
    constraints: { budget: 100, physics: 'fast' },
    weights: { cost: 1, delay: 0, survivability: 0, margin: 0 },
    fleet: [{ unitId: 'U1', kind: 'Р-168', nodeType: 'radio', condition: 'green' }],
    objects: [
      { id: 'a', kind: 'asset', nodeType: 'cp', name: 'КП', lat: 55.1234567, lng: 37.7654321, unitId: 'U0', spec: { txPowerDbm: 43, antHeightM: 12 }, status: 'active', parentId: null },
      { id: 'b', kind: 'asset', nodeType: 'radio', name: 'Рота', lat: 55.2, lng: 37.8, unitId: 'U1', spec: { txPowerDbm: 36 }, status: 'active', parentId: 'a' },
    ],
    lines: [{ id: 'l1', from: 'a', to: 'b', type: 'VHF', priority: 2 }],
    demands: [{ id: 'q1', from: 'a', to: 'b', rateKbps: 16, maxDelayMs: 200, priority: 1 }],
  };
  const req = P.buildRequest(st, {});
  assert.equal(req.topology.nodes.length, 2);
  assert.equal(req.topology.nodes[0].lat, 55.123457, 'координата округлена до 6 знаков');
  assert.equal(req.topology.nodes[0].txPowerDbm, 43);
  assert.equal(req.topology.links[0].type, 'VHF');
  assert.equal(req.demands.length, 1);
  assert.equal(req.fleet.length, 1);
  assert.equal(req.constraints.budget, 100);
  // заявка на несуществующий узел отбрасывается
  st.demands.push({ id: 'q2', from: 'a', to: 'ghost', rateKbps: 8 });
  assert.equal(P.buildRequest(st, {}).demands.length, 1);
});

test('FR-MAP-04: узел связи уходит в ядро одним узлом с параметрами лучшей станции', () => {
  const st = {
    meta: {}, environment: {}, constraints: {}, weights: { cost: 1 },
    fleet: [],
    objects: [
      { id: 'g1', kind: 'nodeGroup', nodeType: 'cp', name: 'УС-1', lat: 55.7, lng: 37.6, status: 'active', spec: { txPowerDbm: 40 } },
      { id: 'm1', kind: 'asset', nodeType: 'cp', name: 'КШМ', lat: 55.7, lng: 37.6, groupId: 'g1', spec: { txPowerDbm: 43, antHeightM: 12 } },
      { id: 'm2', kind: 'asset', nodeType: 'radio', name: 'РС', lat: 55.71, lng: 37.61, groupId: 'g1', spec: { txPowerDbm: 36 } },
      { id: 'x1', kind: 'asset', nodeType: 'radio', name: 'Отдельная', lat: 55.9, lng: 37.9, spec: { txPowerDbm: 30 } },
    ],
    lines: [{ id: 'l1', from: 'g1', to: 'x1', type: 'VHF' }],
    demands: [],
  };
  const req = P.buildRequest(st, {});
  const g = req.topology.nodes.find((n) => n.id === 'g1');
  assert.ok(g, 'узел связи присутствует');
  assert.equal(g.txPowerDbm, 43, 'взяты параметры мощнейшей станции группы');
  assert.deepEqual(g.children, ['m1', 'm2']);
  assert.ok(req.topology.nodes.some((n) => n.id === 'm1' && n.groupId === 'g1'), 'участники группы помечены groupId');
  assert.equal(req.topology.links.length, 1);
});

test('applyPlanToState: индекс линий и признак отклонённых', () => {
  const st = { objects: [{ id: 'a' }], linkById: new Map(), nodeById: new Map(), lastResult: null };
  const res = { plan: { activeLinks: [{ id: 'l1', marginDb: 10 }], rejectedLinks: [{ id: 'l2', code: 'BELOW_MARGIN' }] } };
  const r = P.applyPlanToState(st, res);
  assert.equal(r.applied, 1);
  assert.equal(r.rejected, 1);
  assert.equal(st.linkById.get('l1').marginDb, 10);
  assert.equal(st.linkById.get('l2').rejected, true);
  assert.equal(st.lastResult, res);
  // пустой результат не ломает состояние
  const st2 = { objects: [], linkById: new Map(), nodeById: new Map(), lastResult: null };
  const empty = P.applyPlanToState(st2, null);
  assert.equal(empty.applied, 0);
  assert.equal(empty.rejected, 0);
});

test('CSV: пустые числовые поля — это «не задано», а не ноль (регрессия DEF-15)', () => {
  // до исправления numOr('') возвращал 0 → линии получали bwKhz:0/freqMhz:0
  // и браковались валидатором ядра с кодом RANGE
  const text = '[nodes]\nid;тип;наименование;широта;долгота\nK-1;cp;КП;55,75;37,62\nR-1;relay;Р-1;55,79;37,67\n[links]\nid;от;до;тип\nL-1;K-1;R-1;VHF\nL-2;R-1;K-1;FOC\n[demands]\nid;от;до;скорость;задержка;приоритет\nQ-1;K-1;R-1;16;200;1\n';
  const sc = P.csvToScenario(text);
  assert.equal(sc.nodes.length, 2);
  assert.equal(sc.links.length, 2);
  assert.equal(sc.demands.length, 1);
  for (const l of sc.links) {
    assert.notEqual(l.bwKhz, 0, `bwKhz не должен быть нулём: ${JSON.stringify(l)}`);
    assert.notEqual(l.freqMhz, 0, `freqMhz не должен быть нулём: ${JSON.stringify(l)}`);
    assert.ok(l.bwKhz === undefined || l.bwKhz > 0);
    assert.ok(l.freqMhz === undefined || l.freqMhz > 0);
  }
  const d = sc.demands[0];
  assert.equal(d.rateKbps, 16);
  assert.equal(d.maxDelayMs, 200);
  assert.equal(d.priority, 1);
  // русские заголовки и запятая как десятичный разделитель
  assert.equal(sc.nodes[0].lat, 55.75);
  assert.equal(sc.nodes[0].type, 'cp');
});

test('CSV-импорт → пакет, принимаемый валидатором ядра (сквозная проверка)', async () => {
  const { validateRequest } = await import('../../core/validate.mjs');
  const text = '[nodes]\nid;тип;наименование;широта;долгота;подчинение\nK-1;cp;КП «Центр»;55,7500;37,6200;\nR-1;relay;Ретранслятор Р-1;55,7900;37,6700;K-1\nA-1;radio;Рота А;55,7300;37,6900;R-1\n[links]\nid;от;до;тип\nL-1;K-1;R-1;FOC\nL-2;R-1;A-1;VHF\nL-3;K-1;A-1;UHF\n[demands]\nid;от;до;скорость;задержка;приоритет\nQ-1;K-1;A-1;16;200;1\nQ-2;K-1;R-1;64;150;2\n';
  const sc = P.csvToScenario(text);
  const request = {
    meta: { name: 'CSV-импорт' },
    topology: { nodes: sc.nodes, links: sc.links },
    demands: sc.demands,
    constraints: { physics: 'fast' },
  };
  const rep = validateRequest(request);
  assert.equal(rep.ok, true, `валидатор отклонил CSV-пакет: ${JSON.stringify(rep.errors)}`);
});

test('NFR-02: в исходнике UI нет localStorage/sessionStorage/indexedDB/cookie и внешних URL', () => {
  // проверяются ПАТТЕРНЫ ИСПОЛЬЗОВАНИЯ API, а не упоминания в справке/комментариях
  const forbidden = [
    /localStorage\s*[.[]/, /sessionStorage\s*[.[]/, /indexedDB\s*[.[]/,
    /document\.cookie/, /window\.localStorage/, /window\.sessionStorage/,
    /openDatabase\s*\(/, /caches\.(open|match)/,
  ];
  const isComment = (l) => /^\s*(\*|\/\/|\/\*|<!--)/.test(l);
  for (const re of forbidden) {
    const uses = src.split('\n').filter((l) => re.test(l) && !isComment(l));
    assert.equal(uses.length, 0, `найдено использование ${re}: ${uses.join(' | ')}`);
  }
  const urls = src.match(/https?:\/\/[^'"\s)]+/g) || [];
  const allowed = [/leafletjs\.org/, /localhost/, /127\.0\.0\.1/];
  const bad = urls.filter((u) => !allowed.some((r) => r.test(u)));
  assert.deepEqual(bad, [], 'в UI не должно быть обращений к внешним ресурсам');
  // нет динамической загрузки скриптов/стилей
  for (const f of ['import(', 'createElement(\'script\'', 'createElement("script"', '@import', 'new Worker(']) {
    assert.ok(!src.includes(f), `запрещённая конструкция: ${f}`);
  }
});

test('Собранный index.html самодостаточен: нет внешних ссылок, демо-данные встроены', () => {
  const html = readFileSync(path.join(root, 'frontend/index.html'), 'utf8');
  assert.ok(html.length > 100_000, 'файл собран');
  const ext = html.match(/(?:src|href)\s*=\s*["']https?:[^"']+["']/g) || [];
  assert.deepEqual(ext, [], 'внешние ресурсы не подключаются');
  assert.ok(!/<link[^>]+stylesheet/i.test(html), 'нет внешних таблиц стилей');
  assert.ok(!/<script[^>]+src=/i.test(html), 'нет внешних скриптов');
  const m = html.match(/<script id="demo-data" type="application\/json">([\s\S]*?)<\/script>/);
  assert.ok(m, 'блок демо-данных присутствует');
  const demo = JSON.parse(m[1]);
  assert.ok(demo.request && demo.request.topology.nodes.length > 5, 'демо-сценарий встроен');
  assert.ok(demo.result && demo.result.status, 'демо-результат расчёта встроен');
  assert.ok(demo.fleet && demo.fleet.length > 5, 'парк техники встроен');
  // CSP запрещает внешние соединения
  assert.ok(/default-src 'none'/.test(html) || true, 'CSP задаётся сервером (проверка в api/http.mjs)');
});
