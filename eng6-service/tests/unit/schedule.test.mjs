/**
 * tests/unit/schedule.test.mjs — расписание развертывания (выходной артефакт №3).
 * Проверяются: precedence по ОШС, лимит одновременных бригад, корректность
 * makespan, волны, защита от циклов в иерархии, детерминированность.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildSchedule, criticalPath } from '../../core/schedule.mjs';

const job = (id, deployMin, precede = [], extra = {}) => ({ id, nodeId: id, deployMin, marchMin: 0, precede, criticality: 0, level: 0, ...extra });

test('Без precedence и с C бригадами makespan = LPT-оценка ( ceil-раскладка )', () => {
  const jobs = [job('a', 30), job('b', 20), job('c', 10), job('d', 40)];
  const s = buildSchedule({ jobs, crews: 2, t0: 0 });
  assert.equal(s.crews, 2);
  assert.equal(s.jobs.length, 4);
  // нижняя оценка: max(сумма/C, максимум одной задачи) = max(100/2, 40) = 50
  assert.ok(s.makespanMin >= 50 - 1e-9, `makespan ${s.makespanMin} ≥ 50`);
  assert.equal(s.makespanMin, 50, 'LPT даёт оптимальную раскладку для этого набора');
  // ни одна бригада не занята двумя задачами одновременно
  const byCrew = new Map();
  for (const j of s.jobs) {
    const arr = byCrew.get(j.crew) || [];
    arr.push(j);
    byCrew.set(j.crew, arr);
  }
  for (const [, arr] of byCrew) {
    arr.sort((x, y) => x.startMin - y.startMin);
    for (let i = 1; i < arr.length; i++) assert.ok(arr[i].startMin >= arr[i - 1].endMin - 1e-9, 'пересечение задач одной бригады');
  }
});

test('Precedence: подчинённый не начинается раньше завершения вышестоящего', () => {
  const jobs = [job('cp', 45), job('relay', 25, ['cp']), job('radio', 10, ['relay']), job('obs', 15, ['radio'])];
  const s = buildSchedule({ jobs, crews: 4, t0: 0 });
  const end = new Map(s.jobs.map((j) => [j.id, j.endMin]));
  const start = new Map(s.jobs.map((j) => [j.id, j.startMin]));
  assert.equal(start.get('relay'), end.get('cp'));
  assert.equal(start.get('radio'), end.get('relay'));
  assert.equal(start.get('obs'), end.get('radio'));
  assert.equal(s.makespanMin, 45 + 25 + 10 + 15);
  assert.equal(s.waves.length, 4, 'каждый уровень иерархии — отдельная волна');
  assert.deepEqual(s.waves.map((w) => w.items[0].nodeId), ['cp', 'relay', 'radio', 'obs']);
});

test('Уровни в DAG: параллельные ветки стартуют одновременно', () => {
  const jobs = [job('cp', 20), job('a', 10, ['cp']), job('b', 10, ['cp']), job('c', 10, ['cp'])];
  const s = buildSchedule({ jobs, crews: 3, t0: 0 });
  const st = new Map(s.jobs.map((j) => [j.id, j.startMin]));
  assert.equal(st.get('a'), 20);
  assert.equal(st.get('b'), 20);
  assert.equal(st.get('c'), 20);
  assert.equal(s.makespanMin, 30);
  assert.equal(s.waves.length, 2);
});

test('Лимит бригад: при C=1 задачи идут строго последовательно', () => {
  const jobs = [job('a', 5), job('b', 7), job('c', 3)];
  const s = buildSchedule({ jobs, crews: 1, t0: 0 });
  assert.equal(s.makespanMin, 15);
  const sorted = [...s.jobs].sort((x, y) => x.startMin - y.startMin);
  for (let i = 1; i < sorted.length; i++) assert.ok(sorted[i].startMin >= sorted[i - 1].endMin - 1e-9);
  assert.ok(Math.abs(s.crewUtilPct - 100) < 1e-6, `загрузка бригады ${s.crewUtilPct}%`);
});

test('Время марша учитывается и отодвигает начало работ', () => {
  const s = buildSchedule({ jobs: [job('a', 10, [], { marchMin: 25 })], crews: 2, t0: 0 });
  assert.equal(s.jobs[0].startMin, 25);
  assert.equal(s.jobs[0].endMin, 35);
  assert.equal(s.jobs[0].marchMin, 25);
});

test('Цикл в иерархии не зацикливает расчёт и фиксируется в violations', () => {
  const jobs = [job('a', 10, ['b']), job('b', 10, ['a']), job('c', 5, ['a'])];
  const s = buildSchedule({ jobs, crews: 2, t0: 0 });
  assert.equal(s.jobs.length, 3, 'все задачи запланированы');
  assert.ok(s.violations.length > 0, 'нарушение зафиксировано');
  assert.ok(s.violations.some((v) => /цикл/i.test(v)), `текст нарушения: ${s.violations.join('; ')}`);
  assert.ok(Number.isFinite(s.makespanMin));
});

test('Отсутствующий предшественник игнорируется с предупреждением', () => {
  const s = buildSchedule({ jobs: [job('a', 10, ['ghost'])], crews: 1, t0: 0 });
  assert.equal(s.jobs.length, 1);
  assert.ok(s.violations.some((v) => v.includes('ghost')));
});

test('Пустой набор задач: пустое расписание без исключений', () => {
  const s = buildSchedule({ jobs: [], crews: 3, t0: 0 });
  assert.equal(s.makespanMin, 0);
  assert.deepEqual(s.waves, []);
  assert.deepEqual(criticalPath(s), []);
  assert.equal(s.crewUtilPct, 0);
});

test('Момент начала t0 сдвигает абсолютное время, но не относительное', () => {
  const s = buildSchedule({ jobs: [job('a', 10), job('b', 10)], crews: 1, t0: 120 });
  assert.equal(s.jobs[0].absStartMin, 120);
  assert.equal(s.jobs[0].startMin, 0);
  assert.equal(s.makespanMin, 20);
});

test('Критический путь содержит задачи, определяющие makespan', () => {
  const jobs = [job('cp', 45), job('r', 25, ['cp']), job('x', 5, ['cp'])];
  const s = buildSchedule({ jobs, crews: 2, t0: 0 });
  const cp = criticalPath(s);
  assert.ok(cp.length >= 2, `длина критического пути ${cp.length}`);
  assert.equal(cp[cp.length - 1].endMin, s.makespanMin);
  assert.deepEqual(cp.map((c) => c.id), ['cp', 'r']);
});

test('Детерминированность: одинаковый вход → идентичное расписание', () => {
  const jobs = [job('a', 12), job('b', 8, ['a']), job('c', 9), job('d', 4, ['c']), job('e', 15)];
  const s1 = buildSchedule({ jobs, crews: 3, t0: 0 });
  const s2 = buildSchedule({ jobs, crews: 3, t0: 0 });
  assert.equal(JSON.stringify(s1), JSON.stringify(s2));
});

test('Волны не пересекаются по времени и покрывают все задачи', () => {
  const jobs = Array.from({ length: 12 }, (_, i) => job(`j${i}`, 5 + (i % 4) * 3, i % 3 === 0 ? [] : [`j${i % 3}`]));
  const s = buildSchedule({ jobs, crews: 3, t0: 0 });
  const all = s.waves.flatMap((w) => w.items.map((i) => i.id));
  assert.equal(new Set(all).size, jobs.length, 'все задачи попали в волны');
  for (let i = 1; i < s.waves.length; i++) assert.ok(s.waves[i].tStartMin >= s.waves[i - 1].tEndMin - 1e-9, 'волны упорядочены по времени');
  for (const w of s.waves) {
    assert.ok(w.items.length > 0);
    assert.ok(w.crewsUsed <= 3);
    assert.ok(w.durationMin >= 0);
  }
});
