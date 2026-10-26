/**
 * core/schedule.mjs — расписание развертывания (выходной артефакт №3 ТЗ).
 *
 * Модель (docs/01_MATH_MODEL.md, п. 5.6):
 *   Задано множество разворачиваемых объектов J = {j}, каждый с:
 *     • t_j    — время развертывания, мин (из парка техники или пресета узла);
 *     • prec_j — множество предшественников (иерархия ОШС: parentId; узел связи
 *                не может быть развернут раньше своего КП/опорного узла);
 *     • march_j — время марша к позиции = dist/crewSpeedKmh·60, мин;
 *     • crew_j — требуемая бригада (ресурс).
 *   Ограничение: не более C бригад работают одновременно (maxSimultaneousDeploy).
 *   Целевая функция: минимизация makespan при соблюдении precedence —
 *   задача R|prec|Cmax, NP-трудная; применяется приоритетное списковое
 *   расписание (list scheduling, Graham 1966) с приоритетом
 *     (уровень в DAG ↓, время развертывания ↓, критичность узла ↓).
 *   Гарантия: makespan ≤ (2 − 1/C)·OPT при отсутствии precedence;
 *   с precedence — ≤ (2 − 1/C)·OPT по длине критического пути.
 *
 * Алгоритм полностью детерминирован (стабильная сортировка, без ГПСЧ).
 */
import { round } from './util.mjs';

/**
 * @param {object} p
 * @param {{id:string,nodeId:string,deployMin:number,marchMin:number,precede:string[],level:number,criticality:number,crew?:number,unitId?:string,kind?:string}[]} p.jobs
 * @param {number} p.crews число одновременных бригад
 * @param {number} [p.t0] момент начала, мин
 * @returns {{waves:Array, makespanMin:number, jobs:Array, violations:string[], crewUtilPct:number}}
 */
export function buildSchedule({ jobs, crews = 4, t0 = 0 }) {
  const C = Math.max(1, Math.floor(crews));
  const byId = new Map(jobs.map((j) => [j.id, j]));
  const violations = [];

  // 1) проверка и обрезка циклов в precedence (защита от битых данных ОШС)
  const level = computeLevels(jobs, byId, violations);

  // 2) приоритет: уровень ↑ (сначала вышестоящие), затем длительность ↓, затем критичность ↓
  const order = jobs
    .map((j, i) => ({ i, j, lvl: level.get(j.id) || 0 }))
    .sort((a, b) => a.lvl - b.lvl || b.j.deployMin - a.j.deployMin || (b.j.criticality || 0) - (a.j.criticality || 0) || a.i - b.i);

  // 3) списковое расписание: событие-ориентированное, бригады — массив freeAt[]
  const freeAt = new Float64Array(C).fill(t0);
  const done = new Map(); // id → finish time
  const results = [];
  for (const { j, lvl } of order) {
    let earliest = t0;
    for (const p of j.precede || []) {
      const pf = done.get(p);
      if (pf === undefined) {
        if (byId.has(p)) violations.push(`Предшественник ${p} задачи ${j.id} не запланирован (цикл или отсутствующий узел) — ограничение проигнорировано`);
        else violations.push(`Предшественник ${p} задачи ${j.id} отсутствует в плане — ограничение проигнорировано`);
        continue;
      }
      if (pf > earliest) earliest = pf;
    }
    earliest += j.marchMin || 0;
    // выбираем бригаду, освобождающуюся раньше всех (стабильно: минимальный индекс)
    let best = 0;
    for (let c = 1; c < C; c++) if (freeAt[c] < freeAt[best]) best = c;
    const start = Math.max(earliest, freeAt[best]);
    const finish = start + (j.deployMin || 0);
    freeAt[best] = finish;
    done.set(j.id, finish);
    results.push({
      id: j.id,
      nodeId: j.nodeId,
      unitId: j.unitId || null,
      kind: j.kind || null,
      crew: best,
      level: lvl,
      startMin: round(start - t0, 2),
      endMin: round(finish - t0, 2),
      deployMin: round(j.deployMin || 0, 2),
      marchMin: round(j.marchMin || 0, 2),
      absStartMin: round(start, 2),
      absEndMin: round(finish, 2),
    });
  }

  // 4) волны: группировка по перекрывающимся интервалам и по «уровню готовности»
  const waves = makeWaves(results);
  const makespanMin = results.length ? Math.max(...results.map((r) => r.endMin)) : 0;
  const busyMin = results.reduce((s, r) => s + (r.endMin - r.startMin), 0);
  const crewUtilPct = makespanMin > 0 ? Math.min(100, (busyMin / (makespanMin * C)) * 100) : 0;

  return { waves, makespanMin: round(makespanMin, 2), jobs: results, violations, crewUtilPct: round(crewUtilPct, 1), crews: C };
}

/** Уровни в DAG предшествования (итеративно, с защитой от циклов). */
function computeLevels(jobs, byId, violations) {
  const level = new Map();
  const state = new Map(); // 0 = in progress, 1 = done
  const reported = new Set();
  for (const j of jobs) {
    const stack = [[j.id, false]];
    while (stack.length) {
      const [id, processed] = stack.pop();
      if (level.has(id)) continue;
      const job = byId.get(id);
      if (!job) {
        level.set(id, 0);
        continue;
      }
      if (processed) {
        let l = 0;
        for (const p of job.precede || []) if (level.has(p) && level.get(p) + 1 > l) l = level.get(p) + 1;
        level.set(id, l);
        state.set(id, 1);
        continue;
      }
      if (state.get(id) === 0) {
        if (!reported.has(id)) {
          reported.add(id);
          violations.push(`Цикл предшествования в ОШС на «${id}» — уровень принят равным 0`);
        }
        level.set(id, 0);
        state.set(id, 1);
        continue;
      }
      state.set(id, 0);
      stack.push([id, true]);
      for (const p of job.precede || []) if (!level.has(p) && byId.has(p)) stack.push([p, false]);
    }
  }
  return level;
}

/**
 * Волны развертывания: непрерывные интервалы, в которых заняты ≥1 бригад.
 * Интервалы разбиваются в точках, где все бригады свободны (окно готовности).
 */
function makeWaves(results) {
  if (!results.length) return [];
  const sorted = [...results].sort((a, b) => a.startMin - b.startMin || a.endMin - b.endMin);
  const waves = [];
  let cur = { index: 1, tStartMin: sorted[0].startMin, tEndMin: sorted[0].endMin, items: [sorted[0]], crews: new Set([sorted[0].crew]) };
  for (let i = 1; i < sorted.length; i++) {
    const r = sorted[i];
    if (r.startMin >= cur.tEndMin - 1e-9) {
      waves.push(seal(cur));
      cur = { index: waves.length + 1, tStartMin: r.startMin, tEndMin: r.endMin, items: [r], crews: new Set([r.crew]) };
    } else {
      cur.items.push(r);
      cur.crews.add(r.crew);
      cur.tEndMin = Math.max(cur.tEndMin, r.endMin);
    }
  }
  waves.push(seal(cur));
  return waves;
}

function seal(w) {
  return {
    index: w.index,
    tStartMin: round(w.tStartMin, 2),
    tEndMin: round(w.tEndMin, 2),
    durationMin: round(w.tEndMin - w.tStartMin, 2),
    crewsUsed: w.crews.size,
    items: w.items.map((i) => ({ id: i.id, nodeId: i.nodeId, unitId: i.unitId, kind: i.kind, crew: i.crew, startMin: i.startMin, endMin: i.endMin })),
  };
}

/**
 * Критический путь расписания (цепочка задач, определяющая makespan).
 * Используется в отчёте: «что нужно ускорить в первую очередь».
 */
export function criticalPath(schedule) {
  if (!schedule.jobs.length) return [];
  // индекс по времени завершения: без него поиск предшественника даёт O(J^2)
  const byEnd = new Map();
  for (const j of schedule.jobs) {
    const k = j.endMin.toFixed(6);
    if (!byEnd.has(k)) byEnd.set(k, []);
    byEnd.get(k).push(j);
  }
  const last = schedule.jobs.reduce((a, b) => (a.endMin >= b.endMin ? a : b));
  const path = [last];
  let cur = last;
  const visited = new Set([cur.id]);
  let guard = 0;
  while (guard++ < schedule.jobs.length) {
    const need = (cur.startMin - (cur.marchMin || 0)).toFixed(6);
    const cands = byEnd.get(need) || [];
    const pred = cands.find((j) => j.id !== cur.id && !visited.has(j.id));
    if (!pred) break;
    visited.add(pred.id);
    path.unshift(pred);
    cur = pred;
  }
  return path.map((j) => ({ id: j.id, nodeId: j.nodeId, startMin: j.startMin, endMin: j.endMin }));
}
