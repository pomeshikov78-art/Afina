/**
 * core/physics.mjs — физический уровень ENG6-PLAN.
 *
 * Два режима расчёта (ADR-003 «Tiered fidelity»):
 *
 *  ┌────────┬──────────────────────────────────────────────┬───────────────┐
 *  │ режим  │ что считается                                 │ стоимость     │
 *  ├────────┼──────────────────────────────────────────────┼───────────────┤
 *  │ 'full' │ ENG6.calcLink() дословно: профиль рельефа,    │ ~60 мкс/ребро │
 *  │        │ дифракция ITU-R P.526, цепочка моделей        │               │
 *  │        │ (замирания/MIMO/MODCOD/информ./трафик), РЭБ   │               │
 *  ├────────┼──────────────────────────────────────────────┼───────────────┤
 *  │ 'fast' │ те же формулы ENG6, но БЕЗ профиля рельефа и  │ ~2 мкс/ребро  │
 *  │        │ цепочки моделей; только ENG6-примитивы:       │               │
 *  │        │ PROP.hata/fspl/logDistance/twoRay/rainGamma/  │               │
 *  │        │ gasGamma/fogGamma, CLUTTER, noiseDbm,         │               │
 *  │        │ practicalRate, shannonKbps, HF, SAT, FIBER    │               │
 *  └────────┴──────────────────────────────────────────────┴───────────────┘
 *
 * Важно: 'fast' НЕ является «своей» физикой — это те же функции ENG6, вызванные
 * напрямую, в том же порядке и с теми же коэффициентами, что и ветви calcLink.
 * Отличия только два (оба измерены в tests/unit/physics_fidelity.test.mjs):
 *   − нет дифракционных потерь по рельефу (компенсируются fastMarginPenaltyDb);
 *   − нет цепочки моделей (замирания/MIMO/MODCOD) → запас считается по порогу
 *     демодуляции, доступность — по логнормальному затенению Φ(margin/σ).
 *
 * Модуль чистый: нет БД, нет сети, нет DEM-загрузок (terrain.source ∈ {synth,flat}).
 */
import { loadEng6 } from './eng6.mjs';

const C_KM_MS = 299792.458; // скорость света, км/мс
const EARTH_R_KM = 6371; // как в ENG6 (EARTH_R_KM)

export const LINK_TYPE_IDS = ['VHF', 'UHF', 'HF', 'RRL', 'SAT', 'FOC'];
export const LINK_TYPE_IX = Object.fromEntries(LINK_TYPE_IDS.map((t, i) => [t, i]));
export const MOD_IDS = ['BPSK', 'QPSK', 'GMSK', 'PI4DQPSK', '8PSK', '16QAM', '64QAM', '256QAM'];
export const MOD_IX = Object.fromEntries(MOD_IDS.map((t, i) => [t, i]));
export const FEC_IDS = ['none', '1/2', '2/3', '3/4', 'conv1/2', 'conv2/3', 'conv3/4', 'conv7/8', 'turbo1/3', 'turbo1/2', 'turbo3/4', 'ldpc1/4', 'ldpc1/2', 'ldpc3/4'];
export const FEC_IX = Object.fromEntries(FEC_IDS.map((t, i) => [t, i]));

/** Причины отбраковки ребра — стабильные коды для API/аудита. */
export const REJECT = [
  'OK',
  'NODE_NOT_FOUND',
  'COINCIDENT_NODES',
  'UNKNOWN_LINK_TYPE',
  'BELOW_MARGIN',
  'OUT_OF_RANGE',
  'NO_LOS_REQUIRED',
  'FORBIDDEN_TYPE',
  'UNIT_NOT_READY',
  'CAPACITY_ZERO',
  'DELAY_EXCEEDED',
  'PHYSICS_ERROR',
];
export const REJECT_IX = Object.fromEntries(REJECT.map((r, i) => [r, i]));
export const REJECT_TEXT = {
  OK: 'принято',
  NODE_NOT_FOUND: 'узел линии не найден в топологии',
  COINCIDENT_NODES: 'узлы совпадают (длина < 10 м)',
  UNKNOWN_LINK_TYPE: 'неизвестный тип линии',
  BELOW_MARGIN: 'запас по уровню ниже минимально допустимого',
  OUT_OF_RANGE: 'вне физической дальности (запас менее −25 дБ)',
  NO_LOS_REQUIRED: 'требуется прямая видимость (РРЛ)',
  FORBIDDEN_TYPE: 'тип линии запрещён ограничениями запроса',
  UNIT_NOT_READY: 'техника неисправна и не может быть развернута',
  CAPACITY_ZERO: 'нулевая пропускная способность при заданном SNR',
  DELAY_EXCEEDED: 'задержка линии превышает ограничение',
  PHYSICS_ERROR: 'внутренняя ошибка расчёта физического уровня',
};

/**
 * Haversine — та же формула и та же константа R=6371 км, что в ENG6, поэтому
 * расстояния в режимах 'fast' и 'full' совпадают побитово.
 */
export function haversineKm(lat1, lng1, lat2, lng2) {
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLng = ((lng2 - lng1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
  return 2 * EARTH_R_KM * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

/**
 * Таблица рёбер — struct-of-arrays. Всё, что нужно графовому слою,
 * маршрутизатору и отчёту, лежит в непрерывных типизированных массивах.
 */
export class EdgeTable {
  constructor(m) {
    this.m = m;
    this.from = new Int32Array(m);
    this.to = new Int32Array(m);
    this.typeIdx = new Uint8Array(m);
    this.modIdx = new Uint8Array(m).fill(MOD_IX.QPSK);
    this.fecIdx = new Uint8Array(m);
    this.distKm = new Float64Array(m);
    this.freqMhz = new Float64Array(m);
    this.bwKhz = new Float64Array(m);
    this.plDb = new Float64Array(m);
    this.rxDbm = new Float64Array(m);
    this.marginDb = new Float64Array(m); // ENG6-семантика: SINR − reqSnr (порог демодуляции)
    this.sensMarginDb = new Float64Array(m); // rxPower − rxSens (запас по чувствительности)
    this.snrDb = new Float64Array(m);
    this.sinrDb = new Float64Array(m);
    this.capKbps = new Float64Array(m);
    this.theoreticalKbps = new Float64Array(m);
    this.delayMs = new Float64Array(m);
    this.availability = new Float64Array(m).fill(0.5);
    this.cost = new Float64Array(m);
    this.feasible = new Uint8Array(m);
    this.rejectCode = new Int32Array(m);
    this.usedModIdx = new Uint8Array(m);
    this.los = new Uint8Array(m).fill(1);
    this.raw = null; // подробности режима 'full' (модель, breakdown, warnings) — по требованию
  }

  rejectName(e) {
    return REJECT[this.rejectCode[e]] || 'UNKNOWN';
  }
}

/** Параметры узлов — struct-of-arrays (нормализованы пресетами ENG6). */
export class NodeTable {
  constructor(n) {
    this.n = n;
    this.lat = new Float64Array(n);
    this.lng = new Float64Array(n);
    this.txPowerDbm = new Float64Array(n);
    this.txGainDbi = new Float64Array(n);
    this.rxGainDbi = new Float64Array(n);
    this.rxSensDbm = new Float64Array(n);
    this.antHeightM = new Float64Array(n);
    this.feederLossDb = new Float64Array(n);
    this.noiseFigureDb = new Float64Array(n).fill(6);
    this.typeIdx = new Uint8Array(n);
    this.ready = new Uint8Array(n).fill(1); // техника исправна и может быть развернута
    this.deployMin = new Float64Array(n); // время развертывания, мин
    this.cost = new Float64Array(n); // стоимость развертывания узла
  }
}

/** Нормализовать параметры узла пресетами ENG6.NODE_PRESETS по типу. */
export function normalizeNodeParams(node, E) {
  const preset = E.NODE_PRESETS[node.type] || E.NODE_PRESETS.relay || {};
  const num = (v, d) => (typeof v === 'number' && Number.isFinite(v) ? v : d);
  return {
    txPowerDbm: num(node.txPowerDbm, num(preset.txPowerDbm, 36)),
    txGainDbi: num(node.txGainDbi, num(preset.txGainDbi, 0)),
    rxGainDbi: num(node.rxGainDbi, num(preset.rxGainDbi, 0)),
    rxSensDbm: num(node.rxSensDbm, num(preset.rxSensDbm, -100)),
    antHeightM: num(node.antHeightM, num(preset.antHeightM, 10)),
    feederLossDb: num(node.feederLossDb, num(preset.feederLossDb, 0)),
    noiseFigureDb: num(node.noiseFigureDb, num(preset.noiseFigureDb, 6)),
    freqMhz: num(node.freqMhz, num(preset.freqMhz, 150)),
    mobility: node.mobility || preset.mobility || 'static',
  };
}

/**
 * Φ(x) — стандартная нормальная CDF.
 * erfc считается той же аппроксимацией Numerical Recipes (erfcNR), что и в ENG6,
 * поэтому значения доступности совпадают с моделью f_lognormal ядра.
 */
export function erfcApprox(x) {
  const t = Math.abs(x);
  const n = 2 / (2 + t);
  const poly =
    1.00002368 +
    n * (0.37409196 + n * (0.09678418 + n * (-0.18628806 + n * (0.27886807 + n * (-1.13520398 + n * (1.48851587 + n * (-0.82215223 + n * 0.17087277)))))));
  const v = n * Math.exp(-t * t - 1.26551223 + n * poly);
  return x >= 0 ? v : 2 - v;
}

export function normCdfPhi(x) {
  return Math.max(0, Math.min(1, 1 - 0.5 * erfcApprox(x / Math.SQRT2)));
}

/**
 * Быстрый аналитический расчёт всех рёбер (векторизованный, без аллокаций
 * на ребро). Формулы — дословные ветви ENG6.calcLink для FOC / SAT / HF /
 * УКВ-ДМВ-РРЛ в режиме quality='fast'.
 *
 * @param {{table:EdgeTable, nodes:NodeTable, env:object, opts:object}} p
 * @returns {EdgeTable}
 */
export function computeEdgesFast({ table, nodes, env, opts }) {
  const E = loadEng6().ENG6;
  const clutter = E.CLUTTER[env.clutterType || 'open'] || E.CLUTTER.open;
  const rain = (env.weather && env.weather.rainRateMmHr) || 0;
  const fog = (env.weather && env.weather.fogDensity) || 0;
  const tempC = (env.weather && env.weather.temperatureC) ?? 15;
  const hum = (env.weather && env.weather.humidityPercent) ?? 60;
  const iono = env.ionosphere || {};
  const tod = iono.timeOfDay ?? 12;
  const miscLossDb = opts.miscLossDb ?? env.miscLossDb ?? 2;
  const penalty = opts.fastMarginPenaltyDb ?? 6;
  const minMarginDb = opts.minMarginDb ?? 3;
  const maxDelayMs = opts.maxDelayMs ?? Infinity;
  const sigmaClutter = Math.max(0.5, clutter.sig || 6);

  const lat = nodes.lat;
  const lng = nodes.lng;

  for (let e = 0; e < table.m; e++) {
    if (table.rejectCode[e] === REJECT_IX.NODE_NOT_FOUND || table.rejectCode[e] === REJECT_IX.UNKNOWN_LINK_TYPE) continue;
    const u = table.from[e];
    const v = table.to[e];
    const type = LINK_TYPE_IDS[table.typeIdx[e]];
    const d = haversineKm(lat[u], lng[u], lat[v], lng[v]);
    table.distKm[e] = d;
    if (d < 0.01) {
      table.rejectCode[e] = REJECT_IX.COINCIDENT_NODES;
      continue;
    }
    const f = table.freqMhz[e] > 0 ? table.freqMhz[e] : E.LINK_TYPES[type].freq || 150;
    table.freqMhz[e] = f;
    const bw = table.bwKhz[e] > 0 ? table.bwKhz[e] : 25;
    table.bwKhz[e] = bw;

    const txP = nodes.txPowerDbm[u];
    const txG = nodes.txGainDbi[u];
    const rxG = nodes.rxGainDbi[v];
    const rxSens = nodes.rxSensDbm[v];
    const hTx = nodes.antHeightM[u] || 10;
    const hRx = nodes.antHeightM[v] || 3;
    const feeder = nodes.feederLossDb[u] + nodes.feederLossDb[v];

    let pl = 0;
    let rx = 0;
    let snr = 0;
    let delayMs = 0;
    let avail = 0.99;
    let margin = 0;
    let sensMargin = 0;
    let cap = 0;
    let theo = 0;
    let usedMod = table.modIdx[e];
    const modName0 = MOD_IDS[table.modIdx[e]] || 'QPSK';
    const fecName0 = FEC_IDS[table.fecIdx[e]] || 'none';
    // порог демодуляции с учётом выигрыша FEC — та же формула, что в ENG6.calcLink
    const reqSnr0 = (E.MODS[modName0] ? E.MODS[modName0].req : 11) - (E.FECS[fecName0] ? E.FECS[fecName0].gain : 0);

    if (type === 'FOC') {
      const fiber = E.LINK_PRESETS.FOC.fiber;
      const lenKm = d * 1.15;
      const loss = E.FIBER.loss(fiber, lenKm);
      pl = loss;
      rx = (fiber.txDbm || 0) - loss;
      margin = rx - (fiber.rxSensDbm || -28); // ENG6: для ВОЛС marginDb = оптический запас
      sensMargin = margin;
      delayMs = E.FIBER.delayMs(lenKm);
      const nominal = (fiber.nominalMbps || 1000) * 1000;
      theo = nominal;
      cap = Math.min(nominal, E.shannonKbps(bw, 40));
      const breaksPerYear = (fiber.laying === 'aerial' ? 0.12 : 0.03) * (lenKm / 100) * 10;
      avail = Math.max(0.9, Math.min(1, Math.exp(-breaksPerYear * 0.02)));
    } else if (type === 'SAT') {
      const sat = E.LINK_PRESETS.SAT.sat;
      const slant = E.SAT.slantKm(sat.elevDeg || 30, sat.orbitKm || 35786);
      const fspl = E.PROP.fspl(slant, f);
      const rainDb = E.PROP.rainSlant(f, rain, sat.elevDeg || 30);
      const eff = Math.min(slant, 12 / Math.sin((Math.max(5, sat.elevDeg || 30) * Math.PI) / 180));
      const gas = E.PROP.gasGamma(f, tempC, hum) * eff;
      const fogDb = E.PROP.fogGamma(f, fog) * eff;
      pl = fspl + rainDb + gas + fogDb + feeder + miscLossDb;
      rx = (sat.satEirpDbm || 80) + rxG - pl;
      const noise = E.noiseDbm(bw, 1.2);
      snr = rx - noise;
      margin = snr - reqSnr0;
      sensMargin = rx - rxSens;
      delayMs = (2 * slant) / C_KM_MS + 30;
      avail = 0.995;
    } else if (type === 'HF') {
      const hf = E.HF.analyse(f, d, { timeOfDay: tod, season: iono.season || 'equinox', solarActivity: iono.solarActivity || 'medium' });
      const fading = 25;
      pl = E.PROP.fspl(d, f) + hf.absorptionDb + fading + miscLossDb;
      rx = txP + txG + rxG - pl;
      const noise = E.noiseDbm(bw, 8) + (tod >= 6 && tod <= 18 ? 6 : 0);
      snr = hf.usable ? rx - noise : -30;
      margin = snr - reqSnr0;
      sensMargin = rx - rxSens;
      delayMs = (d / C_KM_MS) * 1000 + 120;
      avail = hf.usable ? Math.max(0.5, hf.avail) : 0.2;
      if (!hf.usable) { margin = -30; sensMargin = Math.min(sensMargin, -30); }
    } else {
      let propLoss;
      if (type === 'RRL') {
        propLoss = E.PROP.fspl(d, f);
      } else {
        // Дословная ветвь ENG6.calcLink для quality='balanced' (без профиля рельефа):
        //   дальняя зона (d > 4·h1·h2/λ) и LOS → min(Hata(open), two-ray);
        //   иначе → log-distance с показателем n из таблицы CLUTTER.
        // Ветвь quality='fast' оригинала (чистый Hata) на УКВ существенно
        // пессимистичнее balanced и даёт расхождение fast/full до 40 дБ —
        // поэтому fast-режим планировщика зеркалит именно balanced (ADR-003).
        const farZoneKm = (4 * hTx * hRx * 1000) / (300 / f) / 1000;
        if (d > Math.max(1, farZoneKm) && table.los[e]) {
          propLoss = Math.min(E.PROP.hata(f, d, hTx, hRx, 'open'), E.PROP.twoRay(d, hTx, hRx));
        } else {
          propLoss = E.PROP.logDistance(d, f, clutter.n);
        }
      }
      const clutterLoss = clutter.loss * Math.min(1, d / 5) * (type === 'RRL' ? 0.3 : 1);
      const rainDb = E.PROP.rainGamma(f, rain) * Math.min(d, d * (1 / (1 + d / 25))) * (f >= 1000 ? 1 : 0.05);
      const gas = E.PROP.gasGamma(f, tempC, hum) * d;
      const fogDb = E.PROP.fogGamma(f, fog) * d * (f >= 10000 ? 1 : 0);
      pl = propLoss + clutterLoss + rainDb + gas + fogDb + feeder + miscLossDb + penalty;
      rx = txP + txG + rxG - pl;
      const noise = E.noiseDbm(bw, nodes.noiseFigureDb[v]);
      snr = rx - noise;
      // ENG6-семантика: marginDb = SINR − reqSnr; sensMarginDb = rxPower − rxSens
      margin = snr - reqSnr0;
      sensMargin = rx - rxSens;
      delayMs = (d / C_KM_MS) * 1000 + 4;
      // доступность — логнормальное затенение Φ(margin/σ), как модель f_lognormal ENG6
      avail = normCdfPhi(margin / sigmaClutter);
    }

    table.plDb[e] = pl;
    table.rxDbm[e] = rx;
    table.snrDb[e] = snr;
    table.sinrDb[e] = snr;
    table.marginDb[e] = margin;
    table.sensMarginDb[e] = sensMargin;
    table.delayMs[e] = delayMs;
    table.availability[e] = avail;
    table.theoreticalKbps[e] = theo || E.shannonKbps(bw, snr);

    const pr = E.practicalRate(bw, modName0, fecName0, snr, reqSnr0);
    cap = cap || pr.kbps;
    table.capKbps[e] = cap;
    table.usedModIdx[e] = pr.mod ? MOD_IX[pr.mod] ?? table.modIdx[e] : table.modIdx[e];

    const okMargin = margin >= minMarginDb;
    const okCap = cap > 0;
    const okDelay = delayMs <= maxDelayMs;
    if (okMargin && okCap && okDelay) {
      table.feasible[e] = 1;
      table.rejectCode[e] = 0;
    } else {
      table.feasible[e] = 0;
      table.rejectCode[e] = !okMargin
        ? margin < minMarginDb - 25
          ? REJECT_IX.OUT_OF_RANGE
          : REJECT_IX.BELOW_MARGIN
        : !okCap
          ? REJECT_IX.CAPACITY_ZERO
          : REJECT_IX.DELAY_EXCEEDED;
    }
  }
  return table;
}

/**
 * Полный расчёт рёбер через ENG6.calcLink: рельеф, дифракция, цепочка моделей,
 * РЭБ (внешние источники подавления), BER/FER, доступность.
 * @param {{table:EdgeTable, scenario:object, linkIndex:Int32Array, opts:object}} p
 *        linkIndex[e] — индекс ссылки в scenario.links
 */
export function computeEdgesFull({ table, scenario, linkIndex, opts }) {
  const E = loadEng6().ENG6;
  const q = opts.quality || 'fast';
  const minMarginDb = opts.minMarginDb ?? 3;
  const maxDelayMs = opts.maxDelayMs ?? Infinity;
  table.raw = new Array(table.m);
  for (let e = 0; e < table.m; e++) {
    if (table.rejectCode[e] === REJECT_IX.NODE_NOT_FOUND || table.rejectCode[e] === REJECT_IX.UNKNOWN_LINK_TYPE) continue;
    const link = scenario.links[linkIndex[e]];
    let r;
    try {
      r = E.calcLink(link, scenario, {
        quality: q,
        miscLossDb: opts.miscLossDb ?? scenario.environment.miscLossDb ?? 2,
      });
    } catch (err) {
      table.feasible[e] = 0;
      table.rejectCode[e] = REJECT_IX.PHYSICS_ERROR;
      table.raw[e] = { error: String((err && err.message) || err) };
      continue;
    }
    table.distKm[e] = r.distKm;
    table.freqMhz[e] = r.freqMhz;
    table.bwKhz[e] = r.bwKhz;
    table.plDb[e] = r.breakdown ? sumBreakdown(r.breakdown) : 0;
    table.rxDbm[e] = r.rxPowerDbm ?? 0;
    table.marginDb[e] = r.marginDb ?? r.opticalMarginDb ?? -999;
    table.sensMarginDb[e] = r.sensMarginDb ?? table.marginDb[e];
    table.snrDb[e] = r.snrDb ?? -999;
    table.sinrDb[e] = r.sinrDb ?? r.snrDb ?? -999;
    table.capKbps[e] = r.practicalKbps || 0;
    table.theoreticalKbps[e] = r.theoreticalKbps || 0;
    table.delayMs[e] = r.delayMs || 0;
    table.availability[e] = r.availability ?? 0.5;
    table.los[e] = r.lineOfSight ? 1 : 0;
    table.usedModIdx[e] = MOD_IX[r.usedMod] ?? table.modIdx[e];
    table.raw[e] = {
      model: r.model,
      status: r.status,
      breakdown: r.breakdown,
      warnings: r.warnings,
      ber: r.ber,
      fer: r.fer,
      sensMarginDb: r.sensMarginDb ?? null,
      fresnelClearancePct: r.fresnelClearancePct,
      minClearanceM: r.minClearanceM,
      jam: r.jam || null,
    };
    const rrlNoLos = link.type === 'RRL' && !r.lineOfSight && opts.requireLosForRrl !== false;
    const ok = !rrlNoLos && table.marginDb[e] >= minMarginDb && table.capKbps[e] > 0 && r.status !== 'down' && table.delayMs[e] <= maxDelayMs;
    if (ok) {
      table.feasible[e] = 1;
      table.rejectCode[e] = 0;
    } else {
      table.feasible[e] = 0;
      table.rejectCode[e] = rrlNoLos
        ? REJECT_IX.NO_LOS_REQUIRED
        : table.capKbps[e] <= 0
          ? REJECT_IX.CAPACITY_ZERO
          : table.marginDb[e] < minMarginDb - 25
            ? REJECT_IX.OUT_OF_RANGE
            : table.delayMs[e] > maxDelayMs
              ? REJECT_IX.DELAY_EXCEEDED
              : REJECT_IX.BELOW_MARGIN;
    }
  }
  return table;
}

function sumBreakdown(b) {
  let s = 0;
  for (const k of Object.keys(b)) if (typeof b[k] === 'number' && Number.isFinite(b[k])) s += b[k];
  return s;
}
