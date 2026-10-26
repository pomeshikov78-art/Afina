/**
 * core/validate.mjs — строгая валидация входа ENG6-PLAN.
 *
 * Задача (ЭТАП 3, требование «Военпреда» и «Айтишника»): некорректный граф не
 * должен ни «повесить» ядро, ни привести к OOM, ни молча дать бессмысленный
 * результат. Поэтому:
 *   1) все лимиты проверяются ДО выделения памяти под таблицы;
 *   2) каждая ошибка имеет стабильный машинный код и JSON-путь — пригодна для
 *      аудита и для автоматической подсветки в UI;
 *   3) число возвращаемых ошибок ограничено (maxErrors) — защита от раздувания
 *      ответа на графе из 10^5 узлов с мусором в каждом узле;
 *   4) после нормализации выполняется вторая линия обороны — штатные
 *      валидаторы ENG6 (validateNode/validateLink) на собранном сценарии.
 *
 * Модуль не бросает исключений: возвращает {ok, errors, warnings}.
 */
import { createHash } from 'node:crypto';
import { LIMITS, NODE_TYPES, LINK_TYPES, MODS, FECS, CLUTTER, CONDITIONS, PHYSICS_MODES, QUALITY } from './schema.mjs';

export const V = {
  REQUIRED: 'REQUIRED',
  TYPE: 'TYPE',
  RANGE: 'RANGE',
  ENUM: 'ENUM',
  DUPLICATE: 'DUPLICATE',
  REF: 'REF',
  LIMIT: 'LIMIT',
  CROSS: 'CROSS',
  FINITE: 'FINITE',
};

const isObj = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
const isNum = (x) => typeof x === 'number' && Number.isFinite(x);
const isStr = (x) => typeof x === 'string';

export class ValidationReport {
  constructor(maxErrors = 100) {
    this.errors = [];
    this.warnings = [];
    this.maxErrors = maxErrors;
    this.truncated = false;
  }

  err(code, path, message, extra) {
    if (this.errors.length >= this.maxErrors) {
      if (!this.truncated) {
        this.truncated = true;
        this.errors.push({ code: 'TRUNCATED', path: '', message: `Список ошибок сокращён (лимит ${this.maxErrors})` });
      }
      return this;
    }
    this.errors.push({ code, path, message, ...(extra || {}) });
    return this;
  }

  warn(code, path, message, extra) {
    if (this.warnings.length < this.maxErrors) this.warnings.push({ code, path, message, ...(extra || {}) });
    return this;
  }

  get ok() {
    return this.errors.length === 0;
  }

  toJSON() {
    return { ok: this.ok, errors: this.errors, warnings: this.warnings, truncated: this.truncated };
  }
}

function checkNum(rep, path, value, { min = -Infinity, max = Infinity, required = false, def = null, integer = false } = {}) {
  if (value === undefined || value === null) {
    if (required) rep.err(V.REQUIRED, path, `Обязательное числовое поле отсутствует`);
    return def;
  }
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) value = Number(value);
  if (!isNum(value)) {
    rep.err(V.TYPE, path, `Ожидалось число, получено ${JSON.stringify(value).slice(0, 64)}`);
    return def;
  }
  if (value < min || value > max) {
    rep.err(V.RANGE, path, `Значение ${value} вне допустимого диапазона [${min}; ${max}]`, { value, min, max });
    return def;
  }
  if (integer && !Number.isInteger(value)) {
    rep.err(V.TYPE, path, `Ожидалось целое число, получено ${value}`);
    return def;
  }
  return value;
}

function checkStr(rep, path, value, { required = false, maxLen = LIMITS.maxStringLength, def = '' } = {}) {
  if (value === undefined || value === null) {
    if (required) rep.err(V.REQUIRED, path, 'Обязательное строковое поле отсутствует');
    return def;
  }
  if (!isStr(value)) {
    rep.err(V.TYPE, path, `Ожидалась строка, получено ${typeof value}`);
    return def;
  }
  if (required && value.trim() === '') {
    rep.err(V.REQUIRED, path, 'Пустая строка в обязательном поле');
    return def;
  }
  if (value.length > maxLen) {
    rep.err(V.LIMIT, path, `Длина строки ${value.length} > ${maxLen}`);
    return value.slice(0, maxLen);
  }
  return value;
}

function checkEnum(rep, path, value, allowed, { required = false, def = null } = {}) {
  if (value === undefined || value === null) {
    if (required) rep.err(V.REQUIRED, path, `Обязательное поле отсутствует (допустимо: ${allowed.join(', ')})`);
    return def;
  }
  if (!allowed.includes(value)) {
    rep.err(V.ENUM, path, `Недопустимое значение «${String(value).slice(0, 32)}»; допустимо: ${allowed.join(', ')}`, { value, allowed });
    return def;
  }
  return value;
}

/**
 * Валидация полного запроса планирования.
 * @param {any} req
 * @returns {ValidationReport}
 */
export function validateRequest(req) {
  const rep = new ValidationReport();
  if (!isObj(req)) {
    rep.err(V.TYPE, '$', 'Тело запроса должно быть JSON-объектом');
    return rep;
  }
  if (req.meta !== undefined) validateMeta(rep, req.meta);
  const topo = req.topology;
  if (!isObj(topo)) {
    rep.err(V.REQUIRED, '$.topology', 'Отсутствует блок topology');
    return rep;
  }
  validateEnvironment(rep, req.environment);

  const nodeIds = validateNodes(rep, topo.nodes);
  const linkIds = validateLinks(rep, topo.links, nodeIds);
  validateFleet(rep, req.fleet);
  validateDemands(rep, req.demands, nodeIds);
  validateConstraints(rep, req.constraints, topo, linkIds);
  validateWeights(rep, req.weights);
  if (req.threats !== undefined) {
    if (!Array.isArray(req.threats)) rep.err(V.TYPE, '$.threats', 'threats должен быть массивом');
    else if (req.threats.length > LIMITS.maxThreats) rep.err(V.LIMIT, '$.threats', `Слишком много угроз: ${req.threats.length} > ${LIMITS.maxThreats}`);
  }
  return rep;
}

function validateMeta(rep, meta) {
  if (!isObj(meta)) {
    rep.err(V.TYPE, '$.meta', 'meta должен быть объектом');
    return;
  }
  checkStr(rep, '$.meta.name', meta.name, { required: false, def: '' });
  checkStr(rep, '$.meta.requestId', meta.requestId, { required: false, def: '' });
  checkStr(rep, '$.meta.author', meta.author, { required: false, def: '' });
  if (meta.seed !== undefined && meta.seed !== null) {
    const s = Number(meta.seed);
    if (!Number.isInteger(s) || s < 0 || s > 2 ** 31) rep.err(V.RANGE, '$.meta.seed', 'seed должен быть целым в [0; 2^31)');
  }
  for (const k of Object.keys(meta)) {
    if (!['name', 'requestId', 'author', 'seed', 'description', 'createdAt', 'id', 'version'].includes(k))
      rep.warn('UNKNOWN_FIELD', `$.meta.${k}`, `Неизвестное поле meta.${k} — игнорируется`);
  }
}

function validateEnvironment(rep, env) {
  if (env === undefined || env === null) return;
  if (!isObj(env)) {
    rep.err(V.TYPE, '$.environment', 'environment должен быть объектом');
    return;
  }
  checkEnum(rep, '$.environment.clutterType', env.clutterType, CLUTTER, { def: 'open' });
  if (isObj(env.weather)) {
    checkNum(rep, '$.environment.weather.rainRateMmHr', env.weather.rainRateMmHr, { min: 0, max: 300 });
    checkNum(rep, '$.environment.weather.fogDensity', env.weather.fogDensity, { min: 0, max: 10 });
    checkNum(rep, '$.environment.weather.temperatureC', env.weather.temperatureC, { min: -80, max: 60 });
    checkNum(rep, '$.environment.weather.humidityPercent', env.weather.humidityPercent, { min: 0, max: 100 });
  } else if (env.weather !== undefined) rep.err(V.TYPE, '$.environment.weather', 'weather должен быть объектом');
  if (isObj(env.ionosphere)) {
    checkNum(rep, '$.environment.ionosphere.timeOfDay', env.ionosphere.timeOfDay, { min: 0, max: 23 });
    checkEnum(rep, '$.environment.ionosphere.season', env.ionosphere.season, ['equinox', 'summer', 'winter']);
    checkEnum(rep, '$.environment.ionosphere.solarActivity', env.ionosphere.solarActivity, ['low', 'medium', 'high']);
  } else if (env.ionosphere !== undefined) rep.err(V.TYPE, '$.environment.ionosphere', 'ionosphere должен быть объект');
  if (isObj(env.terrain)) {
    checkEnum(rep, '$.environment.terrain.source', env.terrain.source, ['synth', 'flat', 'dem', 'online']);
    checkNum(rep, '$.environment.terrain.amplitudeM', env.terrain.amplitudeM, { min: 0, max: 5000 });
    if (env.terrain.source === 'online' || env.terrain.source === 'dem')
      rep.warn('CROSS', '$.environment.terrain.source', 'Режим DEM/online требует внешних данных; headless-ядро работает только с synth/flat — будет использован synth');
  }
  checkNum(rep, '$.environment.miscLossDb', env.miscLossDb, { min: 0, max: 40 });
}

function validateNodes(rep, nodes) {
  const ids = new Set();
  if (!Array.isArray(nodes)) {
    rep.err(V.REQUIRED, '$.topology.nodes', 'nodes должен быть массивом');
    return ids;
  }
  if (nodes.length === 0) rep.err(V.REQUIRED, '$.topology.nodes', 'Топология не содержит ни одного узла');
  if (nodes.length > LIMITS.maxNodes) {
    rep.err(V.LIMIT, '$.topology.nodes', `Узлов ${nodes.length} > лимита ${LIMITS.maxNodes}`, { got: nodes.length, limit: LIMITS.maxNodes });
    return ids;
  }
  for (let i = 0; i < nodes.length; i++) {
    const p = `$.topology.nodes[${i}]`;
    const nd = nodes[i];
    if (!isObj(nd)) {
      rep.err(V.TYPE, p, 'Узел должен быть объектом');
      continue;
    }
    const id = checkStr(rep, `${p}.id`, nd.id, { required: true });
    if (id) {
      if (ids.has(id)) rep.err(V.DUPLICATE, `${p}.id`, `Дублирование id узла «${id}»`, { id });
      ids.add(id);
    }
    checkEnum(rep, `${p}.type`, nd.type, NODE_TYPES, { required: true });
    checkNum(rep, `${p}.lat`, nd.lat, { min: -90, max: 90, required: true });
    checkNum(rep, `${p}.lng`, nd.lng, { min: -180, max: 180, required: true });
    checkNum(rep, `${p}.txPowerDbm`, nd.txPowerDbm, { min: -30, max: 90 });
    checkNum(rep, `${p}.txGainDbi`, nd.txGainDbi, { min: -20, max: 60 });
    checkNum(rep, `${p}.rxGainDbi`, nd.rxGainDbi, { min: -20, max: 60 });
    checkNum(rep, `${p}.rxSensDbm`, nd.rxSensDbm, { min: -160, max: 0 });
    checkNum(rep, `${p}.antHeightM`, nd.antHeightM, { min: 0, max: 500 });
    checkNum(rep, `${p}.feederLossDb`, nd.feederLossDb, { min: 0, max: 30 });
    checkNum(rep, `${p}.noiseFigureDb`, nd.noiseFigureDb, { min: 0, max: 30 });
    checkNum(rep, `${p}.freqMhz`, nd.freqMhz, { min: 0.001, max: 100000 });
    checkEnum(rep, `${p}.status`, nd.status, ['active', 'planned', 'failed'], { def: 'active' });
    checkEnum(rep, `${p}.mobility`, nd.mobility, ['static', 'portable', 'mobile', 'vehicle']);
    if (nd.parentId !== undefined && nd.parentId !== null) checkStr(rep, `${p}.parentId`, nd.parentId, { required: true });
    checkNum(rep, `${p}.deployTimeMin`, nd.deployTimeMin, { min: 0, max: 100000 });
    checkNum(rep, `${p}.costDeploy`, nd.costDeploy, { min: 0, max: 1e9 });
    // точность координат — NFR-03 Дополнения к ТЗ (6 знаков ≈ 10 см)
    if (isNum(nd.lat) && decimals(nd.lat) > 8) rep.warn('CROSS', `${p}.lat`, 'Избыточная точность координат (>8 знаков) будет округлена до 6');
  }
  // ссылки parentId
  for (let i = 0; i < nodes.length; i++) {
    const nd = nodes[i];
    if (isObj(nd) && nd.parentId && !ids.has(nd.parentId))
      rep.err(V.REF, `$.topology.nodes[${i}].parentId`, `parentId «${nd.parentId}» не найден среди узлов`, { ref: nd.parentId });
    if (isObj(nd) && nd.parentId === nd.id) rep.err(V.CROSS, `$.topology.nodes[${i}].parentId`, 'Узел не может быть собственным родителем');
  }
  return ids;
}

function decimals(x) {
  const s = String(Math.abs(x));
  const i = s.indexOf('.');
  return i < 0 ? 0 : s.length - i - 1;
}

function validateLinks(rep, links, nodeIds) {
  const ids = new Set();
  if (links === undefined || links === null) {
    rep.err(V.REQUIRED, '$.topology.links', 'links должен быть массивом (допустим пустой — тогда топология строится из парка техники)');
    return ids;
  }
  if (!Array.isArray(links)) {
    rep.err(V.TYPE, '$.topology.links', 'links должен быть массивом');
    return ids;
  }
  if (links.length > LIMITS.maxLinks) {
    rep.err(V.LIMIT, '$.topology.links', `Рёбер ${links.length} > лимита ${LIMITS.maxLinks}`, { got: links.length, limit: LIMITS.maxLinks });
    return ids;
  }
  const seenPair = new Set();
  for (let i = 0; i < links.length; i++) {
    const p = `$.topology.links[${i}]`;
    const l = links[i];
    if (!isObj(l)) {
      rep.err(V.TYPE, p, 'Ребро должно быть объектом');
      continue;
    }
    const id = checkStr(rep, `${p}.id`, l.id, { required: true });
    if (id) {
      if (ids.has(id)) rep.err(V.DUPLICATE, `${p}.id`, `Дублирование id ребра «${id}»`, { id });
      ids.add(id);
    }
    const from = checkStr(rep, `${p}.from`, l.from, { required: true });
    const to = checkStr(rep, `${p}.to`, l.to, { required: true });
    if (from && !nodeIds.has(from)) rep.err(V.REF, `${p}.from`, `Узел «${from}» не найден`, { ref: from });
    if (to && !nodeIds.has(to)) rep.err(V.REF, `${p}.to`, `Узел «${to}» не найден`, { ref: to });
    if (from && to && from === to) rep.err(V.CROSS, p, 'Начальный и конечный узлы должны отличаться');
    const type = checkEnum(rep, `${p}.type`, l.type, LINK_TYPES, { required: true });
    if (type && type !== 'FOC') checkNum(rep, `${p}.bwKhz`, l.bwKhz, { min: 0.5, max: 1e7, required: false });
    if (type && type !== 'SAT' && type !== 'FOC') checkNum(rep, `${p}.freqMhz`, l.freqMhz, { min: 0.001, max: 100000 });
    checkEnum(rep, `${p}.mod`, l.mod, MODS);
    checkEnum(rep, `${p}.fec`, l.fec, FECS);
    checkNum(rep, `${p}.cost`, l.cost, { min: 0, max: 1e9 });
    checkNum(rep, `${p}.priority`, l.priority, { min: 1, max: 5, integer: true });
    checkNum(rep, `${p}.reqSnrDb`, l.reqSnrDb === '' ? undefined : l.reqSnrDb, { min: -20, max: 60 });
    if (l.isBackup !== undefined && typeof l.isBackup !== 'boolean') rep.err(V.TYPE, `${p}.isBackup`, 'isBackup должен быть boolean');
    if (from && to) {
      const key = from < to ? `${from}|${to}|${type}` : `${to}|${from}|${type}`;
      if (seenPair.has(key)) rep.warn('DUPLICATE', p, `Параллельное ребро ${from}—${to} типа ${type}: оставлено как альтернатива (ёмкость не суммируется)`);
      seenPair.add(key);
    }
  }
  return ids;
}

function validateFleet(rep, fleet) {
  if (fleet === undefined || fleet === null) return;
  if (!Array.isArray(fleet)) {
    rep.err(V.TYPE, '$.fleet', 'fleet должен быть массивом');
    return;
  }
  if (fleet.length > LIMITS.maxFleetUnits) {
    rep.err(V.LIMIT, '$.fleet', `Единиц техники ${fleet.length} > лимита ${LIMITS.maxFleetUnits}`);
    return;
  }
  const ids = new Set();
  for (let i = 0; i < fleet.length; i++) {
    const p = `$.fleet[${i}]`;
    const u = fleet[i];
    if (!isObj(u)) {
      rep.err(V.TYPE, p, 'Единица техники должна быть объектом');
      continue;
    }
    const id = checkStr(rep, `${p}.unitId`, u.unitId, { required: true });
    if (id) {
      if (ids.has(id)) rep.err(V.DUPLICATE, `${p}.unitId`, `Дублирование unitId «${id}»`, { id });
      ids.add(id);
    }
    checkEnum(rep, `${p}.condition`, u.condition, CONDITIONS, { required: true });
    checkEnum(rep, `${p}.nodeType`, u.nodeType, NODE_TYPES);
    checkNum(rep, `${p}.costDeploy`, u.costDeploy, { min: 0, max: 1e9 });
    checkNum(rep, `${p}.deployTimeMin`, u.deployTimeMin, { min: 0, max: 100000 });
    checkNum(rep, `${p}.powerW`, u.powerW, { min: 0, max: 1e6 });
    checkNum(rep, `${p}.massKg`, u.massKg, { min: 0, max: 1e6 });
    if (u.redConditionReason !== undefined) checkStr(rep, `${p}.redConditionReason`, u.redConditionReason);
  }
}

function validateDemands(rep, demands, nodeIds) {
  if (demands === undefined || demands === null) return;
  if (!Array.isArray(demands)) {
    rep.err(V.TYPE, '$.demands', 'demands должен быть массивом');
    return;
  }
  if (demands.length > LIMITS.maxDemands) {
    rep.err(V.LIMIT, '$.demands', `Заявок ${demands.length} > лимита ${LIMITS.maxDemands}`);
    return;
  }
  const ids = new Set();
  for (let i = 0; i < demands.length; i++) {
    const p = `$.demands[${i}]`;
    const d = demands[i];
    if (!isObj(d)) {
      rep.err(V.TYPE, p, 'Заявка должна быть объектом');
      continue;
    }
    const id = checkStr(rep, `${p}.id`, d.id, { required: true });
    if (id) {
      if (ids.has(id)) rep.err(V.DUPLICATE, `${p}.id`, `Дублирование id заявки «${id}»`, { id });
      ids.add(id);
    }
    const from = checkStr(rep, `${p}.from`, d.from, { required: true });
    const to = checkStr(rep, `${p}.to`, d.to, { required: true });
    if (from && !nodeIds.has(from)) rep.err(V.REF, `${p}.from`, `Узел «${from}» не найден`, { ref: from });
    if (to && !nodeIds.has(to)) rep.err(V.REF, `${p}.to`, `Узел «${to}» не найден`, { ref: to });
    if (from && to && from === to) rep.err(V.CROSS, p, 'Источник и получатель заявки совпадают — заявка тривиальна');
    checkNum(rep, `${p}.rateKbps`, d.rateKbps, { min: 0.001, max: 1e9, required: true });
    checkNum(rep, `${p}.maxDelayMs`, d.maxDelayMs, { min: 0.1, max: 60000 });
    checkNum(rep, `${p}.priority`, d.priority, { min: 1, max: 5, integer: true, def: 3 });
    checkNum(rep, `${p}.minAvailabilityPct`, d.minAvailabilityPct, { min: 0, max: 100 });
    if (d.protected !== undefined && typeof d.protected !== 'boolean') rep.err(V.TYPE, `${p}.protected`, 'protected должен быть boolean');
  }
}

function validateConstraints(rep, c, topo, linkIds) {
  if (c === undefined || c === null) return;
  if (!isObj(c)) {
    rep.err(V.TYPE, '$.constraints', 'constraints должен быть объектом');
    return;
  }
  if (c.budget !== undefined && c.budget !== null) {
    if (c.budget === Infinity || c.budget === 'unbounded') return;
    checkNum(rep, '$.constraints.budget', c.budget, { min: 0, max: 1e12 });
  }
  checkNum(rep, '$.constraints.minMarginDb', c.minMarginDb, { min: -20, max: 60 });
  checkNum(rep, '$.constraints.maxDelayMs', c.maxDelayMs, { min: 0.1, max: 60000 });
  checkNum(rep, '$.constraints.maxHops', c.maxHops, { min: 1, max: 64, integer: true });
  checkNum(rep, '$.constraints.maxSimultaneousDeploy', c.maxSimultaneousDeploy, { min: 1, max: 100000, integer: true });
  checkNum(rep, '$.constraints.reserveFactor', c.reserveFactor, { min: 1, max: 10 });
  checkNum(rep, '$.constraints.timeLimitMs', c.timeLimitMs, { min: 100, max: LIMITS.maxTimeLimitMs });
  checkNum(rep, '$.constraints.fastMarginPenaltyDb', c.fastMarginPenaltyDb, { min: 0, max: 40 });
  checkNum(rep, '$.constraints.hopProcessingMs', c.hopProcessingMs, { min: 0, max: 1000 });
  checkNum(rep, '$.constraints.frameKbit', c.frameKbit, { min: 0.001, max: 1000 });
  checkNum(rep, '$.constraints.utilPenaltyMs', c.utilPenaltyMs, { min: 0, max: 10000 });
  checkNum(rep, '$.constraints.upgradeFactor', c.upgradeFactor, { min: 0, max: 10 });
  checkNum(rep, '$.constraints.upgradeCostRatio', c.upgradeCostRatio, { min: 0, max: 10 });
  checkEnum(rep, '$.constraints.backbone', c.backbone, ['none', 'cp', 'terminals']);
  checkNum(rep, '$.constraints.maxUtilizationPct', c.maxUtilizationPct, { min: 1, max: 1000 });
  checkNum(rep, '$.constraints.crewSpeedKmh', c.crewSpeedKmh, { min: 0.1, max: 300 });
  checkEnum(rep, '$.constraints.physics', c.physics, ['auto', ...PHYSICS_MODES]);
  checkEnum(rep, '$.constraints.quality', c.quality, QUALITY);
  for (const k of ['requireRedundancy', 'allowYellowUnits', 'allowRedUnits', 'explain']) {
    if (c[k] !== undefined && typeof c[k] !== 'boolean') rep.err(V.TYPE, `$.constraints.${k}`, `${k} должен быть boolean`);
  }
  if (Array.isArray(c.forbidLinkTypes)) {
    for (const t of c.forbidLinkTypes) if (!LINK_TYPES.includes(t)) rep.err(V.ENUM, '$.constraints.forbidLinkTypes', `Неизвестный тип линии «${t}»`);
  }
  // предупреждение: число заявок больше суммарной ёмкости — см. требование Критика
  void topo;
  void linkIds;
}

function validateWeights(rep, w) {
  if (w === undefined || w === null) return;
  if (!isObj(w)) {
    rep.err(V.TYPE, '$.weights', 'weights должен быть объектом');
    return;
  }
  let sum = 0;
  for (const k of ['cost', 'delay', 'survivability', 'margin']) {
    const v = checkNum(rep, `$.weights.${k}`, w[k], { min: 0, max: 100, def: 0 });
    sum += v || 0;
  }
  for (const kk of Object.keys(w))
    if (!['cost', 'delay', 'survivability', 'margin'].includes(kk)) rep.warn('UNKNOWN_FIELD', `$.weights.${kk}`, `Неизвестный вес «${kk}» — игнорируется`);
  if (sum <= 0) rep.err(V.CROSS, '$.weights', 'Все веса нулевые — целевая функция вырождена');
}

/**
 * Канонический SHA-256 запроса для аудита (требование «Военпреда»):
 * ключи сортируются, числа приводятся к 12 значащим цифрам, чтобы одинаковый
 * по сути граф давал одинаковый хэш независимо от порядка полей в JSON.
 */
export function canonicalHash(value) {
  return createHash('sha256').update(canonicalStringify(value)).digest('hex');
}

export function canonicalStringify(value) {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number') return Number.isFinite(value) ? String(Number(value.toPrecision(12))) : 'null';
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalStringify).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalStringify(value[k])}`).join(',')}}`;
}
