/**
 * tests/unit/validation.test.mjs — валидатор входа и статус INVALID.
 *
 * Задача (ЭТАП 3): некорректный граф не должен ни «повесить» ядро, ни привести
 * к OOM, ни молча дать бессмысленный результат. Проверяются все классы ошибок:
 * тип, диапазон, перечисление, дубликат, ссылка, лимит, кросс-проверка.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { validateRequest, canonicalHash, canonicalStringify, V } from '../../core/validate.mjs';
import { plan } from '../../core/planner.mjs';
import { LIMITS } from '../../core/schema.mjs';

const node = (id, extra = {}) => ({ id, type: 'cp', lat: 55.75, lng: 37.62, ...extra });
const base = (nodes, links = [], extra = {}) => ({ topology: { nodes, links }, ...extra });

test('Корректный минимальный запрос проходит валидацию', () => {
  const rep = validateRequest(base([node('a'), node('b')], [{ id: 'l1', from: 'a', to: 'b', type: 'VHF' }]));
  assert.ok(rep.ok, JSON.stringify(rep.errors));
  assert.equal(rep.errors.length, 0);
});

test('Отсутствие topology → REQUIRED, расчёт не запускается', () => {
  const rep = validateRequest({});
  assert.ok(!rep.ok);
  assert.ok(rep.errors.some((e) => e.code === V.REQUIRED && e.path === '$.topology'));
  const r = plan({});
  assert.equal(r.status, 'INVALID');
});

test('Некорректные координаты и типы узлов → RANGE/ENUM/TYPE', () => {
  const cases = [
    [{ id: 'a', type: 'cp', lat: 91, lng: 0 }, V.RANGE, '$.topology.nodes[0].lat'],
    [{ id: 'a', type: 'cp', lat: 0, lng: -181 }, V.RANGE, '$.topology.nodes[0].lng'],
    [{ id: 'a', type: 'hq', lat: 0, lng: 0 }, V.ENUM, '$.topology.nodes[0].type'],
    [{ id: '', type: 'cp', lat: 0, lng: 0 }, V.REQUIRED, '$.topology.nodes[0].id'],
    [{ id: 'a', type: 'cp', lat: 'abc', lng: 0 }, V.TYPE, '$.topology.nodes[0].lat'],
    [{ id: 'a', type: 'cp', lat: 0, lng: 0, txPowerDbm: 200 }, V.RANGE, '$.topology.nodes[0].txPowerDbm'],
    [{ id: 'a', type: 'cp', lat: 0, lng: 0, antHeightM: -5 }, V.RANGE, '$.topology.nodes[0].antHeightM'],
    [{ id: 'a', type: 'cp', lat: 0, lng: 0, rxSensDbm: 10 }, V.RANGE, '$.topology.nodes[0].rxSensDbm'],
  ];
  for (const [nd, code, path] of cases) {
    const rep = validateRequest(base([nd]));
    assert.ok(rep.errors.some((e) => e.code === code && e.path === path), `${path}: ожидался ${code}, получено ${JSON.stringify(rep.errors)}`);
  }
});

test('NaN и Infinity в координатах отклоняются (защита от «тихого» мусора)', () => {
  const rep = validateRequest(base([{ id: 'a', type: 'cp', lat: NaN, lng: 0 }]));
  assert.ok(rep.errors.some((e) => e.path === '$.topology.nodes[0].lat'));
  const r = plan(base([{ id: 'a', type: 'cp', lat: Number.POSITIVE_INFINITY, lng: 0 }]));
  assert.equal(r.status, 'INVALID');
});

test('Дубликаты id узлов/рёбер/заявок → DUPLICATE', () => {
  const rep = validateRequest(base([node('a'), node('a')], [{ id: 'l', from: 'a', to: 'a', type: 'VHF' }]));
  assert.ok(rep.errors.some((e) => e.code === V.DUPLICATE && e.path === '$.topology.nodes[1].id'));
  const rep2 = validateRequest({
    topology: { nodes: [node('a'), node('b')], links: [{ id: 'l', from: 'a', to: 'b', type: 'VHF' }, { id: 'l', from: 'a', to: 'b', type: 'UHF' }] },
    demands: [{ id: 'q', from: 'a', to: 'b', rateKbps: 8 }, { id: 'q', from: 'a', to: 'b', rateKbps: 16 }],
  });
  assert.ok(rep2.errors.some((e) => e.code === V.DUPLICATE && e.path.includes('links[1]')));
  assert.ok(rep2.errors.some((e) => e.code === V.DUPLICATE && e.path.includes('demands[1]')));
});

test('Битые ссылки (несуществующие узлы, parentId, from==to) → REF/CROSS', () => {
  const rep = validateRequest(
    base([node('a'), node('b', { parentId: 'zzz' })], [
      { id: 'l1', from: 'a', to: 'ghost', type: 'VHF' },
      { id: 'l2', from: 'b', to: 'b', type: 'VHF' },
    ], { demands: [{ id: 'q', from: 'a', to: 'nowhere', rateKbps: 8 }] }),
  );
  assert.ok(rep.errors.some((e) => e.code === V.REF && e.path === '$.topology.nodes[1].parentId'));
  assert.ok(rep.errors.some((e) => e.code === V.REF && e.path === '$.topology.links[0].to'));
  assert.ok(rep.errors.some((e) => e.code === V.CROSS && e.path === '$.topology.links[1]'));
  assert.ok(rep.errors.some((e) => e.code === V.REF && e.path === '$.demands[0].to'));
  const r = plan(base([node('a')], [{ id: 'l1', from: 'a', to: 'ghost', type: 'VHF' }]));
  assert.equal(r.status, 'INVALID');
});

test('Недопустимые перечисления (тип линии, модуляция, FEC, clutter, condition) → ENUM', () => {
  const rep = validateRequest({
    topology: { nodes: [node('a'), node('b')], links: [{ id: 'l', from: 'a', to: 'b', type: 'LASER', mod: 'QAM9', fec: '5/6' }] },
    environment: { clutterType: 'jungle' },
    fleet: [{ unitId: 'u1', condition: 'broken', nodeType: 'cp' }],
  });
  assert.ok(rep.errors.some((e) => e.code === V.ENUM && e.path === '$.topology.links[0].type'));
  assert.ok(rep.errors.some((e) => e.code === V.ENUM && e.path === '$.topology.links[0].mod'));
  assert.ok(rep.errors.some((e) => e.code === V.ENUM && e.path === '$.topology.links[0].fec'));
  assert.ok(rep.errors.some((e) => e.code === V.ENUM && e.path === '$.environment.clutterType'));
  assert.ok(rep.errors.some((e) => e.code === V.ENUM && e.path === '$.fleet[0].condition'));
});

test('Лимиты экземпляра: превышение числа узлов/рёбер/заявок → LIMIT, память не выделяется', () => {
  const rep = validateRequest({ topology: { nodes: new Array(LIMITS.maxNodes + 1).fill(0).map((_, i) => node(`n${i}`)), links: [] } });
  assert.ok(rep.errors.some((e) => e.code === V.LIMIT && e.path === '$.topology.nodes'));
  const rep2 = validateRequest({ topology: { nodes: [node('a')], links: [] }, demands: new Array(LIMITS.maxDemands + 1).fill({ id: 'q', from: 'a', to: 'a', rateKbps: 1 }) });
  assert.ok(rep2.errors.some((e) => e.code === V.LIMIT && e.path === '$.demands'));
  // plan() на таком входе возвращает INVALID мгновенно (без построения таблиц)
  const t0 = Date.now();
  const r = plan({ topology: { nodes: new Array(LIMITS.maxNodes + 1).fill(0).map((_, i) => node(`n${i}`)), links: [] } });
  assert.equal(r.status, 'INVALID');
  assert.ok(Date.now() - t0 < 5000, 'отказ должен быть быстрым');
});

test('Ограничение числа возвращаемых ошибок (защита от раздувания ответа)', () => {
  const nodes = new Array(500).fill(0).map((_, i) => ({ id: `n${i}`, type: 'unknown', lat: 1e6, lng: 1e6 }));
  const rep = validateRequest({ topology: { nodes, links: [] } });
  assert.ok(rep.errors.length <= 101, `ошибок ${rep.errors.length}`);
  assert.ok(rep.truncated, 'флаг усечения установлен');
  assert.equal(rep.errors[rep.errors.length - 1].code, 'TRUNCATED');
});

test('Вырожденные веса и ограничения → CROSS/RANGE', () => {
  assert.ok(validateRequest({ ...base([node('a')]), weights: { cost: 0, delay: 0, survivability: 0, margin: 0 } }).errors.some((e) => e.path === '$.weights'));
  assert.ok(validateRequest({ ...base([node('a')]), weights: { cost: -1 } }).errors.some((e) => e.code === V.RANGE));
  assert.ok(validateRequest({ ...base([node('a')]), constraints: { budget: -5 } }).errors.some((e) => e.path === '$.constraints.budget'));
  assert.ok(validateRequest({ ...base([node('a')]), constraints: { maxHops: 0 } }).errors.some((e) => e.path === '$.constraints.maxHops'));
  assert.ok(validateRequest({ ...base([node('a')]), constraints: { timeLimitMs: 1 } }).errors.some((e) => e.path === '$.constraints.timeLimitMs'));
  assert.ok(validateRequest({ ...base([node('a')]), constraints: { physics: 'quantum' } }).errors.some((e) => e.code === V.ENUM));
  assert.ok(validateRequest({ ...base([node('a')]), constraints: { requireRedundancy: 'yes' } }).errors.some((e) => e.code === V.TYPE));
});

test('Заявки: нулевая/отрицательная полоса, неизвестные узлы, priority вне диапазона', () => {
  const rep = validateRequest({
    topology: { nodes: [node('a'), node('b')] },
    demands: [
      { id: 'q1', from: 'a', to: 'b', rateKbps: 0 },
      { id: 'q2', from: 'a', to: 'b', rateKbps: -5 },
      { id: 'q3', from: 'a', to: 'b', rateKbps: 8, priority: 9 },
      { id: 'q4', from: 'a', to: 'a', rateKbps: 8 },
      { id: 'q5', from: 'a', to: 'b', rateKbps: 8, maxDelayMs: 0 },
    ],
  });
  for (const i of [0, 1, 2, 3, 4]) assert.ok(rep.errors.some((e) => e.path.startsWith(`$.demands[${i}]`)), `demands[${i}] не проверена`);
});

test('Предупреждения (не ошибки): DEM-рельеф, неизвестные поля, избыточная точность', () => {
  const rep = validateRequest({
    topology: { nodes: [{ id: 'a', type: 'cp', lat: 55.123456789, lng: 37.62 }], links: [] },
    environment: { terrain: { source: 'online' } },
    meta: { name: 'x', unknownField: 1 },
    weights: { cost: 1, weird: 2 },
  });
  assert.ok(rep.ok, 'предупреждения не делают запрос некорректным');
  assert.ok(rep.warnings.some((w) => w.code === 'CROSS' && w.path === '$.environment.terrain.source'));
  assert.ok(rep.warnings.some((w) => w.code === 'UNKNOWN_FIELD'));
  assert.ok(rep.warnings.some((w) => String(w.message).includes('точность')));
});

test('Строковые значения числовых полей приводятся к числу (мягкость к CSV-импорту)', () => {
  const rep = validateRequest({ topology: { nodes: [{ id: 'a', type: 'cp', lat: '55,75'.replace(',', '.'), lng: '37.62', txPowerDbm: '43' }], links: [] } });
  assert.ok(rep.ok, JSON.stringify(rep.errors));
});

test('Канонический хэш: порядок полей и «хвосты» нулей не влияют, содержание влияет', () => {
  const a = { topology: { nodes: [{ id: 'a', lat: 55.75, lng: 37.62 }], links: [] }, constraints: { budget: 100 } };
  const b = { constraints: { budget: 100 }, topology: { nodes: [{ lng: 37.62, id: 'a', lat: 55.75 }], links: [] } };
  assert.equal(canonicalHash(a), canonicalHash(b), 'перестановка ключей не меняет хэш');
  assert.equal(canonicalHash({ ...a, constraints: { budget: 100.0 } }), canonicalHash(a), '100 и 100.0 эквивалентны');
  assert.notEqual(canonicalHash({ ...a, constraints: { budget: 101 } }), canonicalHash(a), 'изменение значения меняет хэш');
  assert.match(canonicalHash(a), /^[0-9a-f]{64}$/);
  // каноническая форма устойчива к вложенности и массивам
  assert.equal(canonicalStringify({ b: 1, a: [3, { y: 2, x: 1 }] }), '{"a":[3,{"x":1,"y":2}],"b":1}');
});

test('Вторая линия обороны: штатные валидаторы ENG6 не получают мусор', () => {
  // запрос проходит наш валидатор → собранный сценарий проходит ENG6.validateScenario
  const r = plan(base([node('a'), node('b', { type: 'relay' })], [{ id: 'l1', from: 'a', to: 'b', type: 'VHF' }], { constraints: { physics: 'full', eng6Analysis: false }, environment: { terrain: { source: 'flat' } } }));
  assert.notEqual(r.status, 'INVALID');
  assert.notEqual(r.status, 'ERROR');
  const eng6Issues = (r.diagnostics.validation?.errors || []).filter((e) => e.code === 'ENG6');
  assert.equal(eng6Issues.length, 0, `валидатор ENG6 не принял сценарий: ${JSON.stringify(eng6Issues)}`);
});

test('Пустой граф и граф без рёбер: INFEASIBLE, а не исключение', () => {
  const r1 = plan(base([node('a'), node('b')]));
  assert.equal(r1.status, 'INFEASIBLE');
  assert.ok(r1.statusReasons.some((x) => x.code === 'NO_LINKS'));
  const r2 = plan(base([node('a')]));
  assert.equal(r2.status, 'INFEASIBLE');
});
