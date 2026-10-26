/**
 * core/schema.mjs — контракт данных ENG6-PLAN (вход/выход) и лимиты.
 *
 * Формальная спецификация — docs/01_MATH_MODEL.md (ЭТАП 1). Этот файл — её
 * исполняемая часть: любое расхождение между документом и кодом считается
 * дефектом документа и правится в документе (single source of truth = код).
 *
 * Принцип «Айтишника»: ядро не знает ни про HTTP, ни про БД. Все лимиты
 * задаются здесь и проверяются в core/validate.mjs до входа в математику —
 * некорректный граф не может «повесить» ядро или привести к OOM.
 */

/** Жёсткие лимиты экземпляра задачи (защита от OOM/DoS на уровне ядра). */
export const LIMITS = Object.freeze({
  maxNodes: 200_000,
  maxLinks: 800_000,
  maxDemands: 20_000,
  maxFleetUnits: 50_000,
  maxThreats: 5_000,
  maxTimeLimitMs: 600_000,
  /** выше этого числа рёбер режим 'full' (ENG6.calcLink) автоматически не применяется */
  fullPhysicsMaxLinks: 20_000,
  /** ограничение на число терминалов в точном алгоритме Штейнера */
  steinerExactMaxTerminals: 64,
  /** ограничение на размер графа для точного max-flow (резервирование) */
  flowMaxArcs: 400_000,
  maxStringLength: 256,
});

/** Типы узлов ENG6 (ключи фасада NODE_TYPES). */
export const NODE_TYPES = Object.freeze(['cp', 'relay', 'radio', 'sat', 'observer', 'ew_jam', 'ew_rlr', 'ew_rer']);
/** Типы линий ENG6. */
export const LINK_TYPES = Object.freeze(['VHF', 'UHF', 'HF', 'RRL', 'SAT', 'FOC']);
/** Модуляции ENG6 (MODS). */
export const MODS = Object.freeze(['BPSK', 'QPSK', 'GMSK', 'PI4DQPSK', '8PSK', '16QAM', '64QAM', '256QAM']);
/** Кодовые скорости (подмножество FECS, используемое планировщиком). */
export const FECS = Object.freeze(['none', '1/2', '2/3', '3/4', 'conv1/2', 'conv2/3', 'conv3/4', 'conv7/8', 'turbo1/3', 'turbo1/2', 'turbo3/4', 'ldpc1/4', 'ldpc1/2', 'ldpc3/4']);
/** Типы подстилающей поверхности (ENG6.CLUTTER). */
export const CLUTTER = Object.freeze(['open', 'forest', 'urban', 'dense_urban']);
/** Состояние техники (FR-RES-03 Дополнения к ТЗ). */
export const CONDITIONS = Object.freeze(['green', 'yellow', 'red']);
/** Режимы физики. */
export const PHYSICS_MODES = Object.freeze(['fast', 'full']);
/** Качество расчёта ENG6 (для режима 'full'). */
export const QUALITY = Object.freeze(['fast', 'balanced', 'accurate']);
/** Статусы результата. */
export const PLAN_STATUS = Object.freeze(['OPTIMAL', 'FEASIBLE', 'PARTIAL', 'INFEASIBLE', 'INVALID', 'ERROR', 'TIMEOUT']);

/** Значения по умолчанию для блока constraints. */
export const DEFAULT_CONSTRAINTS = Object.freeze({
  budget: Infinity,
  minMarginDb: 3,
  maxDelayMs: 250,
  maxHops: 8,
  maxSimultaneousDeploy: 4,
  reserveFactor: 1.3,
  requireRedundancy: false,
  allowYellowUnits: true,
  allowRedUnits: false,
  physics: 'auto',
  /**
   * Качество расчёта ENG6 для режима physics='full'.
   * По умолчанию 'balanced', а НЕ 'fast': в ENG6 ветвь quality='fast' считает
   * УКВ-пролёт чистым Hata (≈147 дБ на 10 км / 150 МГц), что пессимистичнее
   * balanced (log-distance, ≈108 дБ) на 30…40 дБ и не согласуется с режимом
   * physics='fast' планировщика, который зеркалит именно balanced (ADR-003,
   * DEF-02). 'fast' допустим только для интерактивного предпросмотра.
   */
  quality: 'balanced',
  fastMarginPenaltyDb: 6,
  miscLossDb: 2,
  timeLimitMs: 30_000,
  hopProcessingMs: 2,
  frameKbit: 1,
  utilPenaltyMs: 10,
  maxUtilizationPct: 85,
  crewSpeedKmh: 40,
  seed: 20260116,
});

/** Веса целевой функции (сумма не обязана быть 1 — нормируется). */
export const DEFAULT_WEIGHTS = Object.freeze({ cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 });

/**
 * Целевая функция планировщика (см. docs/01_MATH_MODEL.md, п. 4):
 *   F = w_c·Ĉ + w_d·D̂ + w_s·(1−Ŝ) + w_m·(1−M̂) → min,
 * где Ĉ, D̂, M̂ — нормированные к [0,1] стоимость, задержка и запас,
 *      Ŝ — нормированная живучесть.
 */
export function objective({ costNorm, delayNorm, survivabilityNorm, marginNorm, weights }) {
  const w = normalizeWeights(weights);
  return w.cost * costNorm + w.delay * delayNorm + w.survivability * (1 - survivabilityNorm) + w.margin * (1 - marginNorm);
}

export function normalizeWeights(weights = {}) {
  const w = { ...DEFAULT_WEIGHTS, ...weights };
  const s = Math.max(1e-9, (w.cost || 0) + (w.delay || 0) + (w.survivability || 0) + (w.margin || 0));
  return { cost: (w.cost || 0) / s, delay: (w.delay || 0) / s, survivability: (w.survivability || 0) / s, margin: (w.margin || 0) / s };
}

/** Модель стоимости активации линии по типу (у.е.), если cost не задан явно. */
export const DEFAULT_LINK_COST = Object.freeze({
  VHF: 0, // радионаправление: стоимость = 0 (оборудование уже в парке)
  UHF: 0,
  HF: 0,
  SAT: 12, // абонентский терминал + ресурс канала
  RRL: 45, // мачта + стволовое оборудование на интервал
  FOC: 90, // прокладка кабеля, у.е./км см. costPerKm
});
export const DEFAULT_LINK_COST_PER_KM = Object.freeze({ VHF: 0, UHF: 0, HF: 0, SAT: 0, RRL: 8, FOC: 22 });

/** Стоимость развертывания единицы техники по типу узла, у.е. */
export const DEFAULT_NODE_COST = Object.freeze({ cp: 30, relay: 18, radio: 6, sat: 60, observer: 4, ew_jam: 25, ew_rlr: 25, ew_rer: 20 });
/** Время развертывания, мин. */
export const DEFAULT_NODE_DEPLOY_MIN = Object.freeze({ cp: 45, relay: 25, radio: 10, sat: 60, observer: 15, ew_jam: 30, ew_rlr: 30, ew_rer: 20 });
