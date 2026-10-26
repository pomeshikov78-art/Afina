/**
 * core/knapsack.mjs — целочисленное распределение ограниченного бюджета.
 *
 * Задача (см. docs/01_MATH_MODEL.md, п. 5.4): из множества «улучшений»
 * (резервные линии, дополнительные ретрансляторы, замена аппаратуры) выбрать
 * подмножество с максимальной суммарной полезностью при ограничении
 *   Σ cost_i·x_i ≤ BudgetRemaining,  x_i ∈ {0,1}.
 * Это классический 0/1-рюкзак, NP-трудный; решается:
 *   • точно — динамическим программированием по масштабированной стоимости,
 *     если capacity·items ≤ dpCellLimit (по умолчанию 4·10^6 ячеек ≈ 32 МБ);
 *   • приближённо — жадностью по плотности (value/cost) с последующим
 *     локальным улучшением (swap 1↔1, 1↔2), если DP не влезает в лимит памяти.
 *     Гарантированная граница:жадность даёт ≥ 1/2 OPT для 0/1-рюкзака
 *     (при доборе по плотности и сравнении с «тяжёлым» предметом).
 *
 * Детерминированность: при равных плотностях порядок задаётся индексом.
 */
import { argsort } from './util.mjs';

/**
 * @param {{value:number,cost:number,id?:any}[]} items
 * @param {number} capacity
 * @param {{dpCellLimit?:number, scale?:number}} [opts]
 * @returns {{chosen:number[], totalValue:number, totalCost:number, exact:boolean, cellsUsed:number}}
 */
export function knapsack01(items, capacity, opts = {}) {
  const dpCellLimit = opts.dpCellLimit ?? 4_000_000;
  const n = items.length;
  if (n === 0 || capacity <= 0) return { chosen: [], totalValue: 0, totalCost: 0, exact: true, cellsUsed: 0 };

  const usable = items.map((it, i) => ({ i, value: Math.max(0, it.value || 0), cost: Math.max(0, it.cost || 0) })).filter((it) => it.cost <= capacity && it.value > 0);
  if (usable.length === 0) return { chosen: [], totalValue: 0, totalCost: 0, exact: true, cellsUsed: 0 };

  // масштабирование стоимостей до целых
  const scale = opts.scale ?? chooseScale(usable, capacity, dpCellLimit);
  const maxCost = Math.max(1, Math.floor(capacity / scale));
  const cells = usable.length * (maxCost + 1);

  if (cells <= dpCellLimit) {
    return dpExact(usable, items, maxCost, scale, cells);
  }
  return greedyImprove(usable, items, capacity);
}

function chooseScale(usable, capacity, dpCellLimit) {
  const target = (usable.length * capacity) / dpCellLimit;
  const maxCost0 = capacity;
  return Math.max(1e-9, Math.max(target, maxCost0 / 100000));
}

function dpExact(usable, items, maxCost, scale, cells) {
  const n = usable.length;
  const W = maxCost + 1;
  const dp = new Float64Array(n * W); // максимальная ценность
  const take = new Uint8Array(n * W);
  for (let i = 0; i < n; i++) {
    const cost = Math.max(1, Math.floor(usable[i].cost / scale));
    const value = usable[i].value;
    const prev = i === 0 ? null : dp.subarray((i - 1) * W, i * W);
    const cur = dp.subarray(i * W, (i + 1) * W);
    const takeRow = take.subarray(i * W, (i + 1) * W);
    for (let c = 0; c < W; c++) {
      const without = prev ? prev[c] : 0;
      let best = without;
      let t = 0;
      if (c >= cost) {
        const withItem = (prev ? prev[c - cost] : 0) + value;
        if (withItem > best + 1e-12) {
          best = withItem;
          t = 1;
        }
      }
      cur[c] = best;
      takeRow[c] = t;
    }
  }
  // восстановление
  const chosen = [];
  let c = maxCost;
  for (let i = n - 1; i >= 0; i--) {
    if (take[i * W + c]) {
      chosen.push(usable[i].i);
      c -= Math.max(1, Math.floor(usable[i].cost / scale));
    }
  }
  chosen.reverse();
  let totalValue = 0;
  let totalCost = 0;
  for (const idx of chosen) {
    totalValue += items[idx].value || 0;
    totalCost += items[idx].cost || 0;
  }
  return { chosen, totalValue, totalCost, exact: true, cellsUsed: cells };
}

function greedyImprove(usable, items, capacity) {
  const order = argsort(usable.map((u) => u.cost > 0 ? u.value / u.cost : Infinity), (a, b) => b - a);
  const chosenSet = new Set();
  let spent = 0;
  for (const k of order) {
    const u = usable[k];
    if (spent + u.cost <= capacity + 1e-9) {
      chosenSet.add(u.i);
      spent += u.cost;
    }
  }
  // «тяжёлый» предмет: сравнить с вариантом «взять один самый ценный»
  let best = { set: new Set(chosenSet), value: sumValue(chosenSet, items), cost: spent };
  const byValue = argsort(usable.map((u) => u.value), (a, b) => b - a);
  for (const k of byValue.slice(0, 4)) {
    const u = usable[k];
    if (u.cost > capacity) continue;
    const set = new Set([u.i]);
    let s = u.cost;
    for (const kk of order) {
      const v = usable[kk];
      if (set.has(v.i)) continue;
      if (s + v.cost <= capacity + 1e-9) {
        set.add(v.i);
        s += v.cost;
      }
    }
    const val = sumValue(set, items);
    if (val > best.value + 1e-9) best = { set, value: val, cost: s };
  }
  // локальное улучшение: swap одного выбранного на два невыбранных
  let improved = true;
  let guard = 0;
  while (improved && guard++ < 20) {
    improved = false;
    const inArr = [...best.set];
    const outArr = items.map((_, i) => i).filter((i) => !best.set.has(i) && (items[i].cost || 0) <= capacity);
    for (const a of inArr) {
      const ca = items[a].cost || 0;
      const va = items[a].value || 0;
      const room = capacity - (best.cost - ca);
      for (let p = 0; p < outArr.length; p++) {
        for (let q = p + 1; q < outArr.length; q++) {
          const b = outArr[p];
          const c = outArr[q];
          if (b === a || c === a) continue;
          const cb = items[b].cost || 0;
          const cc = items[c].cost || 0;
          if (cb + cc > room + 1e-9) continue;
          const gain = (items[b].value || 0) + (items[c].value || 0) - va;
          if (gain > 1e-9) {
            best.set.delete(a);
            best.set.add(b);
            best.set.add(c);
            best.value += gain;
            best.cost += cb + cc - ca;
            improved = true;
            break;
          }
        }
        if (improved) break;
      }
      if (improved) break;
    }
  }
  return {
    chosen: [...best.set].sort((a, b) => a - b),
    totalValue: best.value,
    totalCost: best.cost,
    exact: false,
    cellsUsed: 0,
  };
}

function sumValue(set, items) {
  let s = 0;
  for (const i of set) s += items[i].value || 0;
  return s;
}

/**
 * Приоритетное «отсечение по бюджету»: когда обязательный каркас дороже
 * бюджета, нужно честно отдать максимум ценности в порядке приоритета.
 * Возвращает индексы принятых заявок и суммарный дефицит.
 *
 * @param {{value:number,cost:number,priority:number}[]} items
 * @param {number} budget
 */
export function prioritizeWithinBudget(items, budget) {
  const order = argsort(items, (a, b) => b.priority - a.priority || b.value - a.value || a.cost - b.cost);
  const accepted = [];
  const rejected = [];
  let spent = 0;
  for (const i of order) {
    const c = items[i].cost || 0;
    if (spent + c <= budget + 1e-9) {
      accepted.push(i);
      spent += c;
    } else rejected.push(i);
  }
  accepted.sort((a, b) => a - b);
  rejected.sort((a, b) => a - b);
  return { accepted, rejected, spent };
}
