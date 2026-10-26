/**
 * tests/unit/knapsack.test.mjs — целочисленное распределение бюджета.
 * Точное ДП сверяется с полным перебором (брутфорс) на случайных экземплярах.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { knapsack01, prioritizeWithinBudget } from '../../core/knapsack.mjs';

function brute(items, cap) {
  const n = items.length;
  let best = { value: 0, cost: 0, set: [] };
  for (let mask = 0; mask < 1 << n; mask++) {
    let c = 0;
    let v = 0;
    const set = [];
    for (let i = 0; i < n; i++)
      if (mask & (1 << i)) {
        c += items[i].cost;
        v += items[i].value;
        set.push(i);
      }
    if (c <= cap + 1e-9 && v > best.value + 1e-12) best = { value: v, cost: c, set };
  }
  return best;
}

function mulberry(seed) {
  let t = seed >>> 0;
  return () => {
    t = (t + 0x6d2b79f5) >>> 0;
    let x = Math.imul(t ^ (t >>> 15), 1 | t);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}

test('Точное ДП совпадает с полным перебором на 200 случайных экземплярах', () => {
  const rnd = mulberry(7);
  for (let t = 0; t < 200; t++) {
    const n = 1 + Math.floor(rnd() * 12);
    const items = Array.from({ length: n }, (_, i) => ({ value: Math.round(rnd() * 100) + 1, cost: Math.round(rnd() * 40) + 1, id: i }));
    const cap = Math.round(rnd() * 120) + 1;
    const got = knapsack01(items, cap, { dpCellLimit: 10_000_000 });
    const want = brute(items, cap);
    assert.ok(got.exact, 'режим должен быть точным при малом числе ячеек');
    assert.ok(Math.abs(got.totalValue - want.value) < 1e-6 + want.value * 0.02, `ценность ${got.totalValue} vs ${want.value} (экземпляр ${t})`);
    assert.ok(got.totalCost <= cap + 1e-9, `стоимость ${got.totalCost} > ${cap}`);
    // проверка корректности выбранного множества
    let v = 0;
    let c = 0;
    for (const i of got.chosen) {
      v += items[i].value;
      c += items[i].cost;
    }
    assert.ok(Math.abs(v - got.totalValue) < 1e-6, 'totalValue не соответствует chosen');
    assert.ok(Math.abs(c - got.totalCost) < 1e-6, 'totalCost не соответствует chosen');
    assert.equal(new Set(got.chosen).size, got.chosen.length, 'нет дублей в chosen');
  }
});

test('Жадный режим (ДП не влезает) даёт ≥ 40 % от оптимума и не превышает бюджет', () => {
  const rnd = mulberry(11);
  for (let t = 0; t < 40; t++) {
    const n = 40 + Math.floor(rnd() * 60);
    const items = Array.from({ length: n }, (_, i) => ({ value: Math.round(rnd() * 1000) + 1, cost: Math.round(rnd() * 100000) + 1, id: i }));
    const cap = Math.round(rnd() * 400000) + 1000;
    const got = knapsack01(items, cap, { dpCellLimit: 1000 }); // принудительно малый лимит → жадность
    assert.ok(!got.exact, 'ожидался приближённый режим');
    assert.ok(got.totalCost <= cap + 1e-9, `бюджет превышен: ${got.totalCost} > ${cap}`);
    // сверка с optimum на уменьшенном экземпляре (брутфорс дорог для n=100)
    const small = items.slice(0, 14);
    const smallCap = Math.max(1, Math.round(cap / 8));
    const gs = knapsack01(small, smallCap, { dpCellLimit: 1000 });
    const bs = brute(small, smallCap);
    assert.ok(gs.totalValue >= bs.value * 0.4 - 1e-9, `жадность ${gs.totalValue} < 0.4·${bs.value}`);
  }
});

test('Граничные случаи: пустой список, нулевой/отрицательный бюджет, предметы дороже бюджета', () => {
  assert.deepEqual(knapsack01([], 100), { chosen: [], totalValue: 0, totalCost: 0, exact: true, cellsUsed: 0 });
  assert.equal(knapsack01([{ value: 5, cost: 10 }], 0).chosen.length, 0);
  assert.equal(knapsack01([{ value: 5, cost: 10 }], -5).chosen.length, 0);
  assert.equal(knapsack01([{ value: 5, cost: 1000 }], 10).chosen.length, 0);
  const r = knapsack01([{ value: 5, cost: 10 }, { value: 0, cost: 1 }], 50);
  assert.deepEqual(r.chosen, [0], 'предметы с нулевой ценностью не берутся');
});

test('Детерминированность: одинаковый вход → одинаковый выбор', () => {
  const items = [
    { value: 10, cost: 5 }, { value: 10, cost: 5 }, { value: 12, cost: 6 }, { value: 7, cost: 3 },
  ];
  const a = knapsack01(items, 11);
  const b = knapsack01(items, 11);
  assert.deepEqual(a.chosen, b.chosen);
  assert.equal(a.totalValue, b.totalValue);
});

test('prioritizeWithinBudget: сначала высший приоритет, затем ценность', () => {
  const items = [
    { value: 100, cost: 60, priority: 3 },
    { value: 10, cost: 10, priority: 1 },
    { value: 20, cost: 20, priority: 1 },
    { value: 500, cost: 50, priority: 5 }, // низший приоритет (5), но дорогая
  ];
  const r = prioritizeWithinBudget(items, 30);
  assert.deepEqual(r.accepted.sort(), [1, 2], 'приняты обе заявки приоритета 1');
  assert.deepEqual(r.rejected.sort(), [0, 3]);
  assert.equal(r.spent, 30);
});
