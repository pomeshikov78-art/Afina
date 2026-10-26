/**
 * core/scenario.mjs — сборка сценария ENG6 из запроса ENG6-PLAN.
 *
 * Зачем: ENG6.calcLink / analyzeScenario принимают сценарий формата
 * {meta, environment, nodes, links, threats}. Запрос планировщика — надмножество
 * (парк техники, заявки, ограничения). Модуль выполняет две вещи:
 *   1) нормализация (заполнение дефолтов пресетами ENG6);
 *   2) устранение дефекта поверхности ENG6 v5.1: buildTemplate() возвращает
 *      сценарий БЕЗ поля environment, и analyzeScenario падает с
 *      «Cannot read properties of undefined (reading 'terrain')».
 *      Обнаружено при реверс-инжиниринге изделия comms_planner_v8.0_headless.html
 *      (см. docs/06_DEFECTS.md, DEF-01). Здесь environment формируется всегда.
 *
 * Модуль чистый: никаких внешних источников данных, terrain.source ∈ {synth, flat}.
 */
import { loadEng6 } from './eng6.mjs';
import { normalizeNodeParams } from './physics.mjs';
import { round6 } from './util.mjs';

export const DEFAULT_ENVIRONMENT = Object.freeze({
  terrain: { source: 'synth', amplitudeM: 60 },
  clutterType: 'open',
  weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 },
  ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' },
  miscLossDb: 2,
});

/**
 * Нормализовать environment запроса: недостающие поля — из DEFAULT_ENVIRONMENT,
 * DEM/online-источники рельефа принудительно заменяются на synth (изоляция ядра).
 */
export function normalizeEnvironment(env, report = null) {
  const out = {
    terrain: { ...(DEFAULT_ENVIRONMENT.terrain || {}) },
    clutterType: DEFAULT_ENVIRONMENT.clutterType,
    weather: { ...DEFAULT_ENVIRONMENT.weather },
    ionosphere: { ...DEFAULT_ENVIRONMENT.ionosphere },
    miscLossDb: DEFAULT_ENVIRONMENT.miscLossDb,
  };
  if (!env || typeof env !== 'object') return out;
  if (env.clutterType) out.clutterType = env.clutterType;
  if (typeof env.miscLossDb === 'number' && Number.isFinite(env.miscLossDb)) out.miscLossDb = env.miscLossDb;
  if (env.weather && typeof env.weather === 'object') Object.assign(out.weather, pickNumbers(env.weather));
  if (env.ionosphere && typeof env.ionosphere === 'object') Object.assign(out.ionosphere, env.ionosphere);
  if (env.terrain && typeof env.terrain === 'object') {
    if (typeof env.terrain.amplitudeM === 'number') out.terrain.amplitudeM = env.terrain.amplitudeM;
    const src = env.terrain.source;
    if (src === 'flat' || src === 'synth') out.terrain.source = src;
    else if (src) {
      out.terrain.source = 'synth';
      if (report) report.warn('CROSS', '$.environment.terrain.source', `Источник рельефа «${src}» недоступен в headless-режиме — использован synth`);
    }
  }
  return out;
}

function pickNumbers(o) {
  const out = {};
  for (const k of Object.keys(o)) if (typeof o[k] === 'number' && Number.isFinite(o[k])) out[k] = o[k];
  return out;
}

/**
 * Собрать сценарий ENG6 из узлов/рёбер запроса.
 * @returns {{scenario:object, nodeIndex:Map<string,number>, linkIndex:Int32Array}}
 */
export function buildScenario({ nodes, links, environment, threats = [], meta = {}, nodeIndex, quality = 'fast' }) {
  const E = loadEng6().ENG6;
  const env = environment;
  const scNodes = new Array(nodes.length);
  for (let i = 0; i < nodes.length; i++) {
    const src = nodes[i];
    const p = normalizeNodeParams(src, E);
    scNodes[i] = {
      id: src.id,
      type: src.type,
      name: src.name || src.id,
      // NFR-03 Дополнения к ТЗ: координаты хранятся и передаются с точностью 6 знаков
      lat: round6(src.lat),
      lng: round6(src.lng),
      status: src.status || 'active',
      parentId: src.parentId ?? null,
      tags: Array.isArray(src.tags) ? src.tags : [],
      note: src.note || '',
      txPowerDbm: p.txPowerDbm,
      txGainDbi: p.txGainDbi,
      rxGainDbi: p.rxGainDbi,
      rxSensDbm: p.rxSensDbm,
      freqMhz: p.freqMhz,
      antHeightM: p.antHeightM,
      feederLossDb: p.feederLossDb,
      noiseFigureDb: p.noiseFigureDb,
      mobility: p.mobility,
    };
  }
  const scLinks = new Array(links.length);
  const linkIndex = new Int32Array(links.length);
  for (let e = 0; e < links.length; e++) {
    const src = links[e];
    linkIndex[e] = e;
    scLinks[e] = {
      id: src.id,
      from: src.from,
      to: src.to,
      type: src.type,
      status: src.status || 'active',
      priority: src.priority ?? 2,
      isBackup: !!src.isBackup,
      tags: [],
      reqSnrDb: src.reqSnrDb ?? '',
      fhss: src.fhss ?? 0,
      dsss: src.dsss ?? 0,
      freqMhz: src.freqMhz || E.LINK_TYPES[src.type]?.freq || 150,
      bwKhz: src.bwKhz || E.LINK_TYPES[src.type]?.bw || 25,
      mod: src.mod || E.LINK_TYPES[src.type]?.mod || 'QPSK',
      fec: src.fec || E.LINK_TYPES[src.type]?.fec || 'none',
    };
    if (src.type === 'FOC' && src.fiber) scLinks[e].fiber = src.fiber;
    if (src.type === 'SAT' && src.sat) scLinks[e].sat = src.sat;
    if (src.propModel) scLinks[e].propModel = src.propModel;
  }
  const scenario = {
    meta: {
      id: meta.id || `sc-plan-${Date.now().toString(36)}`,
      name: meta.name || 'ENG6-PLAN',
      version: '1.0.0',
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      author: meta.author || '',
      description: meta.description || '',
      datetime: meta.datetime || new Date().toISOString().slice(0, 16),
      counters: { node: scNodes.length, link: scLinks.length, threat: threats.length },
    },
    environment: env,
    nodes: scNodes,
    links: scLinks,
    threats: Array.isArray(threats) ? threats : [],
  };
  return { scenario, nodeIndex, linkIndex, quality };
}

/**
 * Вторая линия обороны: прогнать собранный сценарий через штатные валидаторы
 * ENG6. Возвращает список строк-замечаний (пустой = сценарий корректен).
 */
export function eng6Validate(scenario) {
  const E = loadEng6().ENG6;
  try {
    return E.validateScenario(scenario) || [];
  } catch (err) {
    return [`validateScenario: ${String((err && err.message) || err)}`];
  }
}
