/**
 * core/util.mjs — мелкие чистые утилиты ядра.
 * Ничего, кроме математики и форматирования: ни I/O, ни часов реального времени
 * (время передаётся извне — см. nowMs в planner.mjs, чтобы результат был
 * воспроизводимым в тестах).
 */

/** Округление до 6 знаков — NFR-03 Дополнения к ТЗ (точность ~10 см). */
export function round6(x) {
  return Number.isFinite(x) ? Math.round(x * 1e6) / 1e6 : x;
}

export function round(x, digits = 3) {
  if (!Number.isFinite(x)) return x;
  const p = 10 ** digits;
  return Math.round(x * p) / p;
}

/**
 * Детерминированный ГПСЧ (mulberry32). Используется только для тай-брейков и
 * генерации тестовых данных — в маршрутизации случайность не применяется,
 * чтобы один и тот же вход всегда давал один и тот же план (NFR-DET-01).
 */
export function mulberry32(seed) {
  let t = (seed >>> 0) || 1;
  return function () {
    t = (t + 0x6d2b79f5) >>> 0;
    let x = Math.imul(t ^ (t >>> 15), 1 | t);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}

/** Устойчивое целочисленное хеширование строки (FNV-1a) — для компактных id. */
export function fnv1a(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** Сортировка индексов по ключу с детерминированным тай-брейком. */
export function argsort(keys, cmp = (a, b) => a - b) {
  const idx = new Array(keys.length);
  for (let i = 0; i < idx.length; i++) idx[i] = i;
  idx.sort((a, b) => cmp(keys[a], keys[b]) || a - b);
  return idx;
}

/** Форматирование числа для отчётов: фиксированная точность без «-0». */
export function fmt(x, digits = 2) {
  if (!Number.isFinite(x)) return '—';
  const v = Number(x.toFixed(digits));
  return Object.is(v, -0) ? (0).toFixed(digits) : String(v);
}

/** Кламп (та же семантика, что ENG6.clamp). */
export function clamp(x, lo, hi) {
  return x < lo ? lo : x > hi ? hi : x;
}

/** Монотонные часы в мс (единственный источник времени в ядре). */
export function nowMs() {
  return Number(process.hrtime.bigint() / 1000n) / 1000;
}
