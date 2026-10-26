/* ═══════════════════════════════════════════════════════════════════════════
   ENG6 · Визуальный картографический планировщик — клиентский модуль.
   Дополнение к ТЗ №1 (ГОСТ 34.602). Внешние библиотеки НЕ используются:
   геоподложка синтетическая (векторно-растровая, детерминированный шум),
   рендеринг — Canvas2D с батчингом и кэшем слоя рельефа (NFR-01).

   NFR-02 (БЕЗОПАСНОСТЬ): координаты размещения узлов связи НЕ сохраняются в
   localStorage / sessionStorage / IndexedDB / cookie. Состояние живёт только
   в памяти вкладки; выгрузка — явным действием пользователя (Экспорт JSON).
   Все обращения к ядру — только по настроенному base URL (в production — TLS).

   NFR-03 (ТОЧНОСТЬ): координаты хранятся и передаются с точностью 6 знаков
   после запятой (~0,11 м по широте).

   Структура файла:
     §1  PURE — чистые функции (проекция, генерализация, кластеризация, CSV,
         формирование пакета для ядра) — вынесены в window.ENG6UI.pure и
         покрыты unit-тестами tests/unit/frontend_pure.test.mjs;
     §2  Состояние (store) и журнал;
     §3  Геоподложка и рендер карты;
     §4  Тактические знаки (условные обозначения);
     §5  ОШС-панель и drag-and-drop;
     §6  Взаимодействие (выбор, линия, узел связи, контекстное меню, карточка);
     §7  Интеграция с ядром ENG6-PLAN (FR-INT-02/03);
     §8  Панель результатов;
     §9  Импорт/экспорт, справка, горячие клавиши, инициализация.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ═══ §1. PURE ══════════════════════════════════════════════════════════ */

  const DEG = Math.PI / 180;
  const TILE = 256;
  const MAX_LAT = 85.0511287798;

  /** Web-Mercator: доля мирового размера по X. */
  function lngToWorldX(lng) { return (lng + 180) / 360; }
  function worldXToLng(x) { return x * 360 - 180; }
  function latToWorldY(lat) {
    const l = Math.max(-MAX_LAT, Math.min(MAX_LAT, lat));
    return 0.5 - Math.log(Math.tan(Math.PI / 4 + (l * DEG) / 2)) / (2 * Math.PI);
  }
  function worldYToLat(y) {
    const v = Math.max(-0.5, Math.min(0.5, 0.5 - y));
    return (2 * Math.atan(Math.exp(v * 2 * Math.PI)) - Math.PI / 2) / DEG;
  }

  /**
   * Проекция «вид ↔ мир». scale = TILE·2^zoom (пикселей на весь мир).
   * @returns {{x:number,y:number}}
   */
  function project(view, lat, lng) {
    const s = TILE * Math.pow(2, view.zoom);
    return { x: (lngToWorldX(lng) * s) - view.ox, y: (latToWorldY(lat) * s) - view.oy };
  }
  function unproject(view, px, py) {
    const s = TILE * Math.pow(2, view.zoom);
    return { lng: worldXToLng((px + view.ox) / s), lat: worldYToLat((py + view.oy) / s) };
  }
  /** Смещения view (ox, oy) для центрирования на (lat, lng) при данном размере окна. */
  function viewOffsets(lat, lng, zoom, w, h) {
    const s = TILE * Math.pow(2, zoom);
    return { ox: lngToWorldX(lng) * s - w / 2, oy: latToWorldY(lat) * s - h / 2 };
  }
  /** Масштаб «1 : N» и длина пикселя в метрах на данной широте. */
  function metersPerPixel(lat, zoom) {
    return (156543.03392 * Math.cos(lat * DEG)) / Math.pow(2, zoom);
  }
  function scaleDenominator(lat, zoom, dpi = 96) {
    return (metersPerPixel(lat, zoom) * dpi) / 0.0254;
  }
  /** Расстояние по гаверсинусу, км (та же формула, что в ядре ENG6). */
  function haversineKm(a, b) {
    const dLat = (b.lat - a.lat) * DEG;
    const dLng = (b.lng - a.lng) * DEG;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * DEG) * Math.cos(b.lat * DEG) * Math.sin(dLng / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function centroid(points) {
    if (!points.length) return null;
    let la = 0, ln = 0;
    for (const p of points) { la += p.lat; ln += p.lng; }
    return { lat: round6(la / points.length), lng: round6(ln / points.length) };
  }
  /** NFR-03: 6 знаков после запятой. */
  function round6(x) { return Math.round(x * 1e6) / 1e6; }

  function fitBounds(points, w, h, { minZoom = 3, maxZoom = 16, padding = 0.12 } = {}) {
    if (!points || !points.length) return { lat: 55.75, lng: 37.62, zoom: 6 };
    let minLat = 90, maxLat = -90, minLng = 180, maxLng = -180;
    for (const p of points) {
      if (p.lat < minLat) minLat = p.lat;
      if (p.lat > maxLat) maxLat = p.lat;
      if (p.lng < minLng) minLng = p.lng;
      if (p.lng > maxLng) maxLng = p.lng;
    }
    const lat = (minLat + maxLat) / 2;
    const lng = (minLng + maxLng) / 2;
    if (points.length === 1) return { lat, lng, zoom: 12 };
    let zoom = maxZoom;
    for (let z = maxZoom; z >= minZoom; z--) {
      const s = TILE * Math.pow(2, z);
      const dx = Math.abs(lngToWorldX(maxLng) - lngToWorldX(minLng)) * s;
      const dy = Math.abs(latToWorldY(minLat) - latToWorldY(maxLat)) * s;
      if (dx <= w * (1 - padding * 2) && dy <= h * (1 - padding * 2)) { zoom = z; break; }
    }
    return { lat, lng, zoom };
  }

  /* ── FR-MAP-07: генерализация тактических знаков ─────────────────────── */
  const GEN = Object.freeze({ DETAIL: 'detail', NODE: 'node', UNIT: 'unit' });
  const GEN_ZOOM = Object.freeze({ detail: 12, unit: 8 }); // detail ≥12, node 8…12, unit <8

  function generalizationLevel(zoom, thresholds = GEN_ZOOM) {
    if (zoom >= thresholds.detail) return GEN.DETAIL;
    if (zoom >= thresholds.unit) return GEN.NODE;
    return GEN.UNIT;
  }

  /**
   * Кластеризация сеткой в ЭКРАННЫХ координатах (детерминированная, O(n)).
   * Применяется на уровне UNIT: близко расположенные узлы связи одного
   * подразделения сливаются в знак «Подразделение связи».
   * @param {{x:number,y:number,groupKey:string}[]} items
   * @param {number} cellPx размер ячейки (обычно 64…96 px)
   * @returns {{key:string, items:object[], x:number, y:number}[]}
   */
  function gridCluster(items, cellPx = 72) {
    const map = new Map();
    for (const it of items) {
      const cx = Math.floor(it.x / cellPx);
      const cy = Math.floor(it.y / cellPx);
      const k = `${it.groupKey}|${cx}|${cy}`;
      let c = map.get(k);
      if (!c) { c = { key: k, items: [], sx: 0, sy: 0, groupKey: it.groupKey }; map.set(k, c); }
      c.items.push(it);
      c.sx += it.x; c.sy += it.y;
    }
    const out = [];
    for (const c of map.values()) {
      out.push({ key: c.key, items: c.items, x: c.sx / c.items.length, y: c.sy / c.items.length, groupKey: c.groupKey });
    }
    return out.sort((a, b) => (a.key < b.key ? -1 : 1));
  }

  /**
   * Радиус зоны покрытия узла по активным линиям (км).
   * Берётся максимальная дальность среди его активных линий; если линий нет —
   * оценка по бюджету: r = d·10^((margin − minMargin)/(10·n)), n — показатель
   * log-distance из таблицы CLUTTER ENG6.
   */
  function coverageRadiusKm(node, links, { fallbackKm = 12, n = 3.2 } = {}) {
    let best = 0;
    for (const l of links) {
      if (l.from !== node.id && l.to !== node.id) continue;
      const d = l.distKm || haversineKm(node, l._otherEnd || node);
      if (d > best) best = d;
    }
    if (best > 0) return round6(best * 1.15);
    return fallbackKm;
  }

  /** Цвет линии по запасу, дБ (пороги согласованы с ядром: 3 / 10 / 20 дБ). */
  function linkColorByMargin(marginDb, { rejected = false } = {}) {
    if (rejected) return '#6b4a52';
    if (!Number.isFinite(marginDb)) return '#6b7280';
    if (marginDb >= 20) return '#3fb950';
    if (marginDb >= 10) return '#7ee2a8';
    if (marginDb >= 3) return '#d29922';
    if (marginDb >= 0) return '#f0883e';
    return '#c93545';
  }
  function linkWidthByCapacity(kbps) {
    if (!kbps || kbps <= 0) return 1;
    return Math.max(1, Math.min(6, 1 + Math.log10(1 + kbps) * 0.9));
  }

  /* ── Парсер CSV (импорт исходных данных, FR-UI-01) ──────────────────── */
  /**
   * Разбор CSV с учётом кавычек, разделителя (, ; \t) и заголовка.
   * @returns {{header:string[], rows:string[][]}}
   */
  function parseCsv(text) {
    const src = String(text).replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n');
    // разделитель определяется по первой НЕПУСТОЙ строке: секции [nodes]/[links]
    // начинаются с перевода строки, иначе детектор видел пустую строку и брал ','
    const firstLine = src.split('\n').find((l) => l.trim() !== '') || '';
    const delim = [',', ';', '\t'].map((d) => ({ d, c: (firstLine.match(new RegExp('\\' + (d === '\t' ? 't' : d), 'g')) || []).length })).sort((a, b) => b.c - a.c)[0].d || ',';
    const rows = [];
    let row = [], field = '', inQ = false;
    for (let i = 0; i < src.length; i++) {
      const ch = src[i];
      if (inQ) {
        if (ch === '"') { if (src[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
        else field += ch;
      } else if (ch === '"') inQ = true;
      else if (ch === delim) { row.push(field); field = ''; }
      else if (ch === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
      else field += ch;
    }
    if (field.length || row.length) { row.push(field); rows.push(row); }
    const clean = rows.filter((r) => r.some((c) => String(c).trim() !== ''));
    if (!clean.length) return { header: [], rows: [] };
    return { header: clean[0].map((h) => h.trim()), rows: clean.slice(1) };
  }

  /**
   * CSV → сценарий. Поддерживаются два листа в одном файле (секции [nodes]/[links])
   * либо плоский список узлов с колонками: id,type,name,lat,lng,parentId,status.
   */
  function csvToScenario(text) {
    const nodes = [], links = [], demands = [];
    const sections = String(text).split(/^\s*\[(nodes|links|demands)\]\s*$/mi);
    const push = (arr, obj) => arr.push(obj);
    if (sections.length >= 3) {
      for (let i = 1; i < sections.length; i += 2) {
        const kind = sections[i].toLowerCase();
        const { header, rows } = parseCsv(sections[i + 1]);
        for (const r of rows) {
          const o = {};
          header.forEach((h, k) => { o[h] = r[k] === undefined ? '' : String(r[k]).trim(); });
          if (kind === 'nodes') push(nodes, normalizeNodeCsv(o));
          else if (kind === 'links') push(links, normalizeLinkCsv(o));
          else push(demands, normalizeDemandCsv(o));
        }
      }
    } else {
      const { header, rows } = parseCsv(text);
      const lower = header.map((h) => h.toLowerCase());
      const isLinks = lower.includes('from') && lower.includes('to');
      for (const r of rows) {
        const o = {};
        header.forEach((h, k) => { o[h] = r[k] === undefined ? '' : String(r[k]).trim(); });
        if (isLinks) push(links, normalizeLinkCsv(o));
        else push(nodes, normalizeNodeCsv(o));
      }
    }
    return { nodes, links, demands };
  }
  /** Секция [demands]: наряды/заявки на каналы связи. */
  function normalizeDemandCsv(o) {
    const g = (names, def) => { for (const n of names) { for (const k of Object.keys(o)) if (k.toLowerCase() === n && o[k] !== '') return o[k]; } return def; };
    return {
      id: String(g(['id', 'номер', 'наряд'], '') || '').trim(),
      from: String(g(['from', 'от', 'источник'], '')).trim(),
      to: String(g(['to', 'до', 'получатель'], '')).trim(),
      rateKbps: numOr(g(['ratekbps', 'скорость', 'полоса', 'kbps'], 16), 16),
      maxDelayMs: numOr(g(['maxdelayms', 'задержка', 'мс'], 250), 250),
      priority: numOr(g(['priority', 'приоритет'], 3), 3),
    };
  }
  /**
   * Числовое поле из CSV. Пустая строка — это «не задано», а НЕ ноль:
   * иначе линии получали bwKhz: 0 / freqMhz: 0 и справедливо браковались
   * валидатором ядра (RANGE). Обнаружено сквозным прогоном CSV-импорта.
   */
  function numOr(v, def) {
    if (v === undefined || v === null) return def;
    const str = String(v).trim();
    if (str === '') return def;
    const n = Number(str.replace(',', '.'));
    return Number.isFinite(n) ? n : def;
  }
  function normalizeNodeCsv(o) {
    const g = (names, def) => { for (const n of names) { for (const k of Object.keys(o)) if (k.toLowerCase() === n && o[k] !== '') return o[k]; } return def; };
    return {
      id: String(g(['id', 'идентификатор', 'name'], '') || '').trim(),
      type: String(g(['type', 'тип'], 'radio')).trim(),
      name: String(g(['label', 'наименование', 'title'], '') || '').trim(),
      lat: round6(numOr(g(['lat', 'широта'], 0), 0)),
      lng: round6(numOr(g(['lng', 'lon', 'долгота'], 0), 0)),
      parentId: g(['parentid', 'parent', 'подчинение'], null) || null,
      status: g(['status', 'состояние'], 'active') || 'active',
      unitId: g(['unitid', 'инвентарный номер', 'инв. №'], null) || null,
    };
  }
  function normalizeLinkCsv(o) {
    const g = (names, def) => { for (const n of names) { for (const k of Object.keys(o)) if (k.toLowerCase() === n && o[k] !== '') return o[k]; } return def; };
    return {
      id: String(g(['id'], '') || '').trim(),
      from: String(g(['from', 'от'], '')).trim(),
      to: String(g(['to', 'до'], '')).trim(),
      type: String(g(['type', 'тип'], 'VHF')).trim(),
      bwKhz: numOr(g(['bwkhz', 'полоса'], ''), undefined),
      freqMhz: numOr(g(['freqmhz', 'частота'], ''), undefined),
    };
  }

  /* ── FR-INT-02: формирование JSON-пакета для ядра ───────────────────── */
  const BAND_BY_LINK_TYPE = { HF: 'КВ', VHF: 'УКВ', UHF: 'УКВ', RRL: 'РТ', SAT: 'СПУТНИК', FOC: 'ВОЛС' };
  const LINK_TYPE_BY_BAND = { 'КВ': 'HF', 'УКВ': 'VHF', 'РТ': 'RRL', 'СПУТНИК': 'SAT', 'ВОЛС': 'FOC' };

  /**
   * Собрать запрос ENG6-PLAN из состояния планировщика.
   * Координаты — WGS-84, 6 знаков (NFR-03); параметры антенн/мощностей — из
   * карточки техники; состояние 🔴 исключает размещение (FR-RES-03).
   */
  function buildRequest(state, opts = {}) {
    const nodes = [];
    const links = [];
    const placed = state.objects.filter((o) => o.kind === 'asset' || o.kind === 'nodeGroup');
    for (const o of placed) {
      if (o.kind === 'nodeGroup') {
        // FR-MAP-04: узел связи — единый логический объект; в ядро уходит
        // один узел с параметрами лучшей станции группы
        const members = state.objects.filter((x) => x.kind === 'asset' && x.groupId === o.id);
        const best = members.slice().sort((a, b) => (b.spec?.txPowerDbm || 0) - (a.spec?.txPowerDbm || 0))[0] || null;
        nodes.push(makeNode(o, best ? best.spec : null, { children: members.map((m) => m.id) }));
      } else {
        nodes.push(makeNode(o, o.spec || null, { groupId: o.groupId || null }));
      }
    }
    const idset = new Set(nodes.map((n) => n.id));
    for (const l of state.lines) {
      if (!idset.has(l.from) || !idset.has(l.to)) continue;
      links.push({
        id: l.id,
        from: l.from,
        to: l.to,
        type: l.type || 'VHF',
        bwKhz: l.bwKhz,
        freqMhz: l.freqMhz,
        mod: l.mod,
        fec: l.fec,
        priority: l.priority ?? 2,
        isBackup: !!l.isBackup,
        cost: l.cost,
      });
    }
    // автоматические кандидаты: если линий мало — ядро получит только явные
    const demands = (state.demands || []).map((d, i) => ({
      id: d.id || `q${i + 1}`, from: d.from, to: d.to, rateKbps: d.rateKbps,
      maxDelayMs: d.maxDelayMs, priority: d.priority ?? 3, protected: !!d.protected,
    })).filter((d) => idset.has(d.from) && idset.has(d.to));
    const fleet = state.fleet.map((u) => ({
      unitId: u.unitId, kind: u.kind, nodeType: u.nodeType, condition: u.condition,
      costDeploy: u.costDeploy, deployTimeMin: u.deployTimeMin, powerW: u.powerW, note: u.note || '',
    }));
    return {
      meta: { name: state.meta.name || 'Сценарий планировщика', author: state.meta.author || '', requestId: opts.requestId || null, seed: state.meta.seed ?? 20260116 },
      environment: state.environment,
      topology: { nodes, links },
      demands,
      fleet: fleet.length ? fleet : undefined,
      constraints: { ...state.constraints, physics: opts.physics || state.constraints.physics || 'auto' },
      weights: state.weights,
    };
    function makeNode(o, spec, extra) {
      return {
        id: o.id, type: o.nodeType || (spec && spec.nodeType) || 'radio', name: o.name || o.id,
        lat: round6(o.lat), lng: round6(o.lng), status: o.status || 'active',
        parentId: o.parentId ?? null, unitId: o.unitId ?? null,
        txPowerDbm: spec?.txPowerDbm, txGainDbi: spec?.txGainDbi, rxGainDbi: spec?.rxGainDbi,
        rxSensDbm: spec?.rxSensDbm, antHeightM: spec?.antHeightM, freqMhz: spec?.freqMhz,
        feederLossDb: spec?.feederLossDb, noiseFigureDb: spec?.noiseFigureDb, mobility: spec?.mobility,
        deployTimeMin: spec?.deployTimeMin, costDeploy: spec?.costDeploy, ...extra,
      };
    }
  }

  /**
   * Наложить результат расчёта на состояние: статусы линий, маршруты, покрытие.
   * (FR-INT-03: ядро возвращает данные для отрисовки оверлеев.)
   */
  function applyPlanToState(state, result) {
    state.lastResult = result || null;
    state.linkById = new Map();
    state.nodeById = new Map();
    for (const o of state.objects) state.nodeById.set(o.id, o);
    if (!result || !result.plan) return { applied: 0, rejected: 0 };
    let applied = 0;
    for (const l of result.plan.activeLinks || []) { state.linkById.set(l.id, l); applied++; }
    for (const l of result.plan.rejectedLinks || []) state.linkById.set(l.id, { ...l, rejected: true });
    return { applied, rejected: (result.plan.rejectedLinks || []).length };
  }

  /* ═══ §2. Состояние ════════════════════════════════════════════════════ */

  /**
   * NFR-02: никакой персистенции в браузере. Состояние — только в памяти.
   * (Проверка: tests/unit/frontend_pure.test.mjs сканирует исходник на
   * localStorage/sessionStorage/indexedDB/document.cookie.)
   */
  const state = {
    meta: { name: 'Учебный сценарий: батальонный узел связи', author: 'ENG6-UI', seed: 20260116 },
    environment: {
      terrain: { source: 'synth', amplitudeM: 60 },
      clutterType: 'forest',
      weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 },
      ionosphere: { timeOfDay: 12, season: 'equinox', solarActivity: 'medium' },
      miscLossDb: 2,
    },
    constraints: { budget: 1200, minMarginDb: 3, maxDelayMs: 250, maxHops: 8, maxSimultaneousDeploy: 3, reserveFactor: 1.3, requireRedundancy: false, physics: 'auto', quality: 'fast', timeLimitMs: 60000, maxUtilizationPct: 85, crewSpeedKmh: 40 },
    weights: { cost: 0.4, delay: 0.3, survivability: 0.2, margin: 0.1 },
    oushTree: [],     // организационно-штатная структура (FR-RES-01/02)
    fleet: [],        // единицы техники
    objects: [],      // размещённые на карте объекты (FR-MAP-03)
    lines: [],        // линии связи (FR-MAP-06)
    demands: [],      // заявки на ресурсы
    selection: new Set(),
    view: { lat: 55.75, lng: 37.62, zoom: 11, ox: 0, oy: 0 },
    tool: 'select',
    layers: { base: true, coverage: true, links: true, rejected: false, labels: true, grid: true },
    lastResult: null,
    linkById: new Map(),
    nodeById: new Map(),
    task: null,
    log: [],
    seq: { obj: 0, line: 0, grp: 0 },
    dirty: true,
  };

  function invalidate() { state.dirty = true; }
  function nextId(prefix, counter) { state.seq[counter] = (state.seq[counter] || 0) + 1; return `${prefix}-${Date.now().toString(36).slice(-4)}${state.seq[counter].toString(36)}`; }

  function logMsg(level, msg, meta) {
    state.log.unshift({ t: new Date().toLocaleTimeString('ru-RU'), level, msg, meta: meta || null });
    if (state.log.length > 500) state.log.pop();
    if (typeof document !== 'undefined') renderLog();
  }

  /* ═══ §3–§9 реализуются только в браузере ══════════════════════════════ */
  const PURE = {
    lngToWorldX, worldXToLng, latToWorldY, worldYToLat, project, unproject, viewOffsets,
    metersPerPixel, scaleDenominator, haversineKm, centroid, round6, fitBounds,
    GEN, GEN_ZOOM, generalizationLevel, gridCluster, coverageRadiusKm,
    linkColorByMargin, linkWidthByCapacity, parseCsv, csvToScenario,
    buildRequest, applyPlanToState, BAND_BY_LINK_TYPE, LINK_TYPE_BY_BAND,
  };

  const EXPORT = {
    pure: PURE,
    version: '1.0.0',
    state,
    // тестовый шов (tests/unit/frontend_dom.test.mjs): загрузка сценария и
    // применение результата расчёта без DOM-событий. В промышленном коде не
    // используется — UI работает только через пользовательские обработчики.
    __testApplyScenario: typeof applyScenarioPayload === 'function' ? (d) => applyScenarioPayload(d) : null,
    __testApplyPlan: (r) => applyPlanToState(state, r),
  };
  if (typeof window !== 'undefined') {
    window.ENG6UI = EXPORT;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = EXPORT; // для unit-тестов в Node (без DOM)
    return;
  }

  /* ── дальше — только браузерная часть ───────────────────────────────── */
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const el = (tag, attrs, children) => {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') n.className = v;
      else if (k === 'html') n.innerHTML = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) n.setAttribute(k, v);
    }
    if (children) for (const c of [].concat(children)) if (c) n.appendChild(c);
    return n;
  };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const nf = (v, d = 1) => (v === null || v === undefined || !Number.isFinite(v) ? '—' : Number(v).toFixed(d));

  function toast(title, text, kind = 'info', ttl = 5000) {
    const t = el('div', { class: `toast ${kind}` }, [el('b', { text: title }), text ? el('span', { text }) : null]);
    $('#toasts').appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .25s'; setTimeout(() => t.remove(), 260); }, ttl);
  }

  /* ═══ §4. Тактические знаки ════════════════════════════════════════════ */

  const SIGN = {
    cp: { label: 'КП', color: '#58a6ff', shape: 'cp' },
    relay: { label: 'Ретранслятор', color: '#bc8cff', shape: 'relay' },
    radio: { label: 'Радиостанция', color: '#3fb950', shape: 'radio' },
    sat: { label: 'Спутниковая станция', color: '#f0883e', shape: 'sat' },
    observer: { label: 'Наблюдательный пост', color: '#00bcd4', shape: 'observer' },
    ew_jam: { label: 'Источник подавления (РЭБ)', color: '#c93545', shape: 'ew' },
    ew_rlr: { label: 'Радиолокационная разведка', color: '#e85d6f', shape: 'ew' },
    ew_rer: { label: 'Источник обнаружения (РЭР)', color: '#d29922', shape: 'ew' },
    nodeGroup: { label: 'Узел связи', color: '#e0e4ec', shape: 'nodeGroup' },
    unit: { label: 'Подразделение связи', color: '#9aa4b8', shape: 'cluster' },
  };
  const COND_COLOR = { green: '#3fb950', yellow: '#d29922', red: '#c93545', 'n/a': '#8892a4' };

  /**
   * Отрисовка тактического знака (упрощённые обозначения в духе
   * ГОСТ 21.205 / военных топографических стандартов — FR-MAP-05).
   * @param {CanvasRenderingContext2D} c
   */
  function drawSign(c, type, x, y, size, opts = {}) {
    const s = SIGN[type] || SIGN.radio;
    const sel = !!opts.selected;
    const cond = opts.condition || 'green';
    const r = size / 2;
    c.save();
    c.translate(x, y);
    if (opts.coverage) {
      c.beginPath(); c.arc(0, 0, opts.coverage, 0, Math.PI * 2);
      c.fillStyle = hexA(s.color, 0.07); c.fill();
      c.strokeStyle = hexA(s.color, 0.35); c.lineWidth = 1; c.setLineDash([4, 4]); c.stroke(); c.setLineDash([]);
    }
    if (sel) {
      c.beginPath(); c.arc(0, 0, r + 6, 0, Math.PI * 2);
      c.strokeStyle = '#58a6ff'; c.lineWidth = 2; c.setLineDash([3, 3]); c.stroke(); c.setLineDash([]);
    }
    c.lineWidth = 1.6;
    c.strokeStyle = opts.dim ? hexA(s.color, 0.5) : s.color;
    c.fillStyle = opts.dim ? 'rgba(20,24,32,0.75)' : 'rgba(18,22,30,0.92)';
    switch (s.shape) {
      case 'cp':
        rect(c, -r, -r * 0.72, r * 2, r * 1.44); c.fill(); c.stroke();
        line(c, -r * 0.45, -r * 0.72, -r * 0.45, r * 0.72); line(c, r * 0.45, -r * 0.72, r * 0.45, r * 0.72);
        c.fillStyle = s.color; c.font = `600 ${Math.max(7, r * 0.62)}px Segoe UI, sans-serif`; c.textAlign = 'center'; c.textBaseline = 'middle';
        c.fillText('КП', 0, 0.5);
        break;
      case 'relay':
        rect(c, -r * 0.8, -r * 0.6, r * 1.6, r * 1.2); c.fill(); c.stroke();
        line(c, 0, -r * 0.6, 0, -r * 1.25);
        c.beginPath(); c.moveTo(-r * 0.35, -r * 1.25); c.lineTo(r * 0.35, -r * 1.25); c.lineTo(0, -r * 0.72); c.closePath(); c.stroke();
        break;
      case 'radio':
        c.beginPath(); c.arc(0, 0, r * 0.42, 0, Math.PI * 2); c.fill(); c.stroke();
        for (let i = 1; i <= 2; i++) { c.beginPath(); c.arc(0, 0, r * (0.42 + i * 0.34), -Math.PI * 0.75, -Math.PI * 0.25); c.stroke(); }
        break;
      case 'sat':
        rect(c, -r * 0.42, -r * 0.42, r * 0.84, r * 0.84); c.fill(); c.stroke();
        line(c, -r * 1.05, 0, -r * 0.42, 0); line(c, r * 0.42, 0, r * 1.05, 0);
        c.beginPath(); c.ellipse(0, 0, r * 1.15, r * 0.5, -0.5, 0, Math.PI * 2); c.stroke();
        break;
      case 'observer':
        c.beginPath(); c.moveTo(0, -r * 0.85); c.lineTo(r * 0.85, r * 0.7); c.lineTo(-r * 0.85, r * 0.7); c.closePath(); c.fill(); c.stroke();
        c.beginPath(); c.arc(0, r * 0.1, r * 0.18, 0, Math.PI * 2); c.fillStyle = s.color; c.fill();
        break;
      case 'ew':
        rect(c, -r * 0.85, -r * 0.65, r * 1.7, r * 1.3); c.fill(); c.stroke();
        c.beginPath(); c.moveTo(r * 0.1, -r * 0.5); c.lineTo(-r * 0.3, r * 0.05); c.lineTo(r * 0.02, r * 0.05); c.lineTo(-r * 0.15, r * 0.55); c.lineTo(r * 0.35, -r * 0.08); c.lineTo(r * 0.02, -r * 0.08); c.closePath();
        c.fillStyle = s.color; c.fill();
        break;
      case 'nodeGroup': {
        // FR-MAP-05: единый знак «Узел связи» — рамка с молнией и числом средств
        rect(c, -r, -r * 0.78, r * 2, r * 1.56); c.fill(); c.stroke();
        c.beginPath(); c.moveTo(r * 0.18, -r * 0.55); c.lineTo(-r * 0.28, r * 0.06); c.lineTo(r * 0.02, r * 0.06); c.lineTo(-r * 0.18, r * 0.6); c.lineTo(r * 0.34, -r * 0.06); c.lineTo(r * 0.02, -r * 0.06); c.closePath();
        c.fillStyle = s.color; c.fill();
        if (opts.count > 1) {
          c.beginPath(); c.arc(r * 0.92, -r * 0.7, r * 0.4, 0, Math.PI * 2);
          c.fillStyle = '#1a1e2a'; c.fill(); c.strokeStyle = s.color; c.lineWidth = 1.2; c.stroke();
          c.fillStyle = s.color; c.font = `700 ${Math.max(7, r * 0.44)}px Segoe UI, sans-serif`; c.textAlign = 'center'; c.textBaseline = 'middle';
          c.fillText(String(opts.count), r * 0.92, -r * 0.68);
        }
        break;
      }
      case 'cluster': {
        roundRect(c, -r * 1.15, -r * 0.85, r * 2.3, r * 1.7, 5); c.fill(); c.stroke();
        c.fillStyle = s.color; c.font = `700 ${Math.max(9, r * 0.6)}px Segoe UI, sans-serif`; c.textAlign = 'center'; c.textBaseline = 'middle';
        c.fillText(opts.label || `${opts.count || 0}`, 0, -r * 0.1);
        c.font = `${Math.max(7, r * 0.36)}px Segoe UI, sans-serif`;
        c.fillStyle = 'rgba(224,228,236,0.75)';
        c.fillText(opts.sub || 'подразделение', 0, r * 0.42);
        break;
      }
      default:
        c.beginPath(); c.arc(0, 0, r * 0.6, 0, Math.PI * 2); c.fill(); c.stroke();
    }
    // индикатор состояния техники (FR-RES-03)
    if (opts.showCondition !== false && cond && cond !== 'n/a') {
      c.beginPath(); c.arc(r * 0.95, r * 0.75, Math.max(2.4, r * 0.19), 0, Math.PI * 2);
      c.fillStyle = COND_COLOR[cond] || COND_COLOR['n/a']; c.fill();
      c.strokeStyle = 'rgba(0,0,0,.55)'; c.lineWidth = 0.8; c.stroke();
    }
    // маркер результата расчёта (статус линии/узла по ядру)
    if (opts.badge) {
      c.beginPath(); c.arc(-r * 0.95, r * 0.75, Math.max(2.4, r * 0.19), 0, Math.PI * 2);
      c.fillStyle = opts.badge; c.fill(); c.strokeStyle = 'rgba(0,0,0,.55)'; c.lineWidth = 0.8; c.stroke();
    }
    c.restore();
  }
  function rect(c, x, y, w, h) { c.beginPath(); c.rect(x, y, w, h); }
  function roundRect(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y); c.lineTo(x + w - r, y); c.quadraticCurveTo(x + w, y, x + w, y + r);
    c.lineTo(x + w, y + h - r); c.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    c.lineTo(x + r, y + h); c.quadraticCurveTo(x, y + h, x, y + h - r);
    c.lineTo(x, y + r); c.quadraticCurveTo(x, y, x + r, y); c.closePath();
  }
  function line(c, x1, y1, x2, y2) { c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke(); }
  function hexA(hex, a) {
    const h = hex.replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  /* ═══ §5. Рендер карты (FR-MAP-01…08, NFR-01) ══════════════════════════ */
  let ctx2d = null, dpr = 1, vw = 0, vh = 0;
  let terrainCanvas = null, terrainKey = '';
  let fps = 0, frameCount = 0, fpsTs = Date.now();
  let drawStats = { signs: 0, links: 0, clusters: 0, ms: 0 };

  function resize() {
    const c = $('#map');
    dpr = Math.min(2, window.devicePixelRatio || 1);
    const r = c.getBoundingClientRect();
    vw = Math.max(1, Math.floor(r.width));
    vh = Math.max(1, Math.floor(r.height));
    c.width = Math.floor(vw * dpr);
    c.height = Math.floor(vh * dpr);
    ctx2d = c.getContext('2d', { alpha: false });
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    syncOffsets();
    invalidate();
  }

  function syncOffsets() {
    const off = viewOffsets(state.view.lat, state.view.lng, state.view.zoom, vw, vh);
    state.view.ox = off.ox;
    state.view.oy = off.oy;
  }

  function setView(lat, lng, zoom) {
    state.view.lat = lat;
    state.view.lng = lng;
    state.view.zoom = Math.max(2, Math.min(18, zoom));
    syncOffsets();
    invalidate();
  }

  function panBy(dxPx, dyPx) {
    state.view.ox -= dxPx;
    state.view.oy -= dyPx;
    const c = unproject(state.view, vw / 2, vh / 2);
    state.view.lat = c.lat;
    state.view.lng = c.lng;
    invalidate();
  }

  function zoomAt(factor, px, py) {
    const before = unproject(state.view, px, py);
    state.view.zoom = Math.max(2, Math.min(18, state.view.zoom + Math.log2(factor)));
    syncOffsets();
    const after = project(state.view, before.lat, before.lng);
    state.view.ox += after.x - px;
    state.view.oy += after.y - py;
    const c = unproject(state.view, vw / 2, vh / 2);
    state.view.lat = c.lat;
    state.view.lng = c.lng;
    invalidate();
  }

  /** Детерминированный value-noise (синтетический рельеф, без внешних тайлов). */
  function hash2(ix, iy, seed) {
    let h = (ix | 0) * 374761393 + (iy | 0) * 668265263 + (seed | 0) * 1442695040;
    h = (h ^ (h >>> 13)) * 1274126177;
    return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
  }
  function smooth(t) { return t * t * (3 - 2 * t); }
  function valueNoise(x, y, seed) {
    const ix = Math.floor(x), iy = Math.floor(y);
    const fx = smooth(x - ix), fy = smooth(y - iy);
    const a = hash2(ix, iy, seed), b = hash2(ix + 1, iy, seed), c = hash2(ix, iy + 1, seed), d = hash2(ix + 1, iy + 1, seed);
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy;
  }
  function fbm(x, y, seed, oct) {
    let v = 0, amp = 0.5, f = 1;
    for (let i = 0; i < (oct || 4); i++) { v += amp * valueNoise(x * f, y * f, seed + i * 17); amp *= 0.5; f *= 2; }
    return v;
  }

  const HYPSO = [[0.0, [22, 30, 40]], [0.3, [28, 40, 38]], [0.44, [36, 52, 40]], [0.56, [56, 68, 46]], [0.68, [84, 82, 54]], [0.82, [112, 98, 68]], [1.0, [146, 134, 118]]];
  function hypsometric(v) {
    const tint = { open: [1, 1, 0.95], forest: [0.88, 1, 0.9], urban: [1, 0.95, 0.94], dense_urban: [1, 0.92, 0.92] }[state.environment.clutterType] || [1, 1, 1];
    let c = HYPSO[0][1];
    for (let i = 0; i < HYPSO.length - 1; i++) {
      if (v >= HYPSO[i][0] && v <= HYPSO[i + 1][0]) {
        const t = (v - HYPSO[i][0]) / Math.max(1e-9, HYPSO[i + 1][0] - HYPSO[i][0]);
        c = [0, 1, 2].map((k) => HYPSO[i][1][k] + (HYPSO[i + 1][1][k] - HYPSO[i][1][k]) * t);
        break;
      }
    }
    return `rgb(${Math.round(c[0] * tint[0] * 0.66)},${Math.round(c[1] * tint[1] * 0.66)},${Math.round(c[2] * tint[2] * 0.66)})`;
  }

  /**
   * Кэш слоя рельефа: ключ = (округлённый zoom, ячейка центра, тип местности).
   * Рендер с шагом 4 px и последующим масштабированием — основа NFR-01.
   */
  function ensureTerrain() {
    if (!state.layers.base) { terrainCanvas = null; return; }
    const key = `${Math.round(state.view.zoom * 2)}|${Math.round(state.view.ox / 512)}|${Math.round(state.view.oy / 512)}|${state.environment.clutterType}|${state.environment.terrain.source}`;
    if (terrainCanvas && terrainKey === key) return;
    terrainKey = key;
    const step = 4;
    const w = Math.ceil(vw / step), h = Math.ceil(vh / step);
    if (!terrainCanvas) terrainCanvas = document.createElement('canvas');
    if (terrainCanvas.width !== w || terrainCanvas.height !== h) { terrainCanvas.width = w; terrainCanvas.height = h; }
    const tc = terrainCanvas.getContext('2d');
    const img = tc.createImageData(w, h);
    const d = img.data;
    const s = TILE * Math.pow(2, state.view.zoom);
    const flat = state.environment.terrain.source === 'flat';
    const f = 0.9 / Math.pow(2, state.view.zoom - 9);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const wx = (x * step + state.view.ox) / s;
        const wy = (y * step + state.view.oy) / s;
        const v = flat ? 0.34 : fbm(wx * 760 * f, wy * 760 * f, 1234, 4);
        const i = (y * w + x) * 4;
        const rgb = hypsometricRgb(v);
        d[i] = rgb[0]; d[i + 1] = rgb[1]; d[i + 2] = rgb[2]; d[i + 3] = 255;
      }
    }
    tc.putImageData(img, 0, 0);
  }
  function hypsometricRgb(v) {
    const tint = { open: [1, 1, 0.95], forest: [0.88, 1, 0.9], urban: [1, 0.95, 0.94], dense_urban: [1, 0.92, 0.92] }[state.environment.clutterType] || [1, 1, 1];
    let c = HYPSO[0][1];
    for (let i = 0; i < HYPSO.length - 1; i++) {
      if (v >= HYPSO[i][0] && v <= HYPSO[i + 1][0]) {
        const t = (v - HYPSO[i][0]) / Math.max(1e-9, HYPSO[i + 1][0] - HYPSO[i][0]);
        c = [0, 1, 2].map((k) => HYPSO[i][1][k] + (HYPSO[i + 1][1][k] - HYPSO[i][1][k]) * t);
        break;
      }
    }
    return [Math.round(c[0] * tint[0] * 0.66), Math.round(c[1] * tint[1] * 0.66), Math.round(c[2] * tint[2] * 0.66)];
  }

  function drawTerrain() {
    if (!state.layers.base) { ctx2d.fillStyle = '#0a0c11'; ctx2d.fillRect(0, 0, vw, vh); return; }
    ensureTerrain();
    if (!terrainCanvas) return;
    ctx2d.imageSmoothingEnabled = true;
    ctx2d.drawImage(terrainCanvas, 0, 0, vw, vh);
    // горизонтали (шаг зависит от амплитуды рельефа и масштаба)
    const amp = state.environment.terrain.amplitudeM || 60;
    if (amp > 0 && state.view.zoom >= 8) {
      ctx2d.save();
      ctx2d.globalAlpha = 0.22;
      ctx2d.strokeStyle = '#8a7a5c';
      ctx2d.lineWidth = 0.7;
      const stepPx = 26;
      const s = TILE * Math.pow(2, state.view.zoom);
      for (let y = 0; y < vh; y += stepPx) {
        ctx2d.beginPath();
        for (let x = 0; x <= vw; x += 8) {
          const wx = (x + state.view.ox) / s, wy = (y + state.view.oy) / s;
          const v = fbm(wx * 760 * (0.9 / Math.pow(2, state.view.zoom - 9)), wy * 760 * (0.9 / Math.pow(2, state.view.zoom - 9)), 1234, 3);
          const yy = y + Math.sin(v * 12) * 6;
          if (x === 0) ctx2d.moveTo(x, yy); else ctx2d.lineTo(x, yy);
        }
        ctx2d.stroke();
      }
      ctx2d.restore();
    }
  }

  function drawGrid() {
    if (!state.layers.grid) return;
    const z = state.view.zoom;
    const stepDeg = z < 5 ? 10 : z < 8 ? 5 : z < 11 ? 1 : z < 14 ? 0.25 : 0.05;
    const tl = unproject(state.view, 0, 0), br = unproject(state.view, vw, vh);
    ctx2d.save();
    ctx2d.strokeStyle = 'rgba(150,170,200,0.15)';
    ctx2d.fillStyle = 'rgba(170,190,220,0.5)';
    ctx2d.lineWidth = 1;
    ctx2d.font = '9px ui-monospace, Consolas, monospace';
    for (let lng = Math.floor(tl.lng / stepDeg) * stepDeg; lng <= br.lng; lng += stepDeg) {
      const p = project(state.view, (tl.lat + br.lat) / 2, lng);
      ctx2d.beginPath(); ctx2d.moveTo(Math.round(p.x) + 0.5, 0); ctx2d.lineTo(Math.round(p.x) + 0.5, vh); ctx2d.stroke();
      ctx2d.fillText(`${lng.toFixed(stepDeg < 1 ? 2 : 0)}°`, p.x + 2, vh - 6);
    }
    for (let lat = Math.floor(br.lat / stepDeg) * stepDeg; lat <= tl.lat; lat += stepDeg) {
      const p = project(state.view, lat, tl.lng);
      ctx2d.beginPath(); ctx2d.moveTo(0, Math.round(p.y) + 0.5); ctx2d.lineTo(vw, Math.round(p.y) + 0.5); ctx2d.stroke();
      ctx2d.fillText(`${lat.toFixed(stepDeg < 1 ? 2 : 0)}°`, 3, p.y - 2);
    }
    ctx2d.restore();
  }

  function drawScaleBar() {
    const mpp = metersPerPixel(state.view.lat, state.view.zoom);
    const meters = mpp * 110;
    const pow = Math.pow(10, Math.floor(Math.log10(Math.max(1, meters))));
    const nice = [1, 2, 5, 10].map((m) => m * pow).find((m) => m >= meters) || pow * 10;
    const px = nice / mpp;
    const label = nice >= 1000 ? `${nf(nice / 1000, nice % 1000 === 0 ? 0 : 1)} км` : `${Math.round(nice)} м`;
    $('#scaleBar').innerHTML = `<div>${label}</div><div class="bar" style="width:${Math.round(px)}px"></div><div style="margin-top:2px">1:${Math.round(scaleDenominator(state.view.lat, state.view.zoom) / 1000)} тыс.</div>`;
  }

  /* ═══ §6. Объектная модель карты и рендер знаков ═══════════════════════ */

  /**
   * Видимые объекты с учётом генерализации (FR-MAP-07).
   * Возвращает список «знаков к отрисовке»: отдельные объекты, узлы связи
   * или кластеры подразделений — каждый с экранными координатами.
   */
  function computeSigns() {
    const level = generalizationLevel(state.view.zoom);
    const items = [];
    const groups = new Map();
    for (const o of state.objects) {
      const p = project(state.view, o.lat, o.lng);
      if (p.x < -80 || p.x > vw + 80 || p.y < -80 || p.y > vh + 80) continue;
      o._sx = p.x; o._sy = p.y;
      if (level === GEN.DETAIL) {
        if (o.kind === 'asset' && o.groupId) {
          // на крупном масштабе показываются и отдельные машины узла связи
          items.push({ kind: 'object', obj: o, x: p.x, y: p.y, size: 20, type: o.nodeType });
        } else {
          items.push({ kind: 'object', obj: o, x: p.x, y: p.y, size: o.kind === 'nodeGroup' ? 26 : 20, type: o.kind === 'nodeGroup' ? 'nodeGroup' : o.nodeType });
        }
        continue;
      }
      if (level === GEN.NODE) {
        // FR-MAP-07 (средний масштаб): машины узла связи сливаются в один знак
        if (o.kind === 'asset' && o.groupId) {
          const g = state.objects.find((x) => x.id === o.groupId);
          if (g) { if (!groups.has(g.id)) groups.set(g.id, g); continue; }
        }
        items.push({ kind: 'object', obj: o, x: p.x, y: p.y, size: o.kind === 'nodeGroup' ? 26 : 20, type: o.kind === 'nodeGroup' ? 'nodeGroup' : o.nodeType });
        continue;
      }
      // UNIT: всё сливается в узлы, затем узлы одного подразделения — в кластер
      if (o.kind === 'asset' && o.groupId) {
        const g = state.objects.find((x) => x.id === o.groupId);
        if (g) { if (!groups.has(g.id)) groups.set(g.id, g); continue; }
      }
      items.push({ kind: 'object', obj: o, x: p.x, y: p.y, size: 22, type: o.kind === 'nodeGroup' ? 'nodeGroup' : o.nodeType, count: 1 });
    }
    for (const g of groups.values()) {
      const p = project(state.view, g.lat, g.lng);
      const members = state.objects.filter((x) => x.groupId === g.id);
      items.push({ kind: 'object', obj: g, x: p.x, y: p.y, size: 26, type: 'nodeGroup', count: members.length + 1 });
    }
    if (level !== GEN.UNIT) return { items, clusters: [], level };

    // кластеризация близких узлов одного подразделения (FR-MAP-07, мелкий масштаб)
    const keyed = items.map((it) => ({ ...it, groupKey: it.obj.parentId || it.obj.unitSub || 'root' }));
    const clusters = gridCluster(keyed, 78).filter((c) => c.items.length > 1);
    const clusteredObjs = new Set();
    for (const c of clusters) for (const it of c.items) clusteredObjs.add(it.obj.id);
    return {
      level,
      clusters: clusters.map((c) => ({ key: c.key, x: c.x, y: c.y, size: 30, count: c.items.length, items: c.items, groupKey: c.groupKey, label: `${c.items.length}`, sub: (c.groupKey || 'подразделение').toString().slice(0, 18) })),
      items: items.filter((it) => !clusteredObjs.has(it.obj.id)),
    };
  }

  function drawLinks() {
    if (!state.layers.links) return;
    const c = ctx2d;
    const res = state.lastResult;
    const planLinks = res && res.plan ? res.plan.activeLinks || [] : [];
    const rejected = res && res.plan ? res.plan.rejectedLinks || [] : [];
    const byId = new Map(planLinks.map((l) => [l.id, l]));
    const rejById = new Map(rejected.map((l) => [l.id, l]));
    c.save();
    c.lineCap = 'round';
    // 1) линии топологии (созданные пользователем)
    for (const l of state.lines) {
      const a = objById(l.from), b = objById(l.to);
      if (!a || !b) continue;
      const pa = project(state.view, a.lat, a.lng), pb = project(state.view, b.lat, b.lng);
      if (Math.max(pa.x, pb.x) < -50 || Math.min(pa.x, pb.x) > vw + 50) continue;
      if (Math.max(pa.y, pb.y) < -50 || Math.min(pa.y, pb.y) > vh + 50) continue;
      const pl = byId.get(l.id);
      const rj = rejById.get(l.id);
      const sel = state.selection.has(l.id);
      if (rj && !state.layers.rejected) continue;
      const margin = pl ? pl.marginDb : null;
      const color = pl ? linkColorByMargin(margin) : rj ? linkColorByMargin(null, { rejected: true }) : '#5a6478';
      c.strokeStyle = sel ? '#58a6ff' : color;
      c.globalAlpha = rj ? 0.55 : 0.95;
      c.lineWidth = pl ? linkWidthByCapacity(pl.capacityKbps) + (sel ? 1.2 : 0) : 1.4;
      c.setLineDash(rj ? [5, 4] : l.isBackup ? [9, 5] : []);
      c.beginPath(); c.moveTo(pa.x, pa.y); c.lineTo(pb.x, pb.y); c.stroke();
      c.setLineDash([]);
      // подписи качества (FR-INT-03: цветные линии качества связи)
      if (state.layers.labels && state.view.zoom >= 10 && (pl || rj)) {
        const mx = (pa.x + pb.x) / 2, my = (pa.y + pb.y) / 2;
        const txt = pl ? `${pl.type} · ${nf(pl.marginDb, 1)} дБ · ${nf(pl.utilPct, 0)}%` : `${rj.type} · ${rj.code}`;
        c.font = '9.5px ui-monospace, Consolas, monospace';
        const w = c.measureText(txt).width + 8;
        c.globalAlpha = 0.92;
        c.fillStyle = 'rgba(13,15,20,0.86)';
        roundRect(c, mx - w / 2, my - 8, w, 15, 4); c.fill();
        c.strokeStyle = hexA(color.startsWith('#') ? color : '#8892a4', 0.6); c.lineWidth = 0.8; c.stroke();
        c.fillStyle = color.startsWith('#') ? color : '#c93545';
        c.textAlign = 'center'; c.textBaseline = 'middle';
        c.fillText(txt, mx, my + 0.5);
      }
    }
    // 2) маршруты заявок (поверх линий)
    if (res && res.plan && res.plan.routes) {
      const selDemand = state.selectedDemand;
      for (const r of res.plan.routes) {
        if (selDemand && r.demandId !== selDemand) continue;
        if (!selDemand && r.status !== 'ROUTED' && r.status !== 'PARTIAL') continue;
        const path = r.path || [];
        if (path.length < 2) continue;
        c.strokeStyle = selDemand ? '#00e5ff' : r.status === 'ROUTED' ? 'rgba(88,166,255,0.55)' : 'rgba(210,153,34,0.5)';
        c.lineWidth = selDemand ? 3 : 1.6;
        c.setLineDash(selDemand ? [] : [2, 4]);
        c.beginPath();
        let started = false;
        for (const id of path) {
          const o = objById(id);
          if (!o) continue;
          const p = project(state.view, o.lat, o.lng);
          if (!started) { c.moveTo(p.x, p.y); started = true; } else c.lineTo(p.x, p.y);
        }
        c.stroke();
        c.setLineDash([]);
        if (r.backupPath && r.backupPath.nodes) {
          c.strokeStyle = 'rgba(63,185,80,0.4)'; c.lineWidth = 1.4; c.setLineDash([7, 4]);
          c.beginPath(); let st2 = false;
          for (const id of r.backupPath.nodes) {
            const o = objById(id); if (!o) continue;
            const p = project(state.view, o.lat, o.lng);
            if (!st2) { c.moveTo(p.x, p.y); st2 = true; } else c.lineTo(p.x, p.y);
          }
          c.stroke(); c.setLineDash([]);
        }
      }
    }
    c.restore();
    drawStats.links = state.lines.length;
  }

  function drawCoverage() {
    if (!state.layers.coverage) return;
    const c = ctx2d;
    const mpp = metersPerPixel(state.view.lat, state.view.zoom);
    c.save();
    for (const o of state.objects) {
      const pl = state.linkById && state.linkById.size ? null : null;
      void pl;
      const links = (state.lastResult?.plan?.activeLinks || []).filter((l) => l.from === o.id || l.to === o.id);
      const ownLines = links.length ? links : state.lines.filter((l) => l.from === o.id || l.to === o.id).map((l) => ({ distKm: distOfLine(l), from: l.from, to: l.to }));
      if (!ownLines.length) continue;
      const rKm = coverageRadiusKm({ id: o.id, lat: o.lat, lng: o.lng }, ownLines.map((l) => ({ from: l.from, to: l.to, distKm: l.distKm })));
      const rPx = (rKm * 1000) / mpp;
      if (rPx < 3 || rPx > vw * 3) continue;
      const p = project(state.view, o.lat, o.lng);
      c.beginPath(); c.arc(p.x, p.y, rPx, 0, Math.PI * 2);
      const col = (SIGN[o.kind === 'nodeGroup' ? 'nodeGroup' : o.nodeType] || SIGN.radio).color;
      c.fillStyle = hexA(col, 0.055); c.fill();
      c.strokeStyle = hexA(col, 0.28); c.lineWidth = 1; c.setLineDash([3, 5]); c.stroke(); c.setLineDash([]);
    }
    c.restore();
  }
  function distOfLine(l) {
    const a = objById(l.from), b = objById(l.to);
    return a && b ? haversineKm(a, b) : 0;
  }
  function objById(id) { return state.objects.find((o) => o.id === id); }

  function drawSigns(signs) {
    const c = ctx2d;
    let count = 0;
    c.save();
    for (const s of signs.items) {
      const o = s.obj;
      const sel = state.selection.has(o.id);
      const plan = state.lastResult?.plan;
      let badge = null;
      if (plan) {
        const dep = (plan.deployedUnits || []).find((d) => d.nodeId === o.id);
        if (dep) badge = '#3fb950';
        else if (plan.rejectedLinks && (plan.rejectedLinks || []).some((r) => r.from === o.id || r.to === o.id)) badge = '#c93545';
      }
      drawSign(c, s.type, s.x, s.y, s.size, {
        selected: sel,
        condition: o.condition || 'n/a',
        count: s.count || 1,
        badge,
        coverage: false,
        dim: o.condition === 'red',
      });
      if (state.layers.labels && state.view.zoom >= 10.5) {
        c.font = '10px Segoe UI, sans-serif';
        c.textAlign = 'center';
        c.fillStyle = 'rgba(224,228,236,0.92)';
        c.strokeStyle = 'rgba(10,12,17,0.9)';
        c.lineWidth = 2.5;
        const t = (o.name || o.id).slice(0, 22);
        c.strokeText(t, s.x, s.y + s.size * 0.62 + 10);
        c.fillText(t, s.x, s.y + s.size * 0.62 + 10);
      }
      count++;
    }
    for (const cl of signs.clusters) {
      drawSign(c, 'unit', cl.x, cl.y, cl.size, { count: cl.count, label: cl.label, sub: cl.sub, selected: cl.items.some((it) => state.selection.has(it.obj.id)) });
      count++;
    }
    c.restore();
    drawStats.signs = count;
    drawStats.clusters = signs.clusters.length;
  }

  function drawInteraction() {
    const c = ctx2d;
    if (state.pendingLink) {
      const a = objById(state.pendingLink.from);
      if (a) {
        const pa = project(state.view, a.lat, a.lng);
        c.save();
        c.strokeStyle = '#58a6ff'; c.lineWidth = 1.6; c.setLineDash([6, 4]);
        c.beginPath(); c.moveTo(pa.x, pa.y); c.lineTo(state.mouse.x, state.mouse.y); c.stroke();
        c.setLineDash([]);
        const d = haversineKm(a, unproject(state.view, state.mouse.x, state.mouse.y));
        c.font = '10px ui-monospace, Consolas, monospace';
        c.fillStyle = '#58a6ff';
        c.fillText(`${nf(d, 2)} км`, (pa.x + state.mouse.x) / 2 + 6, (pa.y + state.mouse.y) / 2 - 6);
        c.restore();
      }
    }
    if (state.rubber) {
      const r = state.rubber;
      c.save();
      c.strokeStyle = 'rgba(88,166,255,0.9)'; c.fillStyle = 'rgba(88,166,255,0.12)'; c.lineWidth = 1;
      const x = Math.min(r.x0, r.x1), y = Math.min(r.y0, r.y1), w = Math.abs(r.x1 - r.x0), h = Math.abs(r.y1 - r.y0);
      c.fillRect(x, y, w, h); c.strokeRect(x + 0.5, y + 0.5, w, h);
      c.restore();
    }
    if (state.measure && state.measure.points.length) {
      c.save();
      c.strokeStyle = '#00bcd4'; c.lineWidth = 1.6; c.setLineDash([4, 3]);
      c.beginPath();
      state.measure.points.forEach((p, i) => { const s = project(state.view, p.lat, p.lng); if (i === 0) c.moveTo(s.x, s.y); else c.lineTo(s.x, s.y); });
      if (state.measure.points.length) { c.lineTo(state.mouse.x, state.mouse.y); }
      c.stroke(); c.setLineDash([]);
      let total = 0;
      for (let i = 1; i < state.measure.points.length; i++) total += haversineKm(state.measure.points[i - 1], state.measure.points[i]);
      const last = state.measure.points[state.measure.points.length - 1];
      if (last) total += haversineKm(last, unproject(state.view, state.mouse.x, state.mouse.y));
      c.font = '10.5px ui-monospace, Consolas, monospace'; c.fillStyle = '#00bcd4';
      c.fillText(`Σ ${nf(total, 2)} км`, state.mouse.x + 10, state.mouse.y - 8);
      c.restore();
    }
    if (state.dragPreview) {
      const d = state.dragPreview;
      c.save();
      c.globalAlpha = 0.85;
      drawSign(c, d.type, d.x, d.y, 22, { condition: d.condition, selected: true });
      c.restore();
    }
  }

  function render() {
    if (!ctx2d) return;
    const t0 = performance.now();
    drawTerrain();
    drawGrid();
    drawCoverage();
    drawLinks();
    const signs = computeSigns();
    state._signs = signs;
    drawSigns(signs);
    drawInteraction();
    drawStats.ms = performance.now() - t0;
    $('#sbGen').textContent = `генерализация: ${GEN_LABEL[signs.level]} · знаков ${drawStats.signs} · кластеров ${drawStats.clusters}`;
  }
  const GEN_LABEL = { detail: 'крупный масштаб (все единицы)', node: 'средний (узлы связи слиты)', unit: 'мелкий (кластеры подразделений)' };

  function loop() {
    requestAnimationFrame(loop);
    if (state.dirty) { state.dirty = false; render(); }
    frameCount++;
    const now = Date.now();
    if (now - fpsTs >= 500) {
      fps = Math.round((frameCount * 1000) / (now - fpsTs));
      frameCount = 0; fpsTs = now;
      const chip = $('#perfChip');
      const ok = fps >= 30;
      chip.textContent = `${fps} FPS · ${drawStats.signs} зн. · ${nf(drawStats.ms, 1)} мс`;
      chip.className = `chip ${ok ? 'chip-ok' : 'chip-warn'}`;
      chip.title = `NFR-01: ≥30 FPS при 1000 знаков. Текущий кадр: ${nf(drawStats.ms, 2)} мс`;
    }
  }

  /* ═══ §7. Панель ОШС и ресурсы (FR-RES-01…04), drag-and-drop (FR-MAP-01) ═ */

  const BAND_BY_NODE_TYPE = { cp: 'УКВ', relay: 'УКВ', radio: 'УКВ', sat: 'СПУТНИК', observer: 'УКВ', ew_jam: 'УКВ', ew_rlr: 'РТ', ew_rer: 'КВ' };

  function unitBand(u) {
    if (u.band) return u.band;
    const k = (u.kind || '').toUpperCase();
    if (k.includes('Р-438') || k.includes('БЕЛОЗЁР') || k.includes('СПУТНИК')) return 'СПУТНИК';
    if (k.includes('Р-419') || k.includes('РРЛ') || k.includes('Р-121')) return 'РТ';
    if (k.includes('ВОЛС') || k.includes('КАБЕЛЬ')) return 'ВОЛС';
    if (k.includes('Р-168') || k.includes('Р-187') || k.includes('УКВ')) return 'УКВ';
    if (k.includes('Р-161') || k.includes('КВ')) return 'КВ';
    return BAND_BY_NODE_TYPE[u.nodeType] || 'УКВ';
  }

  function filteredFleet() {
    const q = ($('#resSearch').value || '').trim().toLowerCase();
    const band = $('#fltBand').value;
    const cond = $('#fltCond').value;
    return state.fleet.filter((u) => {
      if (band && unitBand(u) !== band) return false;
      if (cond && u.condition !== cond) return false;
      if (!q) return true;
      const hay = `${u.unitId} ${u.kind} ${u.nodeType} ${u.subunit || ''} ${u.note || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function renderTree() {
    const host = $('#oshTree');
    const visible = new Set(filteredFleet().map((u) => u.unitId));
    const hasFilter = visible.size !== state.fleet.length;
    host.innerHTML = '';
    let shown = 0;
    const build = (node, depth) => {
      if (node.type === 'vehicle') {
        if (hasFilter && !visible.has(node.unit.unitId)) return null;
        shown++;
        return vehicleRow(node, depth);
      }
      const kids = (node.children || []).map((k) => build(k, depth + 1)).filter(Boolean);
      if (hasFilter && !kids.length) return null;
      const wrap = el('div', { class: 'tnode' });
      const open = state._treeOpen ? state._treeOpen.get(node.id) !== false : depth < 2;
      const row = el('div', { class: 'trow' }, [
        el('span', { class: `tcaret ${open ? 'open' : ''}`, text: '▶' }),
        el('span', { class: 'ticon', text: node.type === 'unit' ? '▣' : '◈' }),
        el('span', { class: 'tname', text: node.name }),
        el('span', { class: 'tmeta', text: node.type === 'unit' ? `${countVehicles(node)} ед.` : '' }),
      ]);
      row.addEventListener('click', () => {
        state._treeOpen = state._treeOpen || new Map();
        state._treeOpen.set(node.id, !open);
        renderTree();
      });
      row.addEventListener('contextmenu', (ev) => { ev.preventDefault(); showOrgMenu(ev, node); });
      wrap.appendChild(row);
      if (open && kids.length) wrap.appendChild(el('div', { class: 'tkids' }, kids));
      return wrap;
    };
    for (const root of state.oushTree) {
      const n = build(root, 0);
      if (n) host.appendChild(n);
    }
    if (!host.children.length) host.appendChild(el('div', { class: 'tgroup-hd', text: 'Ничего не найдено по фильтру' }));
    const ready = state.fleet.filter((u) => u.condition === 'green').length;
    const placed = state.objects.filter((o) => o.kind === 'asset' && o.unitId).length;
    $('#oshCount').textContent = `показано ${shown} · в парке ${state.fleet.length} (🟢 ${ready}) · размещено ${placed}`;
  }
  function countVehicles(node) {
    if (node.type === 'vehicle') return 1;
    return (node.children || []).reduce((s, c) => s + countVehicles(c), 0);
  }

  function vehicleRow(node, depth) {
    const u = node.unit;
    const placed = state.objects.find((o) => o.unitId === u.unitId);
    const row = el('div', { class: `trow ${placed ? 'selected' : ''} ${u.condition === 'red' ? 'unavailable' : ''}`, draggable: u.condition === 'red' ? 'false' : 'true' }, [
      el('span', { class: 'tcaret leaf', text: '▶' }),
      el('span', { class: 'tcond c-' + u.condition, title: COND_TITLE[u.condition] }),
      el('span', { class: 'ticon', text: SIGN[u.nodeType] ? '▸' : '▸' }),
      el('span', { class: 'tname', text: `${u.kind}` , title: `${u.unitId} · ${u.kind}`}),
      el('span', { class: 'tmeta', text: u.unitId.slice(-5) }),
    ]);
    row.dataset.unit = u.unitId;
    row.addEventListener('click', () => {
      if (placed) { selectOnly(placed.id); flyTo(placed); }
      else openUnitCard(u);
    });
    row.addEventListener('contextmenu', (ev) => { ev.preventDefault(); showUnitMenu(ev, u, placed); });
    if (u.condition !== 'red') {
      row.addEventListener('dragstart', (ev) => {
        state.dragUnit = u;
        ev.dataTransfer.setData('text/plain', u.unitId);
        ev.dataTransfer.effectAllowed = 'copy';
        const ghost = $('#dragGhost');
        ghost.innerHTML = '';
        const cv = document.createElement('canvas');
        cv.width = 26; cv.height = 26;
        drawSign(cv.getContext('2d'), u.nodeType, 13, 13, 20, { condition: u.condition, showCondition: false });
        ghost.appendChild(cv);
        ghost.appendChild(el('span', { text: `${u.kind}` }));
        ghost.classList.remove('hidden');
        try { ev.dataTransfer.setDragImage(cv, 13, 13); } catch {}
        row.classList.add('dragging');
        logMsg('info', `Перетаскивание: ${u.unitId} (${u.kind})`);
      });
      row.addEventListener('dragend', () => { row.classList.remove('dragging'); $('#dragGhost').classList.add('hidden'); state.dragUnit = null; state.dragPreview = null; invalidate(); });
    } else {
      row.title = '🔴 Неисправна / в ремонте — размещение запрещено (FR-RES-03)';
    }
    void depth;
    return row;
  }
  const COND_TITLE = { green: '🟢 Исправна / готова к применению', yellow: '🟡 Ограниченно пригодна (требуется ЗИП)', red: '🔴 Неисправна / в ремонте' };

  /* ── Размещение объекта на карте (FR-MAP-03) ─────────────────────────── */
  function placeUnit(unit, lat, lng, opts = {}) {
    if (unit.condition === 'red' && !opts.force) {
      toast('Размещение запрещено', `${unit.unitId}: техника неисправна (🔴). ${unit.note || 'Требуется ремонт.'}`, 'err');
      logMsg('err', `Отказ размещения ${unit.unitId}: состояние red (FR-RES-03)`);
      return null;
    }
    const existing = state.objects.find((o) => o.unitId === unit.unitId);
    if (existing) {
      existing.lat = round6(lat); existing.lng = round6(lng);
      logMsg('warn', `${unit.unitId}: объект уже размещён — координаты обновлены (FR-MAP-03)`);
      afterTopologyChange('move');
      return existing;
    }
    const spec = specForUnit(unit);
    const o = {
      id: nextId('obj', 'obj'),
      kind: 'asset',
      nodeType: unit.nodeType,
      name: unit.shortName || unit.kind,
      lat: round6(lat), lng: round6(lng),
      unitId: unit.unitId,
      condition: unit.condition,
      parentId: unit.parentId || null,
      unitSub: unit.subunit || null,
      spec,
      equipment: spec.equipment || [],
      crew: crewForUnit(unit),
      techState: techStateForUnit(unit),
      status: 'active',
      placedAt: new Date().toISOString(),
    };
    state.objects.push(o);
    state.nodeById.set(o.id, o);
    logMsg('ok', `Размещено: ${unit.kind} → ${o.lat.toFixed(6)}, ${o.lng.toFixed(6)}`);
    afterTopologyChange('place');
    return o;
  }

  function specForUnit(u) {
    const base = {
      cp: { txPowerDbm: 43, txGainDbi: 6, rxGainDbi: 6, rxSensDbm: -105, antHeightM: 12, freqMhz: 150, feederLossDb: 1.5, noiseFigureDb: 6, mobility: 'vehicle' },
      relay: { txPowerDbm: 40, txGainDbi: 9, rxGainDbi: 9, rxSensDbm: -107, antHeightM: 15, freqMhz: 150, feederLossDb: 1.5, noiseFigureDb: 6, mobility: 'vehicle' },
      radio: { txPowerDbm: 36, txGainDbi: 2, rxGainDbi: 2, rxSensDbm: -100, antHeightM: 3, freqMhz: 150, feederLossDb: 0.5, noiseFigureDb: 8, mobility: 'portable' },
      sat: { txPowerDbm: 40, txGainDbi: 38, rxGainDbi: 38, rxSensDbm: -118, antHeightM: 2, freqMhz: 12000, feederLossDb: 2, noiseFigureDb: 1.2, mobility: 'vehicle' },
      observer: { txPowerDbm: 30, txGainDbi: 2, rxGainDbi: 2, rxSensDbm: -100, antHeightM: 3, freqMhz: 450, feederLossDb: 0.5, noiseFigureDb: 8, mobility: 'portable' },
      ew_jam: { txPowerDbm: 50, txGainDbi: 12, rxGainDbi: 6, rxSensDbm: -95, antHeightM: 6, freqMhz: 300, feederLossDb: 2, noiseFigureDb: 8, mobility: 'vehicle' },
      ew_rlr: { txPowerDbm: 20, txGainDbi: 20, rxGainDbi: 20, rxSensDbm: -110, antHeightM: 8, freqMhz: 3000, feederLossDb: 2, noiseFigureDb: 4, mobility: 'vehicle' },
      ew_rer: { txPowerDbm: 10, txGainDbi: 8, rxGainDbi: 8, rxSensDbm: -115, antHeightM: 5, freqMhz: 150, feederLossDb: 1, noiseFigureDb: 4, mobility: 'portable' },
    }[u.nodeType] || { txPowerDbm: 36, txGainDbi: 2, rxGainDbi: 2, rxSensDbm: -100, antHeightM: 3, freqMhz: 150, feederLossDb: 0.5, noiseFigureDb: 8, mobility: 'static' };
    const deg = u.condition === 'yellow' ? 3 : 0;
    return {
      ...base,
      txPowerDbm: base.txPowerDbm - deg,
      nodeType: u.nodeType,
      deployTimeMin: u.deployTimeMin,
      costDeploy: u.costDeploy,
      band: unitBand(u),
      equipment: equipmentForUnit(u),
    };
  }

  function equipmentForUnit(u) {
    const rnd = mulberry(strHash(u.unitId));
    const n = 2 + Math.floor(rnd() * 3);
    const kinds = {
      cp: ['Радиостанция Р-168-100КЭ', 'Аппаратная Т-230НЧ', 'ДГУ-6Л', 'Комплекс П-166', 'Навигатор ГЛОНАСС/GPS'],
      relay: ['Ретранслятор Р-419Л1', 'Антенна АШ-50', 'Выносная опора 12 м', 'Блок питания БП-30', 'Модем Р-016'],
      radio: ['Радиостанция Р-168-100К', 'Антенна штыревая', 'Аккумулятор 12 В', 'Гарнитура МВЖ', 'Комплект ЗИП-О'],
      sat: ['Станция Р-438Н «Белозёр»', 'Антенна зеркальная 1,2 м', 'Модем спутниковый', 'ГНСС-приёмник', 'ДГУ-4'],
      observer: ['Комплекс 1Л261', 'Дальномер лазерный', 'Биноколь Б8', 'Радиостанция Р-187П1', 'Планшет цифровой'],
      ew_jam: ['Станция помех РБ-341В', 'Антенное устройство', 'ДГУ-10', 'Комплекс управления', 'ЗИП-Г'],
      ew_rlr: ['РЛС 1Л260', 'Антенный пост', 'Вычислитель', 'ДГУ-8', 'Кабельный комплект'],
      ew_rer: ['Приёмник Р-381Т1', 'Антенна АРК', 'Панорама частот', 'ДГУ-4', 'ЗИП-О'],
    }[u.nodeType] || ['Аппаратура связи', 'Источник питания', 'Антенное устройство', 'Комплект ЗИП', 'Кабельный комплект'];
    const out = [];
    for (let i = 0; i < n; i++) {
      const kind = kinds[i % kinds.length];
      out.push({
        name: kind,
        inv: `${u.unitId.slice(-5)}-${String(i + 1).padStart(3, '0')}`,
        type: i === 0 ? 'основное' : i % 3 === 0 ? 'резервное' : 'вспомогательное',
        ttx: [
          { p: 'Диапазон частот', v: `${Math.round(30 + rnd() * 500)}–${Math.round(600 + rnd() * 2000)} МГц` },
          { p: 'Мощность передатчика', v: `${Math.round(1 + rnd() * 99)} Вт` },
          { p: 'Дальность связи', v: `${Math.round(8 + rnd() * 60)} км` },
          { p: 'Потребляемая мощность', v: `${Math.round(80 + rnd() * 900)} Вт` },
          { p: 'Масса', v: `${nf(10 + rnd() * 240, 0)} кг` },
        ],
      });
    }
    return out;
  }

  const RANKS = ['рядовой', 'ефрейтор', 'мл. сержант', 'сержант', 'ст. сержант', 'старшина', 'прапорщик', 'мл. лейтенант', 'лейтенант', 'ст. лейтенант', 'капитан'];
  const SURNAMES = ['Иванов', 'Петров', 'Сидоров', 'Кузнецов', 'Смирнов', 'Васильев', 'Соколов', 'Михайлов', 'Новиков', 'Фёдоров', 'Морозов', 'Волков', 'Алексеев', 'Лебедев', 'Семёнов', 'Егоров', 'Павлов', 'Козлов', 'Степанов', 'Николаев'];
  const NAMES = ['А.', 'В.', 'Д.', 'И.', 'К.', 'М.', 'Н.', 'О.', 'П.', 'Р.', 'С.', 'Ю.'];
  const POSTS = { cp: ['Командир взвода', 'Начальник связи', 'Оператор КП', 'Электромеханик', 'Водитель-электрик'], relay: ['Начальник направления', 'Электромеханик', 'Оператор', 'Водитель'], radio: ['Радиотелеграфист', 'Оператор', 'Командир отделения', 'Стрелок-радист'], sat: ['Начальник станции', 'Оператор', 'Инженер', 'Водитель'], observer: ['Наблюдатель', 'Дальномерщик', 'Оператор', 'Разведчик'], ew_jam: ['Начальник расчёта', 'Оператор РЭБ', 'Электромеханик', 'Водитель'], ew_rlr: ['Оператор РЛР', 'Начальник станции', 'Электромеханик', 'Водитель'], ew_rer: ['Оператор РЭР', 'Аналитик', 'Электромеханик', 'Водитель'] };

  function crewForUnit(u) {
    const rnd = mulberry(strHash(u.unitId + 'crew'));
    const posts = POSTS[u.nodeType] || ['Специалист', 'Оператор', 'Водитель'];
    const n = 2 + Math.floor(rnd() * Math.min(4, posts.length));
    const out = [];
    for (let i = 0; i < n; i++) {
      const sur = SURNAMES[Math.floor(rnd() * SURNAMES.length)];
      out.push({
        fio: `${sur} ${NAMES[Math.floor(rnd() * NAMES.length)]}${NAMES[Math.floor(rnd() * NAMES.length)]}`,
        rank: RANKS[Math.min(RANKS.length - 1, Math.floor(rnd() * (i === 0 ? RANKS.length : 6)))],
        post: posts[i % posts.length],
        phone: `+7 (9${Math.floor(10 + rnd() * 89)}) ${String(Math.floor(rnd() * 999)).padStart(3, '0')}-${String(Math.floor(rnd() * 99)).padStart(2, '0')}-${String(Math.floor(rnd() * 99)).padStart(2, '0')}`,
        callSign: `${(u.unitId || 'XX').slice(-3)}-${i + 1}`,
      });
    }
    return out;
  }

  function techStateForUnit(u) {
    const rnd = mulberry(strHash(u.unitId + 'tech'));
    const wear = u.condition === 'green' ? Math.round(rnd() * 18) : u.condition === 'yellow' ? 20 + Math.round(rnd() * 45) : 65 + Math.round(rnd() * 30);
    const faults = [];
    if (u.condition !== 'green') {
      faults.push({ date: daysAgo(Math.round(1 + rnd() * 40)), text: u.note || (u.condition === 'yellow' ? 'Требуется ЗИП: отказ блока питания' : 'Отказ приёмопередатчика, выведен из строя'), status: u.condition === 'yellow' ? 'частично устранено' : 'не устранено' });
      if (rnd() < 0.4) faults.push({ date: daysAgo(Math.round(1 + rnd() * 90)), text: 'Замена кабельной сборки, ТО-2', status: 'устранено' });
    } else if (rnd() < 0.5) faults.push({ date: daysAgo(Math.round(5 + rnd() * 180)), text: 'Плановое ТО-1', status: 'устранено' });
    return {
      condition: u.condition,
      wearPct: wear,
      zip: u.condition === 'green' ? 'полный комплект' : u.condition === 'yellow' ? 'частичный (отсутствуют 2 позиции)' : 'отсутствует',
      lastMaintenance: daysAgo(Math.round(1 + rnd() * 200)),
      nextMaintenance: daysAgo(-Math.round(10 + rnd() * 170)),
      runTimeH: Math.round(100 + rnd() * 4000),
      faults,
      resource: u.resource || 'штатный',
    };
  }
  function daysAgo(n) { const d = new Date(Date.now() - n * 86400000); return d.toISOString().slice(0, 10); }
  function strHash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function mulberry(seed) { let t = seed >>> 0; return () => { t = (t + 0x6d2b79f5) >>> 0; let x = Math.imul(t ^ (t >>> 15), 1 | t); x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x; return ((x ^ (x >>> 14)) >>> 0) / 4294967296; }; }

  function afterTopologyChange(kind) {
    // FR-INT-02: при любом изменении топологии формируем пакет и отправляем в ядро
    invalidate();
    renderTree();
    updateStatusbar();
    state.autoRecalc = state.autoRecalc || { timer: null };
    if (!state.autoCalcEnabled) return;
    clearTimeout(state.autoRecalc.timer);
    state.autoRecalc.timer = setTimeout(() => { runCalc({ silent: true, reason: kind }); }, 900);
  }

  /* ═══ §8. Взаимодействие с картой (FR-MAP-01…08, FR-CTX-01/02) ═════════ */

  state.mouse = { x: 0, y: 0, lat: 0, lng: 0 };
  state.pendingLink = null;
  state.rubber = null;
  state.measure = { points: [] };
  state.dragPreview = null;
  state.dragObj = null;

  function hitTest(x, y) {
    const signs = state._signs;
    if (!signs) return null;
    for (const cl of signs.clusters) {
      if (Math.hypot(cl.x - x, cl.y - y) <= cl.size * 0.9) return { kind: 'cluster', cluster: cl };
    }
    let best = null, bestD = 1e9;
    for (const s of signs.items) {
      const d = Math.hypot(s.x - x, s.y - y);
      if (d <= Math.max(13, s.size * 0.72) && d < bestD) { bestD = d; best = { kind: 'object', obj: s.obj, sign: s }; }
    }
    if (best) return best;
    // линии
    for (const l of state.lines) {
      const a = objById(l.from), b = objById(l.to);
      if (!a || !b) continue;
      const pa = project(state.view, a.lat, a.lng), pb = project(state.view, b.lat, b.lng);
      const d = distToSegment(x, y, pa.x, pa.y, pb.x, pb.y);
      if (d <= 6 && d < bestD) { bestD = d; best = { kind: 'line', line: l }; }
    }
    return best;
  }
  function distToSegment(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1, dy = y2 - y1;
    const l2 = dx * dx + dy * dy;
    if (l2 === 0) return Math.hypot(px - x1, py - y1);
    let t = ((px - x1) * dx + (py - y1) * dy) / l2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  function selectOnly(id) { state.selection.clear(); if (id) state.selection.add(id); updateStatusbar(); invalidate(); }
  function toggleSelect(id) { if (state.selection.has(id)) state.selection.delete(id); else state.selection.add(id); updateStatusbar(); invalidate(); }
  function flyTo(o) {
    if (!o) return;
    const z = Math.max(state.view.zoom, 13);
    setView(o.lat, o.lng, z);
  }

  function updateStatusbar() {
    $('#sbSel').textContent = `выделено: ${state.selection.size}`;
    $('#sbZoom').textContent = `z ${nf(state.view.zoom, 2)}`;
  }

  function bindMapEvents() {
    const c = $('#map');
    let panning = null;
    c.addEventListener('mousemove', (ev) => {
      const r = c.getBoundingClientRect();
      const x = ev.clientX - r.left, y = ev.clientY - r.top;
      state.mouse.x = x; state.mouse.y = y;
      const ll = unproject(state.view, x, y);
      state.mouse.lat = ll.lat; state.mouse.lng = ll.lng;
      $('#sbCoords').textContent = `${ll.lat.toFixed(6)}, ${ll.lng.toFixed(6)} (WGS-84)`;
      if (panning) { panBy(ev.movementX, ev.movementY); return; }
      if (state.dragObj) {
        const o = state.dragObj;
        o.lat = round6(ll.lat); o.lng = round6(ll.lng);
        invalidate();
        return;
      }
      if (state.dragUnit) {
        state.dragPreview = { x, y, type: state.dragUnit.nodeType, condition: state.dragUnit.condition };
        const gh = $('#dragGhost');
        gh.style.left = `${ev.clientX}px`; gh.style.top = `${ev.clientY}px`;
        const inside = x >= 0 && y >= 0 && x <= vw && y <= vh;
        gh.classList.toggle('drop-ok', inside);
        gh.classList.toggle('drop-bad', !inside);
        invalidate();
        return;
      }
      if (state.rubber) { state.rubber.x1 = x; state.rubber.y1 = y; invalidate(); }
      if (state.tool === 'measure' || state.tool === 'link') invalidate();
    });
    c.addEventListener('mousedown', (ev) => {
      if (ev.button === 1 || (ev.button === 0 && ev.altKey)) { panning = true; c.classList.add('dragging'); ev.preventDefault(); return; }
      if (ev.button !== 0) return;
      const r = c.getBoundingClientRect();
      const x = ev.clientX - r.left, y = ev.clientY - r.top;
      const hit = hitTest(x, y);
      if (state.tool === 'select') {
        if (hit && hit.kind === 'object') {
          if (ev.shiftKey) toggleSelect(hit.obj.id);
          else if (!state.selection.has(hit.obj.id)) selectOnly(hit.obj.id);
          state.dragObj = hit.obj;
          state.dragStart = { lat: hit.obj.lat, lng: hit.obj.lng, x, y };
          c.classList.add('dragging');
        } else if (hit && hit.kind === 'cluster') {
          // FR-MAP-08: клик по генерализованному знаку — список или зум
          clusterAction(hit.cluster, ev);
        } else if (hit && hit.kind === 'line') {
          selectOnly(hit.line.id);
          state.selectedDemand = null;
        } else {
          if (!ev.shiftKey) state.selection.clear();
          state.rubber = { x0: x, y0: y, x1: x, y1: y };
          updateStatusbar();
        }
      } else if (state.tool === 'link') {
        if (hit && hit.kind === 'object') {
          if (!state.pendingLink) { state.pendingLink = { from: hit.obj.id }; setHint('Линия связи: выберите второй объект (ESC — отмена)'); }
          else { createLine(state.pendingLink.from, hit.obj.id); state.pendingLink = null; setHint(''); }
        } else setHint('Линия создаётся между объектами — выберите узел/станцию');
      } else if (state.tool === 'measure') {
        state.measure.points.push({ lat: round6(state.mouse.lat), lng: round6(state.mouse.lng) });
      } else if (state.tool === 'node') {
        if (state.selection.size >= 2) makeNodeGroup([...state.selection]);
        else setHint('Выделите ≥2 объектов (рамкой или с Shift), затем нажмите «Узел связи»');
      }
      invalidate();
    });
    window.addEventListener('mouseup', (ev) => {
      if (panning) { panning = null; c.classList.remove('dragging'); }
      if (state.dragObj) {
        const o = state.dragObj;
        const moved = state.dragStart && (Math.abs(o.lat - state.dragStart.lat) > 1e-9 || Math.abs(o.lng - state.dragStart.lng) > 1e-9);
        state.dragObj = null;
        c.classList.remove('dragging');
        if (moved) {
          // FR-MAP-03/FR-INT-01: координаты обновлены → фиксируем изменение
          logMsg('ok', `Перемещено: ${o.name || o.id} → ${o.lat.toFixed(6)}, ${o.lng.toFixed(6)}`);
          afterTopologyChange('move');
        }
      }
      if (state.rubber) {
        const r = state.rubber;
        const x0 = Math.min(r.x0, r.x1), x1 = Math.max(r.x0, r.x1), y0 = Math.min(r.y0, r.y1), y1 = Math.max(r.y0, r.y1);
        if (Math.abs(x1 - x0) > 3 || Math.abs(y1 - y0) > 3) {
          if (!ev.shiftKey) state.selection.clear();
          for (const o of state.objects) {
            const p = project(state.view, o.lat, o.lng);
            if (p.x >= x0 && p.x <= x1 && p.y >= y0 && p.y <= y1) state.selection.add(o.id);
          }
          updateStatusbar();
        }
        state.rubber = null;
        invalidate();
      }
      if (state.dragPreview) { state.dragPreview = null; invalidate(); }
    });
    c.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const r = c.getBoundingClientRect();
      zoomAt(ev.deltaY < 0 ? 1.18 : 1 / 1.18, ev.clientX - r.left, ev.clientY - r.top);
      drawScaleBar();
      updateStatusbar();
    }, { passive: false });
    c.addEventListener('dblclick', (ev) => {
      const r = c.getBoundingClientRect();
      const hit = hitTest(ev.clientX - r.left, ev.clientY - r.top);
      if (hit && hit.kind === 'object') openObjectCard(hit.obj);
      else if (hit && hit.kind === 'cluster') clusterAction(hit.cluster, ev, true);
      else if (hit && hit.kind === 'line') openLineCard(hit.line);
    });
    c.addEventListener('contextmenu', (ev) => {
      ev.preventDefault();
      const r = c.getBoundingClientRect();
      const hit = hitTest(ev.clientX - r.left, ev.clientY - r.top);
      if (hit && hit.kind === 'object') { if (!state.selection.has(hit.obj.id)) selectOnly(hit.obj.id); showObjectContext(ev, hit.obj); }
      else if (hit && hit.kind === 'line') { selectOnly(hit.line.id); showLineContext(ev, hit.line); }
      else if (hit && hit.kind === 'cluster') showClusterContext(ev, hit.cluster);
      else showMapContext(ev);
    });
    // Drag-and-drop из панели ОШС (FR-MAP-01…03)
    c.addEventListener('dragover', (ev) => {
      if (!state.dragUnit) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = state.dragUnit.condition === 'red' ? 'none' : 'copy';
      const r = c.getBoundingClientRect();
      state.dragPreview = { x: ev.clientX - r.left, y: ev.clientY - r.top, type: state.dragUnit.nodeType, condition: state.dragUnit.condition };
      const gh = $('#dragGhost');
      gh.style.left = `${ev.clientX}px`; gh.style.top = `${ev.clientY}px`;
      invalidate();
    });
    c.addEventListener('dragleave', () => { state.dragPreview = null; invalidate(); });
    c.addEventListener('drop', (ev) => {
      ev.preventDefault();
      const u = state.dragUnit;
      state.dragPreview = null;
      $('#dragGhost').classList.add('hidden');
      if (!u) return;
      const r = c.getBoundingClientRect();
      const ll = unproject(state.view, ev.clientX - r.left, ev.clientY - r.top);
      const o = placeUnit(u, ll.lat, ll.lng);
      if (o) { selectOnly(o.id); renderTree(); }
      state.dragUnit = null;
      invalidate();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.target && /input|select|textarea/i.test(ev.target.tagName)) return;
      const k = ev.key.toLowerCase();
      if (k === 'escape') { state.pendingLink = null; state.measure.points = []; hideCtx(); closeModal(); setHint(''); invalidate(); }
      else if (k === 'v') setTool('select');
      else if (k === 'l') setTool('link');
      else if (k === 'm') { setTool('measure'); state.measure.points = []; }
      else if (k === 'n') setTool('node');
      else if (k === 'delete' || k === 'backspace') { deleteSelected(); ev.preventDefault(); }
      else if (k === 'f') fitToObjects();
      else if (k === 'enter' && state.selection.size) { const o = objById([...state.selection][0]); if (o) openObjectCard(o); }
      else if (k === 'r' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); runCalc({}); }
      else if (k === 'a' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); state.selection = new Set(state.objects.map((o) => o.id)); updateStatusbar(); invalidate(); }
    });
  }

  function setHint(t) { const h = $('#hint'); h.textContent = t; h.classList.toggle('show', !!t); }
  function setTool(t) {
    state.tool = t;
    $$('.tb-btn.tool').forEach((b) => b.classList.toggle('active', b.dataset.tool === t));
    const c = $('#map');
    c.className = `tool-${t}`;
    if (t !== 'link') { state.pendingLink = null; }
    if (t !== 'measure') { state.measure.points = []; }
    setHint(t === 'link' ? 'Линия связи: клик по первому объекту, затем по второму (FR-MAP-06)' : t === 'measure' ? 'Измерение: клики по карте, ESC — сброс' : t === 'node' ? 'Узел связи: выделите ≥2 объектов и нажмите «Узел связи» (FR-MAP-04)' : '');
    invalidate();
  }

  function createLine(fromId, toId) {
    if (fromId === toId) { toast('Линия не создана', 'Начальный и конечный объекты должны отличаться', 'warn'); return; }
    const dup = state.lines.find((l) => (l.from === fromId && l.to === toId) || (l.from === toId && l.to === fromId));
    if (dup) { toast('Линия уже существует', `${dup.id}: ${dup.from} ↔ ${dup.to} (${dup.type})`, 'warn'); return; }
    const a = objById(fromId), b = objById(toId);
    const dKm = haversineKm(a, b);
    const type = dKm <= 12 ? 'FOC' : dKm <= 25 ? 'VHF' : dKm <= 45 ? 'UHF' : dKm <= 90 ? 'RRL' : 'SAT';
    const l = { id: nextId('ln', 'line'), from: fromId, to: toId, type, priority: 2, isBackup: false, distKm: round6(dKm) };
    state.lines.push(l);
    logMsg('ok', `Линия связи ${l.id}: ${a.name || a.id} ↔ ${b.name || b.id}, ${nf(dKm, 2)} км, тип ${l.type}`);
    afterTopologyChange('line');
  }

  /** FR-MAP-04: группирование нескольких аппаратных в единый «Узел связи». */
  function makeNodeGroup(ids) {
    const members = ids.map((id) => objById(id)).filter(Boolean).filter((o) => o.kind === 'asset');
    if (members.length < 2) { toast('Недостаточно объектов', 'Для узла связи нужно ≥2 единиц техники', 'warn'); return; }
    const c = centroid(members);
    const main = members.slice().sort((a, b) => ({ cp: 0, relay: 1, sat: 2, radio: 3 }[a.nodeType] ?? 9) - ({ cp: 0, relay: 1, sat: 2, radio: 3 }[b.nodeType] ?? 9))[0];
    const g = {
      id: nextId('us', 'grp'), kind: 'nodeGroup', nodeType: main.nodeType,
      name: `Узел связи ${state.seq.grp}`, lat: c.lat, lng: c.lng,
      parentId: main.parentId || null, unitSub: main.unitSub || null,
      condition: members.some((m) => m.condition === 'red') ? 'yellow' : members.some((m) => m.condition === 'yellow') ? 'yellow' : 'green',
      spec: main.spec, status: 'active', isGroup: true,
    };
    state.objects.push(g);
    for (const m of members) m.groupId = g.id;
    // линии между участниками становятся внутренними — переписываем на узел
    for (const l of state.lines) {
      if (ids.includes(l.from) && ids.includes(l.to)) l.internal = true;
      if (ids.includes(l.from)) l.from = g.id;
      if (ids.includes(l.to)) l.to = g.id;
    }
    state.lines = state.lines.filter((l) => !l.internal && l.from !== l.to);
    state.selection = new Set([g.id]);
    logMsg('ok', `Сформирован узел связи ${g.id} из ${members.length} ед. техники (FR-MAP-04)`);
    toast('Узел связи создан', `${g.name}: ${members.length} ед., координаты ${c.lat}, ${c.lng}`, 'ok');
    afterTopologyChange('group');
  }

  function ungroup(gid) {
    const g = objById(gid);
    if (!g || g.kind !== 'nodeGroup') return;
    for (const o of state.objects) if (o.groupId === gid) o.groupId = null;
    for (const l of state.lines) { if (l.from === gid) l.from = gid; if (l.to === gid) l.to = gid; }
    state.objects = state.objects.filter((o) => o.id !== gid);
    state.selection.delete(gid);
    logMsg('warn', `Узел связи ${gid} расформирован`);
    afterTopologyChange('ungroup');
  }

  function deleteSelected() {
    if (!state.selection.size) return;
    const ids = [...state.selection];
    for (const id of ids) {
      const o = objById(id);
      if (o) {
        for (const m of state.objects) if (m.groupId === id) m.groupId = null;
        state.objects = state.objects.filter((x) => x.id !== id);
        state.lines = state.lines.filter((l) => l.from !== id && l.to !== id);
        logMsg('warn', `Удалён объект ${o.name || o.id}`);
      } else {
        const l = state.lines.find((x) => x.id === id);
        if (l) { state.lines = state.lines.filter((x) => x.id !== id); logMsg('warn', `Удалена линия ${id}`); }
      }
    }
    state.selection.clear();
    updateStatusbar();
    afterTopologyChange('delete');
  }

  function clusterAction(cl, ev, force) {
    // FR-MAP-08: клик по генерализованному знаку — список вложенных объектов
    // либо автоматический зум до уровня раздельного отображения
    if (force || cl.items.length <= 6) {
      showClusterList(cl, ev.clientX, ev.clientY);
      return;
    }
    const pts = cl.items.map((it) => ({ lat: it.obj.lat, lng: it.obj.lng }));
    const f = fitBounds(pts, vw, vh, { minZoom: 8, maxZoom: 15, padding: 0.25 });
    setView(f.lat, f.lng, Math.max(f.zoom, GEN_ZOOM.detail));
    toast('Генерализация раскрыта', `Кластер из ${cl.items.length} объектов — масштаб увеличен до раздельного отображения (FR-MAP-08)`, 'info', 3200);
  }

  function fitToObjects() {
    if (!state.objects.length) { setView(55.75, 37.62, 10); return; }
    const f = fitBounds(state.objects.map((o) => ({ lat: o.lat, lng: o.lng })), vw, vh, { padding: 0.14 });
    setView(f.lat, f.lng, f.zoom);
    drawScaleBar();
  }

  /* ═══ §9. Контекстные меню (FR-CTX-01) и карточка объекта (FR-CTX-02) ══ */

  function hideCtx() { $('#ctxmenu').classList.add('hidden'); }
  function showCtx(x, y, title, items) {
    const m = $('#ctxmenu');
    m.innerHTML = '';
    if (title) m.appendChild(el('div', { class: 'ctx-hd', text: title }));
    for (const it of items) {
      if (it === '-') { m.appendChild(el('div', { class: 'ctx-sep' })); continue; }
      const n = el('div', { class: `ctx-i ${it.disabled ? 'dis' : ''}`, html: `<span>${it.icon || ''}</span><span>${esc(it.label)}</span>${it.key ? `<span class="ctx-k">${esc(it.key)}</span>` : ''}` });
      n.addEventListener('click', () => { hideCtx(); if (!it.disabled && it.action) it.action(); });
      m.appendChild(n);
    }
    m.classList.remove('hidden');
    const r = m.getBoundingClientRect();
    m.style.left = `${Math.min(x, window.innerWidth - r.width - 8)}px`;
    m.style.top = `${Math.min(y, window.innerHeight - r.height - 8)}px`;
  }
  document.addEventListener('click', (ev) => { if (!ev.target.closest('#ctxmenu')) hideCtx(); });
  window.addEventListener('blur', hideCtx);

  function showObjectContext(ev, o) {
    const res = state.lastResult;
    const dep = res?.plan?.deployedUnits?.find((d) => d.nodeId === o.id);
    const routesHere = res?.plan?.routes?.filter((r) => (r.path || []).includes(o.id)) || [];
    showCtx(ev.clientX, ev.clientY, `${o.kind === 'nodeGroup' ? 'Узел связи' : 'Единица техники'} · ${o.name || o.id}`, [
      { icon: '▤', label: 'Свойства / Детализация…', key: 'Enter', action: () => openObjectCard(o) },
      { icon: '✎', label: 'Изменить параметры…', action: () => openObjectCard(o, 1) },
      { icon: '⌖', label: 'Центрировать на карте', key: 'F', action: () => flyTo(o) },
      '-',
      { icon: '✎', label: 'Провести линию отсюда…', key: 'L', action: () => { setTool('link'); state.pendingLink = { from: o.id }; setHint('Выберите второй объект для линии связи'); } },
      { icon: '⌘', label: 'Сгруппировать выделенное в узел связи', disabled: state.selection.size < 2, action: () => makeNodeGroup([...state.selection]) },
      { icon: '⊘', label: 'Расформировать узел связи', disabled: o.kind !== 'nodeGroup', action: () => ungroup(o.id) },
      '-',
      { icon: dep ? '🟢' : '⚪', label: dep ? `Развернуто ядром (волна ${dep.wave ?? '—'}, t=${nf(dep.startMin, 0)} мин)` : 'Не входит в план развёртывания', disabled: true },
      { icon: '⇄', label: `Через объект маршрутов: ${routesHere.length}`, disabled: true },
      '-',
      { icon: '🗑', label: 'Удалить с карты', key: 'Del', action: () => { selectOnly(o.id); deleteSelected(); } },
    ]);
  }

  function showLineContext(ev, l) {
    const res = state.lastResult;
    const pl = res?.plan?.activeLinks?.find((x) => x.id === l.id);
    const rj = res?.plan?.rejectedLinks?.find((x) => x.id === l.id);
    showCtx(ev.clientX, ev.clientY, `Линия связи · ${l.id}`, [
      { icon: '▤', label: 'Свойства линии…', action: () => openLineCard(l) },
      { icon: '⇄', label: pl ? `Активна: ${nf(pl.marginDb, 1)} дБ · ${nf(pl.capacityKbps, 0)} кбит/с · загрузка ${nf(pl.utilPct, 0)}%` : rj ? `Отклонена ядром: ${rj.code} (${rj.reason})` : 'Не рассчитана', disabled: true },
      '-',
      { icon: '★', label: l.isBackup ? 'Снять признак резервной' : 'Пометить как резервную', action: () => { l.isBackup = !l.isBackup; afterTopologyChange('line-flag'); } },
      { icon: '⚡', label: 'Изменить тип линии…', action: () => openLineCard(l, 1) },
      '-',
      { icon: '🗑', label: 'Удалить линию', key: 'Del', action: () => { state.lines = state.lines.filter((x) => x.id !== l.id); state.selection.delete(l.id); afterTopologyChange('delete'); } },
    ]);
  }

  function showClusterContext(ev, cl) {
    showCtx(ev.clientX, ev.clientY, `Подразделение связи · ${cl.count} объект(ов)`, [
      { icon: '▦', label: 'Показать список вложенных объектов', action: () => showClusterList(cl, ev.clientX, ev.clientY) },
      { icon: '⌖', label: 'Раскрыть (увеличить масштаб)', key: 'FR-MAP-08', action: () => clusterAction(cl, ev, true) },
      '-',
      { icon: '☑', label: 'Выделить все объекты кластера', action: () => { for (const it of cl.items) state.selection.add(it.obj.id); updateStatusbar(); invalidate(); } },
      { icon: '⌘', label: 'Сформировать узел связи из кластера', disabled: cl.items.length < 2, action: () => makeNodeGroup(cl.items.map((it) => it.obj.id)) },
    ]);
  }

  function showClusterList(cl, x, y) {
    const items = cl.items.map((it) => ({
      icon: it.type === 'nodeGroup' ? '⌘' : '▸',
      label: `${it.obj.name || it.obj.id}${it.count > 1 ? ` (${it.count} ед.)` : ''}`,
      action: () => { selectOnly(it.obj.id); setView(it.obj.lat, it.obj.lng, Math.max(state.view.zoom, GEN_ZOOM.detail + 1)); },
    }));
    showCtx(x, y, `Вложенные объекты (${cl.items.length})`, items);
  }

  function showMapContext(ev) {
    const ll = unproject(state.view, ev.clientX - $('#map').getBoundingClientRect().left, ev.clientY - $('#map').getBoundingClientRect().top);
    showCtx(ev.clientX, ev.clientY, `Карта · ${ll.lat.toFixed(6)}, ${ll.lng.toFixed(6)}`, [
      { icon: '＋', label: 'Разместить технику в этой точке…', action: () => openPlacer(ll) },
      { icon: '⌖', label: 'Центрировать карту здесь', action: () => setView(ll.lat, ll.lng, state.view.zoom) },
      { icon: '↔', label: 'Начать измерение отсюда', key: 'M', action: () => { setTool('measure'); state.measure.points = [{ lat: round6(ll.lat), lng: round6(ll.lng) }]; } },
      '-',
      { icon: '⤢', label: 'Вписать все объекты', key: 'F', action: fitToObjects },
      { icon: '▦', label: 'Слои и легенда…', action: () => { $('#layerCtl').classList.toggle('hidden'); } },
      { icon: '⚙', label: 'Параметры расчёта (ограничения, веса)…', action: () => openConstraints() },
      '-',
      { icon: '▶', label: 'Запустить расчёт ENG6-PLAN', key: 'Ctrl+R', action: () => runCalc({}) },
    ]);
  }

  function showUnitMenu(ev, u, placed) {
    showCtx(ev.clientX, ev.clientY, `${u.unitId} · ${u.kind}`, [
      { icon: '▤', label: 'Карточка единицы техники…', action: () => openUnitCard(u) },
      { icon: '＋', label: 'Разместить в центре карты', disabled: u.condition === 'red', action: () => { const c = unproject(state.view, vw / 2, vh / 2); const o = placeUnit(u, c.lat, c.lng); if (o) { selectOnly(o.id); flyTo(o); } } },
      { icon: '⌖', label: 'Показать на карте', disabled: !placed, action: () => { selectOnly(placed.id); flyTo(placed); } },
      '-',
      { icon: '🟢', label: 'Состояние: исправна', disabled: u.condition === 'green', action: () => setCondition(u, 'green') },
      { icon: '🟡', label: 'Состояние: ограниченно пригодна', disabled: u.condition === 'yellow', action: () => setCondition(u, 'yellow') },
      { icon: '🔴', label: 'Состояние: неисправна', disabled: u.condition === 'red', action: () => setCondition(u, 'red') },
      '-',
      { icon: '🗑', label: placed ? 'Снять с карты' : 'Не размещена', disabled: !placed, action: () => { state.objects = state.objects.filter((o) => o.unitId !== u.unitId); afterTopologyChange('remove'); } },
    ]);
  }

  function showOrgMenu(ev, node) {
    const vehicles = [];
    const walk = (n) => { if (n.type === 'vehicle') vehicles.push(n.unit); (n.children || []).forEach(walk); };
    walk(node);
    showCtx(ev.clientX, ev.clientY, `${node.name} (${vehicles.length} ед.)`, [
      { icon: '⌗', label: 'Разместить все исправные по штатным позициям', action: () => autoPlace(vehicles) },
      { icon: '☑', label: 'Выделить размещённые объекты', action: () => { state.selection.clear(); for (const o of state.objects) if (vehicles.some((v) => v.unitId === o.unitId)) state.selection.add(o.id); updateStatusbar(); invalidate(); } },
      '-',
      { icon: '▦', label: 'Показать список техники…', action: () => showCtx(ev.clientX, ev.clientY, node.name, vehicles.slice(0, 24).map((u) => ({ icon: u.condition === 'green' ? '🟢' : u.condition === 'yellow' ? '🟡' : '🔴', label: `${u.kind} · ${u.unitId}`, action: () => openUnitCard(u) }))) },
    ]);
  }

  function setCondition(u, cond) {
    u.condition = cond;
    const o = state.objects.find((x) => x.unitId === u.unitId);
    if (o) {
      o.condition = cond;
      o.techState = techStateForUnit(u);
      if (cond === 'red') {
        o.status = 'failed';
        toast('Техника переведена в 🔴', `${u.unitId}: объект помечен «неисправен» — ядро исключит его из расчёта (FR-RES-03)`, 'warn');
      } else o.status = 'active';
    }
    logMsg('info', `${u.unitId}: состояние → ${cond}`);
    renderTree();
    afterTopologyChange('condition');
  }

  /** Авторазмещение: штатные позиции вокруг точки сбора подразделения. */
  function autoPlace(units) {
    const list = (units || state.fleet).filter((u) => u.condition !== 'red');
    if (!list.length) { toast('Нечего размещать', 'В парке нет исправной техники', 'warn'); return; }
    const c = unproject(state.view, vw / 2, vh / 2);
    const placed = [];
    const cols = Math.ceil(Math.sqrt(list.length));
    const stepKm = 2.2;
    list.forEach((u, i) => {
      if (state.objects.some((o) => o.unitId === u.unitId)) return;
      const row = Math.floor(i / cols), col = i % cols;
      const p = { lat: c.lat + (row - cols / 2) * (stepKm / 111.32), lng: c.lng + (col - cols / 2) * (stepKm / (111.32 * Math.cos(c.lat * DEG))) };
      const o = placeUnit(u, p.lat, p.lng, { quiet: true });
      if (o) placed.push(o);
    });
    if (placed.length) { fitToObjects(); toast('Авторазмещение выполнено', `Размещено единиц: ${placed.length}`, 'ok'); }
  }

  /* ── Карточка объекта: 3 вкладки (FR-CTX-02) ────────────────────────── */
  let modalCtx = null;
  function openModal(title, tabs, initial, foot) {
    modalCtx = { tabs, current: initial || 0 };
    $('#modalTitle').textContent = title;
    const th = $('#modalTabs');
    th.innerHTML = '';
    tabs.forEach((t, i) => {
      const b = el('button', { class: i === modalCtx.current ? 'active' : '', text: t.label, onclick: () => { modalCtx.current = i; openModal(title, tabs, i, foot); } });
      th.appendChild(b);
    });
    $('#modalBody').innerHTML = '';
    $('#modalBody').appendChild(tabs[modalCtx.current].render());
    const f = $('#modalFoot');
    f.innerHTML = '';
    if (foot) for (const n of foot) f.appendChild(n);
    $('#modal').classList.remove('hidden');
  }
  function closeModal() { $('#modal').classList.add('hidden'); modalCtx = null; }

  function openObjectCard(o, tabIdx = 0) {
    const unit = o.unitId ? state.fleet.find((u) => u.unitId === o.unitId) : null;
    const res = state.lastResult;
    const dep = res?.plan?.deployedUnits?.find((d) => d.nodeId === o.id);
    const pl = o.kind === 'nodeGroup' ? null : null;
    void pl;
    const tabs = [
      { label: 'Состав оборудования', render: () => equipmentTab(o, unit) },
      { label: 'Личный состав', render: () => crewTab(o) },
      { label: 'Техническое состояние', render: () => techTab(o, unit) },
      { label: 'Расчёт ENG6', render: () => planTab(o, dep, res) },
    ];
    const foot = [
      el('span', { class: 'spacer', text: `${o.id} · ${o.lat.toFixed(6)}, ${o.lng.toFixed(6)} (WGS-84, 6 знаков — NFR-03)` }),
      el('button', { class: 'tb-btn', text: 'Параметры…', onclick: () => openParams(o) }),
      el('button', { class: 'tb-btn', text: 'Центрировать', onclick: () => { flyTo(o); closeModal(); } }),
      el('button', { class: 'tb-btn accent', text: 'Закрыть', onclick: closeModal }),
    ];
    openModal(`${o.kind === 'nodeGroup' ? 'Узел связи' : 'Единица техники'} — ${o.name || o.id}`, tabs, tabIdx, foot);
  }

  function equipmentTab(o, unit) {
    const items = o.equipment && o.equipment.length ? o.equipment : (unit ? equipmentForUnit(unit) : []);
    const wrap = el('div');
    wrap.appendChild(el('div', { class: 'card', html: `<h3>Общие сведения</h3><dl class="kv">
      <dt>Идентификатор объекта</dt><dd>${esc(o.id)}</dd>
      <dt>Тип</dt><dd>${esc((SIGN[o.nodeType] || {}).label || o.nodeType)}</dd>
      <dt>Инвентарный номер (единица техники)</dt><dd>${esc(o.unitId || '—')}</dd>
      <dt>Подразделение</dt><dd>${esc(o.unitSub || o.parentId || '—')}</dd>
      <dt>Координаты</dt><dd>${o.lat.toFixed(6)}, ${o.lng.toFixed(6)}</dd>
      <dt>Размещено</dt><dd>${esc(o.placedAt ? new Date(o.placedAt).toLocaleString('ru-RU') : '—')}</dd>
    </dl>` }));
    if (!items.length) { wrap.appendChild(el('div', { class: 'card', html: '<h3>Аппаратура</h3><div style="color:var(--dim)">Состав оборудования не задан</div>' })); return wrap; }
    const tbl = el('table', { class: 'tbl' });
    tbl.appendChild(el('thead', { html: '<tr><th>Наименование</th><th>Инв. №</th><th>Назначение</th><th>ТТХ</th></tr>' }));
    const tb = el('tbody');
    for (const it of items) {
      tb.appendChild(el('tr', { html: `<td>${esc(it.name)}</td><td>${esc(it.inv)}</td><td>${esc(it.type)}</td><td style="font-family:inherit;font-size:10.5px;color:var(--dim)">${(it.ttx || []).map((t) => `${esc(t.p)}: <b style="color:var(--text);font-weight:600">${esc(t.v)}</b>`).join(' · ')}</td>` }));
    }
    tbl.appendChild(tb);
    wrap.appendChild(el('div', { class: 'card' }, [el('h3', { text: `Аппаратура внутри (${items.length})` }), tbl]));
    return wrap;
  }

  function crewTab(o) {
    const wrap = el('div');
    const crew = o.crew || [];
    if (!crew.length) { wrap.appendChild(el('div', { class: 'card', html: '<h3>Личный состав</h3><div style="color:var(--dim)">За объектом не закреплено личного состава</div>' })); return wrap; }
    const tbl = el('table', { class: 'tbl' });
    tbl.appendChild(el('thead', { html: '<tr><th>Звание</th><th>ФИО</th><th>Должность</th><th>Позывной</th><th>Контакт</th></tr>' }));
    const tb = el('tbody');
    for (const p of crew) tb.appendChild(el('tr', { html: `<td>${esc(p.rank)}</td><td>${esc(p.fio)}</td><td>${esc(p.post)}</td><td>${esc(p.callSign)}</td><td>${esc(p.phone)}</td>` }));
    tbl.appendChild(tb);
    wrap.appendChild(el('div', { class: 'card' }, [el('h3', { text: `Закреплено по штату: ${crew.length}` }), tbl]));
    return wrap;
  }

  function techTab(o, unit) {
    const t = o.techState || techStateForUnit(unit || { unitId: o.id, condition: o.condition || 'green' });
    const wrap = el('div');
    wrap.appendChild(el('div', { class: 'card', html: `<h3>Техническое состояние</h3><dl class="kv">
      <dt>Статус</dt><dd>${COND_TITLE[t.condition] || t.condition}</dd>
      <dt>Процент износа</dt><dd>${t.wearPct}%</dd>
      <dt>Комплект ЗИП</dt><dd>${esc(t.zip)}</dd>
      <dt>Последнее ТО</dt><dd>${esc(t.lastMaintenance)}</dd>
      <dt>Следующее ТО</dt><dd>${esc(t.nextMaintenance)}</dd>
      <dt>Наработка</dt><dd>${t.runTimeH} ч</dd>
    </dl><div class="bar-bg"><div class="bar-fill ${t.wearPct > 60 ? 'bad' : t.wearPct > 30 ? 'warn' : ''}" style="width:${Math.min(100, t.wearPct)}%"></div></div>` }));
    const faults = t.faults || [];
    const card = el('div', { class: 'card' }, [el('h3', { text: `Журнал неисправностей (${faults.length})` })]);
    if (!faults.length) card.appendChild(el('div', { style: 'color:var(--dim)', text: 'Записей нет' }));
    for (const f of faults) card.appendChild(el('div', { class: `issue ${f.status === 'не устранено' ? 'crit' : f.status === 'частично устранено' ? 'warn' : 'info'}`, html: `<b>${esc(f.date)} · ${esc(f.status)}</b><span>${esc(f.text)}</span>` }));
    wrap.appendChild(card);
    if (o.condition === 'red') wrap.appendChild(el('div', { class: 'issue crit', html: '<b>Размещение запрещено</b><span>FR-RES-03: техника в состоянии 🔴 не может быть размещена на карте. Ядро исключит её из расчёта (статус узла «failed»).</span>' }));
    return wrap;
  }

  function planTab(o, dep, res) {
    const wrap = el('div');
    if (!res) { wrap.appendChild(el('div', { class: 'card', html: '<h3>Расчёт не выполнялся</h3><div style="color:var(--dim)">Нажмите «▶ Расчёт (sync)» — данные появятся здесь (FR-INT-03)</div>' })); return wrap; }
    const links = (res.plan?.activeLinks || []).filter((l) => l.from === o.id || l.to === o.id);
    const rej = (res.plan?.rejectedLinks || []).filter((l) => l.from === o.id || l.to === o.id);
    const routes = (res.plan?.routes || []).filter((r) => (r.path || []).includes(o.id));
    wrap.appendChild(el('div', { class: 'card', html: `<h3>Роль в плане</h3><dl class="kv">
      <dt>Развёрнуто ядром</dt><dd>${dep ? 'да' : 'нет'}</dd>
      <dt>Волна развёртывания</dt><dd>${dep ? `${dep.wave ?? '—'} (t=${nf(dep.startMin, 0)} мин)` : '—'}</dd>
      <dt>Стоимость</dt><dd>${dep ? nf(dep.cost, 1) + ' у.е.' : '—'}</dd>
      <dt>Активных линий</dt><dd>${links.length}</dd>
      <dt>Отклонённых линий</dt><dd>${rej.length}</dd>
      <dt>Маршрутов через объект</dt><dd>${routes.length}</dd>
    </dl>` }));
    if (links.length) {
      const tbl = el('table', { class: 'tbl' });
      tbl.appendChild(el('thead', { html: '<tr><th>Линия</th><th>Тип</th><th class="num">Длина, км</th><th class="num">Запас, дБ</th><th class="num">Ёмкость</th><th class="num">Загрузка</th><th class="num">Задержка</th></tr>' }));
      const tb = el('tbody');
      for (const l of links) tb.appendChild(el('tr', { html: `<td>${esc(l.id)}</td><td>${esc(l.type)}</td><td class="num">${nf(l.distKm, 2)}</td><td class="num" style="color:${linkColorByMargin(l.marginDb)}">${nf(l.marginDb, 1)}</td><td class="num">${nf(l.capacityKbps, 1)}</td><td class="num">${nf(l.utilPct, 0)}%</td><td class="num">${nf(l.delayMs, 1)}</td>` }));
      tbl.appendChild(tb);
      wrap.appendChild(el('div', { class: 'card' }, [el('h3', { text: 'Линии объекта' }), tbl]));
    }
    if (rej.length) wrap.appendChild(el('div', { class: 'card' }, [el('h3', { text: 'Отклонённые линии' })]).appendChild(el('div', { class: 'issue crit', html: rej.map((r) => `<b>${esc(r.id)}: ${esc(r.code)}</b><span>${esc(r.reason)} · ${nf(r.distKm, 2)} км · запас ${nf(r.marginDb, 1)} дБ</span>`).join('') })) .parentNode);
    return wrap;
  }

  function openUnitCard(u) {
    const placed = state.objects.find((o) => o.unitId === u.unitId);
    const tabs = [
      { label: 'Состав оборудования', render: () => equipmentTab(placed || { id: u.unitId, name: u.kind, unitId: u.unitId, nodeType: u.nodeType, lat: 0, lng: 0, equipment: equipmentForUnit(u), unitSub: u.subunit, parentId: u.parentId }, u) },
      { label: 'Личный состав', render: () => crewTab(placed || { crew: crewForUnit(u) }) },
      { label: 'Техническое состояние', render: () => techTab(placed || { techState: techStateForUnit(u), condition: u.condition }, u) },
    ];
    const foot = [
      el('span', { class: 'spacer', text: `${u.unitId} · ${u.subunit || ''} · ${COND_TITLE[u.condition]}` }),
      el('button', { class: 'tb-btn', text: u.condition === 'green' ? '→ 🟡 Огр. пригодна' : '→ 🟢 Исправна', onclick: () => { setCondition(u, u.condition === 'green' ? 'yellow' : 'green'); openUnitCard(u); } }),
      placed ? el('button', { class: 'tb-btn', text: 'Показать на карте', onclick: () => { closeModal(); selectOnly(placed.id); flyTo(placed); } }) : el('button', { class: 'tb-btn', text: 'Разместить в центре', onclick: () => { const c = unproject(state.view, vw / 2, vh / 2); const o = placeUnit(u, c.lat, c.lng); if (o) { closeModal(); selectOnly(o.id); flyTo(o); } } }),
      el('button', { class: 'tb-btn accent', text: 'Закрыть', onclick: closeModal }),
    ];
    openModal(`${u.kind}`, tabs, 0, foot);
  }

  function openLineCard(l, tabIdx = 0) {
    const res = state.lastResult;
    const pl = res?.plan?.activeLinks?.find((x) => x.id === l.id);
    const rj = res?.plan?.rejectedLinks?.find((x) => x.id === l.id);
    const a = objById(l.from), b = objById(l.to);
    const dKm = a && b ? haversineKm(a, b) : 0;
    const tabs = [
      {
        label: 'Параметры линии',
        render: () => {
          const w = el('div');
          w.appendChild(el('div', { class: 'card', html: `<h3>Линия ${esc(l.id)}</h3><dl class="kv">
            <dt>От</dt><dd>${esc(a ? a.name || a.id : l.from)}</dd>
            <dt>До</dt><dd>${esc(b ? b.name || b.id : l.to)}</dd>
            <dt>Длина (гаверсинус)</dt><dd>${nf(dKm, 3)} км</dd>
            <dt>Тип</dt><dd>${esc(l.type)}</dd>
            <dt>Резервная</dt><dd>${l.isBackup ? 'да' : 'нет'}</dd>
          </dl>` }));
          const f = el('div', { class: 'frm card' });
          f.appendChild(el('h3', { text: 'Редактирование' }));
          const add = (label, node) => { f.appendChild(el('label', { text: label })); f.appendChild(node); };
          const sel = el('select');
          for (const t of ['VHF', 'UHF', 'HF', 'RRL', 'SAT', 'FOC']) sel.appendChild(el('option', { value: t, text: t, ...(l.type === t ? { selected: 'selected' } : {}) }));
          sel.value = l.type;
          sel.addEventListener('change', () => { l.type = sel.value; afterTopologyChange('line-type'); });
          add('Тип линии', sel);
          for (const [key, label, min, max, step] of [['freqMhz', 'Частота, МГц', 0.001, 40000, 0.001], ['bwKhz', 'Полоса, кГц', 0.5, 100000, 0.5], ['cost', 'Стоимость, у.е.', 0, 100000, 1], ['priority', 'Приоритет (1…5)', 1, 5, 1]]) {
            const i = el('input', { type: 'number', min: String(min), max: String(max), step: String(step), value: l[key] === undefined ? '' : String(l[key]) });
            i.addEventListener('change', () => { l[key] = i.value === '' ? undefined : Number(i.value); afterTopologyChange('line-param'); });
            add(label, i);
          }
          w.appendChild(f);
          return w;
        },
      },
      {
        label: 'Результат расчёта',
        render: () => {
          const w = el('div');
          if (pl) w.appendChild(el('div', { class: 'card', html: `<h3>Линия активна</h3><dl class="kv">
            <dt>Запас по уровню</dt><dd style="color:${linkColorByMargin(pl.marginDb)}">${nf(pl.marginDb, 2)} дБ</dd>
            <dt>Ёмкость (с резервом)</dt><dd>${nf(pl.capacityKbps, 1)} кбит/с</dd>
            <dt>Нагрузка</dt><dd>${nf(pl.loadKbps, 1)} кбит/с (${nf(pl.utilPct, 0)}%)</dd>
            <dt>Задержка</dt><dd>${nf(pl.delayMs, 2)} мс</dd>
            <dt>Доступность</dt><dd>${nf(pl.availabilityPct, 2)} %</dd>
            <dt>Потери трассы</dt><dd>${nf(pl.physics?.plDb, 1)} дБ</dd>
            <dt>Уровень приёма</dt><dd>${nf(pl.physics?.rxDbm, 1)} дБм</dd>
            <dt>SNR</dt><dd>${nf(pl.physics?.snrDb, 1)} дБ</dd>
            <dt>Прямая видимость</dt><dd>${pl.physics?.los ? 'да' : 'нет'}</dd>
            <dt>Стоимость</dt><dd>${nf(pl.cost, 1)} у.е.</dd>
          </dl>` }));
          else if (rj) w.appendChild(el('div', { class: 'issue crit', html: `<b>Линия отклонена ядром: ${esc(rj.code)}</b><span>${esc(rj.reason)} · длина ${nf(rj.distKm, 2)} км · запас ${nf(rj.marginDb, 1)} дБ · ёмкость ${nf(rj.capKbps, 1)} кбит/с</span>` }));
          else w.appendChild(el('div', { class: 'card', html: '<div style="color:var(--dim)">Расчёт не выполнялся</div>' }));
          return w;
        },
      },
    ];
    openModal(`Линия связи ${l.id}`, tabs, tabIdx, [
      el('span', { class: 'spacer', text: `${nf(dKm, 3)} км · ${l.type}` }),
      el('button', { class: 'tb-btn', text: l.isBackup ? 'Снять резерв' : 'Сделать резервной', onclick: () => { l.isBackup = !l.isBackup; afterTopologyChange('flag'); openLineCard(l, tabIdx); } }),
      el('button', { class: 'tb-btn accent', text: 'Закрыть', onclick: closeModal }),
    ]);
  }

  function openParams(o) {
    const s = o.spec || (o.spec = {});
    const w = el('div', { class: 'frm' });
    const add = (label, node) => { w.appendChild(el('label', { text: label })); w.appendChild(node); };
    const num = (key, label, step = '0.1') => {
      const i = el('input', { type: 'number', step, value: s[key] === undefined ? '' : String(s[key]) });
      i.addEventListener('change', () => { s[key] = i.value === '' ? undefined : Number(i.value); afterTopologyChange('params'); });
      add(label, i);
    };
    add('Наименование', (() => { const i = el('input', { type: 'text', value: o.name || '' }); i.addEventListener('change', () => { o.name = i.value; renderTree(); invalidate(); }); return i; })());
    num('txPowerDbm', 'Мощность передатчика, дБм');
    num('txGainDbi', 'Усиление ант. (передатчик), дБи');
    num('rxGainDbi', 'Усиление ант. (приёмник), дБи');
    num('rxSensDbm', 'Чувствительность приёмника, дБм', '0.1');
    num('antHeightM', 'Высота подвеса антенны, м');
    num('freqMhz', 'Частота, МГц', '0.001');
    num('feederLossDb', 'Потери в фидере, дБ');
    num('noiseFigureDb', 'Шум-фактор, дБ');
    const mSel = el('select');
    for (const v of ['static', 'portable', 'mobile', 'vehicle']) mSel.appendChild(el('option', { value: v, text: v }));
    mSel.value = s.mobility || 'static';
    mSel.addEventListener('change', () => { s.mobility = mSel.value; afterTopologyChange('params'); });
    add('Подвижность', mSel);
    num('deployTimeMin', 'Время развертывания, мин', '1');
    num('costDeploy', 'Стоимость развертывания, у.е.', '1');
    const coord = el('div', { class: 'frm' });
    for (const [k, label] of [['lat', 'Широта'], ['lng', 'Долгота']]) {
      const i = el('input', { type: 'number', step: '0.000001', value: o[k].toFixed(6) });
      i.addEventListener('change', () => { o[k] = round6(Number(i.value)); afterTopologyChange('coord'); flyTo(o); });
      coord.appendChild(el('label', { text: label }));
      coord.appendChild(i);
    }
    w.appendChild(el('div', { class: 'full' }));
    w.appendChild(coord);
    w.appendChild(el('div', { class: 'full', style: 'color:var(--dim);font-size:10.5px', text: 'NFR-03: координаты хранятся и передаются с точностью 6 знаков после запятой (~0,11 м).' }));
    openModal(`Параметры — ${o.name || o.id}`, [{ label: 'Радиопараметры', render: () => w }], 0, [el('button', { class: 'tb-btn accent', text: 'Готово', onclick: closeModal })]);
  }

  function openPlacer(ll) {
    const w = el('div');
    w.appendChild(el('div', { class: 'card', html: `<h3>Размещение в точке</h3><dl class="kv"><dt>Широта</dt><dd>${ll.lat.toFixed(6)}</dd><dt>Долгота</dt><dd>${ll.lng.toFixed(6)}</dd></dl>` }));
    const list = el('div');
    const ready = state.fleet.filter((u) => u.condition !== 'red' && !state.objects.some((o) => o.unitId === u.unitId));
    if (!ready.length) list.appendChild(el('div', { style: 'color:var(--dim)', text: 'Свободной исправной техники нет' }));
    for (const u of ready.slice(0, 60)) {
      list.appendChild(el('div', { class: 'trow', onclick: () => { const o = placeUnit(u, ll.lat, ll.lng); if (o) { closeModal(); selectOnly(o.id); } }, html: `<span class="tcond c-${u.condition}"></span><span class="tname">${esc(u.kind)}</span><span class="tmeta">${esc(u.unitId)}</span>` }));
    }
    w.appendChild(el('div', { class: 'card' }, [el('h3', { text: `Свободная техника (${ready.length})` }), list]));
    openModal('Разместить технику', [{ label: 'Парк техники', render: () => w }], 0, [el('button', { class: 'tb-btn accent', text: 'Закрыть', onclick: closeModal })]);
  }

  function openConstraints() {
    const c = state.constraints;
    const w = el('div', { class: 'frm' });
    const add = (label, node, hint) => { w.appendChild(el('label', { text: label, title: hint || '' })); w.appendChild(node); };
    const num = (obj, key, label, step = '1', hint = '') => {
      const i = el('input', { type: 'number', step, value: String(obj[key] ?? '') });
      i.addEventListener('change', () => { obj[key] = i.value === '' ? undefined : Number(i.value); });
      add(label, i, hint);
    };
    num(c, 'budget', 'Бюджет, у.е.', '10', 'Ограничение стоимости развертывания и активации линий');
    num(c, 'minMarginDb', 'Мин. запас, дБ', '0.5');
    num(c, 'maxDelayMs', 'Макс. задержка, мс', '10');
    num(c, 'maxHops', 'Макс. число хопов', '1');
    num(c, 'reserveFactor', 'Коэффициент резервирования', '0.05');
    num(c, 'maxSimultaneousDeploy', 'Одновременных бригад', '1', 'Ограничение расписания развёртывания');
    num(c, 'crewSpeedKmh', 'Скорость марша, км/ч', '5');
    num(c, 'maxUtilizationPct', 'Порог загрузки каналов, %', '5');
    num(c, 'timeLimitMs', 'Лимит времени расчёта, мс', '1000');
    const chk = (key, label) => {
      const i = el('input', { type: 'checkbox' });
      i.checked = !!c[key];
      i.addEventListener('change', () => { c[key] = i.checked; });
      const wrap = el('label', { style: 'display:flex;gap:6px;align-items:center', text: label });
      w.appendChild(wrap);
      w.appendChild(i);
    };
    chk('requireRedundancy', 'Требовать резервирование (2 непересекающихся пути)');
    chk('allowYellowUnits', 'Разрешить 🟡 ограниченно пригодную технику');
    chk('allowRedUnits', 'Разрешить 🔴 неисправную технику (нарушение FR-RES-03)');
    const sel = el('select');
    for (const v of ['auto', 'fast', 'full']) sel.appendChild(el('option', { value: v, text: v }));
    sel.value = c.physics || 'auto';
    sel.addEventListener('change', () => { c.physics = sel.value; $('#physicsSel').value = sel.value; });
    add('Режим физики', sel, 'fast — аналитический; full — ENG6.calcLink с рельефом');
    const env = el('select');
    for (const v of ['open', 'forest', 'urban', 'dense_urban']) env.appendChild(el('option', { value: v, text: v }));
    env.value = state.environment.clutterType;
    env.addEventListener('change', () => { state.environment.clutterType = env.value; terrainKey = ''; invalidate(); });
    add('Тип местности', env);
    const wt = el('div', { class: 'frm' });
    for (const k of ['cost', 'delay', 'survivability', 'margin']) {
      const i = el('input', { type: 'number', step: '0.05', value: String(state.weights[k]) });
      i.addEventListener('change', () => { state.weights[k] = Number(i.value); });
      wt.appendChild(el('label', { text: `Вес: ${k}` }));
      wt.appendChild(i);
    }
    w.appendChild(el('div', { class: 'full' }));
    w.appendChild(wt);
    openModal('Параметры расчёта и ограничения', [{ label: 'Ограничения и веса', render: () => w }], 0, [
      el('span', { class: 'spacer', text: 'Значения передаются в ядро в блоках constraints и weights' }),
      el('button', { class: 'tb-btn', text: 'Сбросить к типовым', onclick: () => { Object.assign(state.constraints, { budget: 1200, minMarginDb: 3, maxDelayMs: 250, maxHops: 8, maxSimultaneousDeploy: 3, reserveFactor: 1.3, requireRedundancy: false, physics: 'auto', timeLimitMs: 60000, maxUtilizationPct: 85, crewSpeedKmh: 40 }); openConstraints(); } }),
      el('button', { class: 'tb-btn accent', text: 'Применить', onclick: () => { closeModal(); runCalc({ reason: 'constraints' }); } }),
    ]);
  }

  /* ═══ §10. Интеграция с ядром ENG6-PLAN (FR-INT-02, FR-INT-03) ═════════ */

  function apiBase() { return ($('#apiBase').value || '/api/v1').replace(/\/+$/, ''); }
  function setConn(stateName, text) {
    const c = $('#connState');
    c.className = `chip ${stateName === 'ok' ? 'chip-ok' : stateName === 'bad' ? 'chip-bad' : stateName === 'warn' ? 'chip-warn' : 'chip-dim'}`;
    c.textContent = text;
  }

  /**
   * Отправка пакета в ядро. NFR-02: только по настроенному каналу; в production
   * API обязан работать по TLS (проверяется конфигурацией сервиса).
   */
  async function api(method, path, body, headers = {}) {
    const h = { 'Content-Type': 'application/json', 'X-Requestor': encodeURIComponent('Оператор планировщика (UI)'), ...headers };
    const r = await fetch(apiBase() + path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await r.text();
    let data = null;
    try { data = JSON.parse(text); } catch { data = text; }
    return { status: r.status, headers: Object.fromEntries(r.headers.entries()), data };
  }

  async function runCalc(opts = {}) {
    const request = buildRequest(state, { physics: $('#physicsSel').value === 'auto' ? undefined : $('#physicsSel').value, requestId: `ui-${Date.now().toString(36)}` });
    if (!request.topology.nodes.length) { toast('Нет данных', 'Разместите хотя бы одну единицу техники на карте', 'warn'); return; }
    setConn('warn', 'ядро: расчёт…');
    $('#btnCalc').disabled = true;
    const t0 = performance.now();
    logMsg('info', `POST ${apiBase()}/plan/sync · узлов ${request.topology.nodes.length} · линий ${request.topology.links.length} · заявок ${request.demands.length}`);
    try {
      const r = await api('POST', '/plan/sync', request);
      const dt = performance.now() - t0;
      if (r.status === 200 || r.status === 422) {
        const res = r.data;
        applyPlanToState(state, res);
        renderResults(res, { mode: 'sync', httpMs: dt, http: r.status });
        setConn(res.status === 'INVALID' ? 'bad' : 'ok', `ядро: ${res.status} · ${nf(res.timings?.totalMs ?? dt, 0)} мс`);
        logMsg(res.status === 'INVALID' ? 'err' : 'ok', `Расчёт завершён: ${res.status} за ${nf(res.timings?.totalMs ?? dt, 1)} мс (HTTP ${r.status}, ${nf(dt, 0)} мс)`);
        if (res.status === 'INVALID') toast('Вход отклонён валидатором', (res.statusReasons || []).slice(0, 3).map((x) => `${x.path}: ${x.message}`).join('\n'), 'err', 9000);
        else if (res.status === 'INFEASIBLE' || res.status === 'PARTIAL') toast(`План: ${res.status}`, summarizeStatus(res), 'warn', 9000);
        else if (!opts.silent) toast(`План: ${res.status}`, summarizeStatus(res), 'ok', 6000);
        invalidate();
      } else if (r.status === 429) {
        setConn('warn', 'ядро: 429 (занято)');
        const retry = r.headers['retry-after'];
        logMsg('warn', `429 Too Many Requests: ${r.data?.error?.message}${retry ? ` (Retry-After: ${retry} с)` : ''}`);
        toast('Ядро перегружено (429)', `${r.data?.error?.message || 'Все слоты заняты'}. Повторите через ${retry || 5} с или отправьте задачу в очередь (async).`, 'warn', 9000);
      } else {
        setConn('bad', `ядро: HTTP ${r.status}`);
        logMsg('err', `HTTP ${r.status}: ${typeof r.data === 'string' ? r.data.slice(0, 200) : JSON.stringify(r.data).slice(0, 200)}`);
        toast('Ошибка ядра', `HTTP ${r.status}: ${(r.data && r.data.error && r.data.error.message) || 'неизвестная ошибка'}`, 'err', 9000);
      }
    } catch (err) {
      setConn('bad', 'ядро: недоступно');
      logMsg('err', `Сеть: ${err.message}`);
      toast('Ядро недоступно', `${err.message}. Проверьте адрес API (${apiBase()}) и что сервис запущен: node api/main.mjs. Доступен демонстрационный режим (встроенный результат расчёта).`, 'err', 12000);
      if (!state.lastResult && state.demoResult) { applyPlanToState(state, state.demoResult); renderResults(state.demoResult, { mode: 'demo' }); setConn('warn', 'ядро: демо-данные'); }
    } finally {
      $('#btnCalc').disabled = false;
    }
  }

  function summarizeStatus(res) {
    const m = res.metrics || {};
    return `заявок ${m.demandsSatisfied || 0}/${m.demandsTotal || 0} · линий ${m.linksActive || 0} · стоимость ${nf(m.totalCost, 0)} у.е. · задержка ${nf(m.avgDelayMs, 0)} мс · связность ${nf(m.connectivityPct, 0)}% · оценка ${m.grade || '—'}`;
  }

  async function runAsync() {
    const request = buildRequest(state, { physics: $('#physicsSel').value === 'auto' ? undefined : $('#physicsSel').value });
    if (!request.topology.nodes.length) { toast('Нет данных', 'Разместите технику на карте', 'warn'); return; }
    setConn('warn', 'ядро: постановка в очередь…');
    try {
      const r = await api('POST', '/plan/async', request);
      if (r.status === 202) {
        const d = r.data;
        state.task = d;
        setConn('warn', `очередь: ${d.taskId.slice(0, 14)}… (поз. ${d.position})`);
        logMsg('info', `202 Accepted: задача ${d.taskId}, позиция ${d.position}, оценка ожидания ${nf(d.estimatedWaitMs / 1000, 1)} с`);
        toast('Задача поставлена в очередь', `${d.taskId}\nПозиция: ${d.position} · ожидание ≈ ${nf(d.estimatedWaitMs / 1000, 1)} с`, 'ok', 7000);
        pollTask(d.taskId);
      } else if (r.status === 429) {
        setConn('warn', 'очередь переполнена');
        logMsg('warn', `429: ${r.data?.error?.message}`);
        toast('Очередь переполнена (429)', `${r.data?.error?.message}. Сервис честно отказывает, а не «виснет» — повторите позже.`, 'warn', 9000);
      } else {
        setConn('bad', `HTTP ${r.status}`);
        toast('Ошибка постановки', `HTTP ${r.status}: ${(r.data && r.data.error && r.data.error.message) || ''}`, 'err');
      }
    } catch (err) {
      setConn('bad', 'ядро: недоступно');
      toast('Ядро недоступно', err.message, 'err');
    }
  }

  async function pollTask(taskId, attempt = 0) {
    try {
      const r = await api('GET', `/tasks/${taskId}`, undefined, { 'X-Result-Format': 'full' });
      const d = r.data;
      if (r.status === 404) { setConn('bad', 'задача не найдена'); return; }
      if (d.state === 'SUCCEEDED' || d.state === 'FAILED' || d.state === 'TIMEOUT' || d.state === 'CANCELLED') {
        if (d.state === 'SUCCEEDED' && d.result) {
          applyPlanToState(state, d.result);
          renderResults(d.result, { mode: 'async', taskId, computeMs: d.computeMs, waitMs: d.queueWaitMs });
          setConn('ok', `ядро: ${d.result.status} (async ${nf(d.computeMs, 0)} мс)`);
          logMsg('ok', `Задача ${taskId}: ${d.state}/${d.result.status}, ожидание ${nf(d.queueWaitMs, 0)} мс, расчёт ${nf(d.computeMs, 0)} мс, результат ${nf((d.resultBytes || 0) / 1024, 0)} КБ`);
          invalidate();
        } else {
          setConn('bad', `задача: ${d.state}`);
          logMsg('err', `Задача ${taskId}: ${d.state} ${d.error ? d.error.message : ''}`);
          toast(`Задача ${d.state}`, d.error ? d.error.message : 'Расчёт не выполнен', 'err', 9000);
        }
        state.task = null;
        return;
      }
      setConn('warn', `очередь: ${d.state} (поз. ${d.position ?? '—'})`);
      if (attempt > 600) { setConn('bad', 'ожидание прервано'); return; }
      setTimeout(() => pollTask(taskId, attempt + 1), Math.min(3000, 400 + attempt * 100));
    } catch (err) {
      logMsg('err', `Опрос задачи: ${err.message}`);
      setTimeout(() => pollTask(taskId, attempt + 1), 2000);
    }
  }

  /* ═══ §11. Панель результатов (FR-INT-03) ══════════════════════════════ */

  function renderResults(res, info = {}) {
    switchTab('res');
    const m = res.metrics || {};
    const p = res.plan || {};
    const d = res.diagnostics || {};

    $('#resSummary').innerHTML = `<div class="card">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
        <span class="badge b-${esc(res.status)}">${esc(res.status)}</span>
        <span class="grade g-${esc(m.grade || 'F')}">${esc(m.grade || '—')}</span>
        <span style="color:var(--dim);font-size:10.5px">F=${nf(m.objectiveValue, 4)} · ${esc(info.mode || '')}${info.computeMs ? ' · расчёт ' + nf(info.computeMs, 0) + ' мс' : ''}${info.waitMs ? ' · очередь ' + nf(info.waitMs, 0) + ' мс' : ''}${info.httpMs ? ' · HTTP ' + nf(info.httpMs, 0) + ' мс' : ''}</span>
      </div>
      <div style="font-size:11px;color:var(--dim);line-height:1.5">${(res.statusReasons || []).map((r) => esc(r.message)).join('<br>') || '—'}</div>
      ${res.status === 'INVALID' ? `<div class="issue crit" style="margin-top:6px"><b>Ошибки валидации</b><span>${(d.validation?.errors || []).slice(0, 12).map((e) => `${esc(e.path)}: ${esc(e.message)}`).join('<br>')}</span></div>` : ''}
    </div>`;

    const kv = (rows) => `<dl class="kv">${rows.map(([k, v, cls]) => `<dt>${k}</dt><dd class="${cls || ''}">${v}</dd>`).join('')}</dl>`;
    $('#resMetrics').innerHTML = `<div class="card"><h3>Метрики плана</h3>${kv([
      ['Заявки: обслужено / всего', `${m.demandsSatisfied ?? 0} / ${m.demandsTotal ?? 0}`, (m.demandsRejected || 0) > 0 ? 'p-rej' : ''],
      ['Частично обслужено', `${m.demandsPartial ?? 0}`],
      ['Отклонено', `${m.demandsRejected ?? 0}`, (m.demandsRejected || 0) > 0 ? 'p-rej' : ''],
      ['Обслужено полосы, %', `${nf(m.demandServedPct, 1)}`],
      ['Суммарная нагрузка', `${nf(m.throughputKbps, 1)} кбит/с`],
      ['Ёмкость активных линий', `${nf(m.capacityTotalKbps, 0)} кбит/с`],
      ['Загрузка max / avg', `${nf(m.utilMaxPct, 0)}% / ${nf(m.utilAvgPct, 0)}%`, m.utilMaxPct > 85 ? 'p-part' : ''],
      ['Задержка avg / max', `${nf(m.avgDelayMs, 1)} / ${nf(m.maxDelayMs, 1)} мс`],
      ['Стоимость / бюджет', `${nf(m.totalCost, 0)} / ${m.budget === null || m.budget === undefined ? '∞' : nf(m.budget, 0)} у.е.`],
      ['Использование бюджета', m.budgetUsedPct === null || m.budgetUsedPct === undefined ? '—' : `${nf(m.budgetUsedPct, 1)} %`],
      ['Связность сети', `${nf(m.connectivityPct, 1)} %`, m.connectivityPct < 100 ? 'p-part' : ''],
      ['Компонент связности', `${m.componentsCount ?? 0}`],
      ['ЕТО (узлы / линии-мосты)', `${m.spofNodes ?? 0} / ${m.bridgeLinks ?? 0}`, (m.spofNodes || 0) > 0 ? 'p-rej' : ''],
      ['Min-cut между КП', m.minCutKbps === null || m.minCutKbps === undefined ? 'не считался' : `${nf(m.minCutKbps, 0)} кбит/с`],
      ['Запас avg / min', `${nf(m.avgMarginDb, 1)} / ${m.minMarginDb === null || m.minMarginDb === undefined ? '—' : nf(m.minMarginDb, 1)} дБ`],
      ['Узлов развёрнуто', `${m.nodesDeployed ?? 0} из ${m.nodesTotal ?? 0}`],
      ['Линий: активно / допустимо / всего', `${m.linksActive ?? 0} / ${m.linksFeasible ?? 0} / ${m.linksCandidate ?? 0}`],
      ['Резервирование добавлено', `${m.redundancyAdded ?? 0}`],
      ['Расписание: makespan / волн', `${nf(m.scheduleMakespanMin, 0)} мин / ${m.scheduleWaves ?? 0}`],
      ['Загрузка бригад', `${nf(m.crewUtilPct, 0)} %`],
      ['Нижняя оценка стоимости', `${nf(d.lowerBoundCost, 1)} у.е.`],
    ])}</div>`;

    const routes = (p.routes || []);
    $('#resRoutes').innerHTML = `<div class="card"><h3>Маршруты и заявки (${routes.length}${p.routesTotal > routes.length ? ` из ${p.routesTotal}` : ''})</h3>
      <div style="max-height:230px;overflow:auto">
      <table class="tbl"><thead><tr><th>Заявка</th><th>Статус</th><th class="num">Полоса</th><th class="num">Задержка</th><th class="num">Хопы</th><th class="num">Запас</th><th>Рез.</th></tr></thead><tbody>
      ${routes.map((r) => `<tr data-demand="${esc(r.demandId)}" style="cursor:pointer">
        <td>${esc(r.demandId)}<div style="color:var(--dim);font-size:9px">${esc(r.from)} → ${esc(r.to)}</div></td>
        <td><span class="pill ${r.status === 'ROUTED' ? 'p-ok' : r.status === 'PARTIAL' ? 'p-part' : 'p-rej'}" title="${esc(r.reason || '')}">${esc(r.status)}</span></td>
        <td class="num">${nf(r.grantedKbps, 1)}${r.deficitKbps > 0 ? `<div style="color:var(--yellow);font-size:9px">−${nf(r.deficitKbps, 1)}</div>` : ''}<div style="color:var(--dim);font-size:9px">запрос ${nf(r.requestedKbps, 1)}</div></td>
        <td class="num">${nf(r.delayMs, 1)}</td><td class="num">${r.hops ?? '—'}</td><td class="num">${nf(r.marginDb, 1)}</td>
        <td>${r.redundant ? '🛡' : ''}${r.backupPath ? '⧉' : ''}</td></tr>`).join('')}
      </tbody></table></div>
      <div style="color:var(--dim);font-size:10px;margin-top:4px">Клик по строке — подсветка маршрута на карте (основной — синий, резервный — зелёный).</div></div>`;
    $$('#resRoutes tr[data-demand]').forEach((tr) => tr.addEventListener('click', () => {
      state.selectedDemand = state.selectedDemand === tr.dataset.demand ? null : tr.dataset.demand;
      $$('#resRoutes tr[data-demand]').forEach((x) => (x.style.background = x.dataset.demand === state.selectedDemand ? '#243044' : ''));
      invalidate();
    }));

    const issues = (d.issues || []).slice(0, 60);
    const recs = (d.recommendations || []).slice(0, 20);
    const inf = d.infeasibility;
    $('#resIssues').innerHTML = `<div class="card"><h3>Замечания и рекомендации</h3>
      ${inf && inf.causes && inf.causes.length ? `<div class="issue crit"><b>Причины невыполнимости</b><span>${inf.causes.map((c) => esc(c.message)).join('<br>')}</span></div>` : ''}
      ${inf && inf.minimalRelaxations && inf.minimalRelaxations.length ? `<div class="issue rec"><b>Минимальные послабления / частичное решение</b><span>${inf.minimalRelaxations.slice(0, 10).map((r) => relaxText(r)).join('<br>')}</span></div>` : ''}
      ${inf && inf.partialSolution ? `<div class="issue info"><b>Частичное решение</b><span>обслужено ${inf.partialSolution.demandsServed}/${inf.partialSolution.demandsTotal} заявок · ${nf(inf.partialSolution.throughputKbps, 1)} кбит/с · ${inf.partialSolution.linksActivated} линий · ${inf.partialSolution.nodesDeployed} узлов · стоимость ${nf(inf.partialSolution.cost, 0)} у.е.</span></div>` : ''}
      ${recs.map((r) => `<div class="issue rec"><b>${esc(r.title)}</b><span>${esc(r.text)}</span></div>`).join('')}
      ${issues.map((i) => `<div class="issue ${esc(i.sev)}"><b>${esc(i.title)}${i.code ? ` · ${esc(i.code)}` : ''}</b><span>${esc(i.text)}</span></div>`).join('')}
      ${!issues.length && !recs.length ? '<div style="color:var(--dim)">Замечаний нет</div>' : ''}
      ${d.eng6Analysis ? `<div class="issue ${d.eng6Analysis.skipped ? 'info' : d.eng6Analysis.verdict === 'CONSISTENT' ? 'rec' : 'warn'}"><b>Перекрёстная проверка ядром ENG6</b><span>${d.eng6Analysis.skipped ? `не выполнялась (${esc(d.eng6Analysis.reason)})` : `${esc(d.eng6Analysis.verdict)} · оценка ${esc(d.eng6Analysis.ratings?.grade)} (${nf(d.eng6Analysis.ratings?.overall, 1)}) · активных линий по ENG6: ${d.eng6Analysis.comparison?.activeLinks?.eng6} из ${d.eng6Analysis.comparison?.activeLinks?.planner}`}</span></div>` : ''}
    </div>`;

    const sch = p.schedule;
    $('#resSchedule').innerHTML = sch ? `<div class="card"><h3>Расписание развёртывания</h3>
      <div class="kv"><dt>Всего времени (makespan)</dt><dd>${nf(sch.makespanMin, 0)} мин</dd><dt>Волн</dt><dd>${sch.waves?.length ?? 0}</dd><dt>Бригад одновременно</dt><dd>${sch.crews ?? 0}</dd><dt>Загрузка бригад</dt><dd>${nf(sch.crewUtilPct, 0)}%</dd><dt>Точка отсчёта марша</dt><dd>${sch.referencePoint ? `${sch.referencePoint.lat}, ${sch.referencePoint.lng}` : '—'}</dd></div>
      <div style="max-height:210px;overflow:auto;margin-top:6px">
      ${(sch.waves || []).map((w) => `<div class="wave"><div class="wave-hd"><span>Волна ${w.index}</span><span>${nf(w.tStartMin, 0)}…${nf(w.tEndMin, 0)} мин · бригад ${w.crewsUsed}</span></div>
        <div class="wave-items">${w.items.map((it) => `<span class="wave-item" data-node="${esc(it.nodeId)}" title="t=${nf(it.startMin, 0)}…${nf(it.endMin, 0)} мин, бригада ${it.crew + 1}">${esc(it.nodeId)}</span>`).join('')}</div></div>`).join('')}
      </div>
      ${sch.criticalPath && sch.criticalPath.length ? `<div style="color:var(--dim);font-size:10px;margin-top:4px">Критический путь: ${sch.criticalPath.map((c) => esc(c.id)).join(' → ')}</div>` : ''}
      ${(sch.violations || []).length ? `<div class="issue warn"><b>Нарушения порядка развёртывания</b><span>${sch.violations.map(esc).join('<br>')}</span></div>` : ''}
    </div>` : '';
    $$('#resSchedule .wave-item').forEach((s) => s.addEventListener('click', () => { const o = objById(s.dataset.node); if (o) { selectOnly(o.id); flyTo(o); } }));

    const sv = p.survivability;
    $('#resSurvivability').innerHTML = sv ? `<div class="card"><h3>Живучесть и топология</h3>${kv([
      ['Компонент связности', `${sv.componentsCount ?? 0}`],
      ['Связность', `${nf(sv.connectivityPct, 1)} %`],
      ['Наибольшая компонента', `${nf(sv.largestComponentPct, 1)} %`],
      ['ЕТО по узлам', `${sv.spofCount ?? 0}`],
      ['Линий-мостов', `${sv.bridgeCount ?? 0}`],
      ['Min-cut (КП↔КП)', sv.minCutKbps === null || sv.minCutKbps === undefined ? '—' : `${nf(sv.minCutKbps, 0)} кбит/с`],
      ['Рассчитано', `${(sv.computed || []).join(', ') || '—'}`],
    ])}
    ${sv.spofNodes && sv.spofNodes.length ? `<div style="margin-top:5px;display:flex;flex-wrap:wrap;gap:3px">${sv.spofNodes.slice(0, 24).map((id) => `<span class="wave-item" data-node="${esc(id)}" title="Единая точка отказа">${esc(id)}</span>`).join('')}</div>` : ''}
    ${p.rejectedLinksHistogram && Object.keys(p.rejectedLinksHistogram).length ? `<div style="margin-top:6px;color:var(--dim);font-size:10.5px">Отклонено линий: ${Object.entries(p.rejectedLinksHistogram).map(([k, v]) => `${k}=${v}`).join(', ')}</div>` : ''}
    </div>` : '';
    $$('#resSurvivability .wave-item').forEach((s) => s.addEventListener('click', () => { const o = objById(s.dataset.node); if (o) { selectOnly(o.id); flyTo(o); } }));

    renderLegend(res);
    if (p.truncation && Object.values(p.truncation).some(Boolean)) logMsg('warn', `Ответ усечён: ${Object.entries(p.truncation).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join('; ')}`);
  }

  function relaxText(r) {
    switch (r.code) {
      case 'ADD_RELAY': return `установить ретранслятор в точке ${r.at.lat}, ${r.at.lng} (пролёт ${nf(r.spanKm, 1)} км, требуется ЭИИМ ≥ ${nf(r.requiredEirpDbm, 0)} дБм) — свяжет компоненты ${JSON.stringify(r.wouldConnect)}`;
      case 'RAISE_BUDGET': return `увеличить бюджет до ${nf(r.value, 0)} у.е. (${esc(r.reason || '')})`;
      case 'RELAX_MAX_DELAY_MS': return `ослабить ограничение задержки до ${nf(r.value, 0)} мс`;
      case 'ADD_CAPACITY': return `нарастить ёмкость линии ${esc(r.linkId)} на ${nf(r.value, 1)} кбит/с`;
      case 'RELAX_MARGIN_DB': return `снизить требуемый запас до ${nf(r.value, 1)} дБ`;
      default: return `${esc(r.code)}: ${esc(r.reason || '')}`;
    }
  }

  function renderLegend(res) {
    const m = res?.metrics || {};
    $('#legend').innerHTML = `<b>Качество линий (запас, дБ)</b>
      <div class="lg-row"><span class="lg-sw" style="background:#3fb950"></span> ≥ 20 — устойчиво</div>
      <div class="lg-row"><span class="lg-sw" style="background:#7ee2a8"></span> 10…20 — норма</div>
      <div class="lg-row"><span class="lg-sw" style="background:#d29922"></span> 3…10 — малый запас</div>
      <div class="lg-row"><span class="lg-sw" style="background:#f0883e"></span> 0…3 — критический</div>
      <div class="lg-row"><span class="lg-sw" style="background:#c93545"></span> < 0 — связи нет</div>
      <div class="lg-row"><span class="lg-sw" style="background:#6b4a52;height:2px;border-top:1px dashed #c93545"></span> отклонена ядром</div>
      <div style="margin-top:5px"><b>Тактические знаки</b>
      ${Object.entries(SIGN).filter(([k]) => k !== 'unit').map(([k, s]) => `<div class="lg-row"><span class="lg-dot" style="background:${s.color}"></span> ${s.label}</div>`).join('')}
      <div class="lg-row"><span class="lg-dot" style="background:#9aa4b8"></span> Подразделение (кластер)</div></div>
      ${res ? `<div style="margin-top:5px"><b>Результат</b><div class="lg-row">${esc(res.status)} · оценка ${esc(m.grade || '—')}</div><div class="lg-row">линий ${m.linksActive ?? 0} · заявок ${m.demandsSatisfied ?? 0}/${m.demandsTotal ?? 0}</div></div>` : ''}`;
  }

  function renderLog() {
    const host = $('#logList');
    if (!host) return;
    host.innerHTML = state.log.slice(0, 300).map((l) => `<div class="logline ${l.level}"><span class="lt">${esc(l.t)}</span><span class="lm">${esc(l.msg)}</span></div>`).join('');
    $('#logCount').textContent = `записей: ${state.log.length}`;
  }

  function switchTab(name) {
    $$('.rpt').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    for (const t of ['osh', 'res', 'log']) $('#tab-' + t).classList.toggle('hidden', t !== name);
  }

  /* ═══ §12. Импорт/экспорт, демо-данные, инициализация ══════════════════ */

  function exportScenario() {
    const req = buildRequest(state, { requestId: `export-${Date.now().toString(36)}` });
    const payload = {
      format: 'eng6-plan-request',
      formatVersion: '1.0.0',
      exportedAt: new Date().toISOString(),
      note: 'NFR-02: файл не содержит кэша браузера; координаты WGS-84, 6 знаков (NFR-03)',
      request: req,
      oush: state.oushTree,
      fleet: state.fleet,
      objects: state.objects.map((o) => ({ id: o.id, kind: o.kind, nodeType: o.nodeType, name: o.name, lat: o.lat, lng: o.lng, unitId: o.unitId, groupId: o.groupId || null, parentId: o.parentId, condition: o.condition, status: o.status })),
      lines: state.lines,
      demands: state.demands,
      result: state.lastResult ? { status: state.lastResult.status, metrics: state.lastResult.metrics, timings: state.lastResult.timings, core: state.lastResult.core } : null,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = el('a', { href: URL.createObjectURL(blob), download: `eng6_scenario_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.json` });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    logMsg('ok', `Экспорт сценария: ${payload.objects.length} объектов, ${payload.lines.length} линий`);
    toast('Сценарий выгружен', `Объектов: ${payload.objects.length} · линий: ${payload.lines.length}`, 'ok');
  }

  function importFile(file) {
    const rd = new FileReader();
    rd.onload = () => {
      const text = String(rd.result);
      try {
        if (/\.csv$/i.test(file.name)) importCsv(text);
        else importJson(text);
      } catch (err) {
        toast('Ошибка импорта', err.message, 'err', 9000);
        logMsg('err', `Импорт: ${err.message}`);
      }
    };
    rd.onerror = () => toast('Ошибка чтения файла', String(rd.error), 'err');
    rd.readAsText(file);
  }

  function importCsv(text) {
    const { nodes, links } = csvToScenario(text);
    let placed = 0;
    for (const nd of nodes) {
      if (!nd.id || !Number.isFinite(nd.lat) || !Number.isFinite(nd.lng)) continue;
      const unit = state.fleet.find((u) => u.unitId === nd.unitId) || null;
      const o = {
        id: nd.id, kind: 'asset', nodeType: nd.type || 'radio', name: nd.name || nd.id,
        lat: round6(nd.lat), lng: round6(nd.lng), unitId: unit ? unit.unitId : null,
        condition: unit ? unit.condition : 'green', parentId: nd.parentId || null, unitSub: unit?.subunit || null,
        spec: specForUnit(unit || { unitId: nd.id, nodeType: nd.type || 'radio', kind: nd.name, condition: 'green' }),
        status: nd.status || 'active', placedAt: new Date().toISOString(),
      };
      o.equipment = o.spec.equipment || [];
      o.crew = crewForUnit(unit || { unitId: nd.id, nodeType: o.nodeType });
      o.techState = techStateForUnit(unit || { unitId: nd.id, condition: o.condition });
      state.objects.push(o);
      placed++;
    }
    for (const l of links) if (l.from && l.to && l.from !== l.to) state.lines.push({ id: l.id || nextId('ln', 'line'), from: l.from, to: l.to, type: l.type || 'VHF', freqMhz: l.freqMhz, bwKhz: l.bwKhz, priority: 2, isBackup: false });
    fitToObjects();
    renderTree();
    afterTopologyChange('import');
    toast('Импорт CSV выполнен', `Узлов: ${placed} · линий: ${links.length}. Проверьте привязку к парку техники.`, 'ok');
    logMsg('ok', `Импорт CSV: ${placed} узлов, ${links.length} линий`);
  }

  function importJson(text) {
    applyScenarioPayload(JSON.parse(text));
  }

  /** Загрузка сценария из объекта (импорт файла или встроенные демо-данные). */
  function applyScenarioPayload(data) {
    const req = data.request || data;
    if (req.topology && Array.isArray(req.topology.nodes)) {
      // формат запроса ENG6-PLAN (в т.ч. собственный экспорт)
      state.objects = [];
      state.lines = [];
      state.nodeById = new Map();
      const byUnit = new Map(state.fleet.map((u) => [u.unitId, u]));
      for (const nd of req.topology.nodes) {
        const unit = nd.unitId ? byUnit.get(nd.unitId) : null;
        const spec = { ...nd, nodeType: nd.type, equipment: null };
        const o = {
          id: nd.id, kind: nd.children ? 'nodeGroup' : 'asset', nodeType: nd.type, name: nd.name || nd.id,
          lat: round6(nd.lat), lng: round6(nd.lng), unitId: nd.unitId || null,
          condition: unit ? unit.condition : nd.condition || 'green', parentId: nd.parentId ?? null,
          groupId: nd.groupId || null, unitSub: unit?.subunit || null,
          spec: { ...specForUnit(unit || { unitId: nd.id, nodeType: nd.type, kind: nd.name, condition: 'green' }), ...compact(nd) },
          status: nd.status || 'active', placedAt: new Date().toISOString(),
        };
        o.equipment = o.spec.equipment || [];
        o.crew = unit ? crewForUnit(unit) : crewForUnit({ unitId: nd.id, nodeType: nd.type });
        o.techState = techStateForUnit(unit || { unitId: nd.id, condition: o.condition });
        state.objects.push(o);
      }
      for (const l of req.topology.links || []) state.lines.push({ id: l.id, from: l.from, to: l.to, type: l.type || 'VHF', freqMhz: l.freqMhz, bwKhz: l.bwKhz, mod: l.mod, fec: l.fec, priority: l.priority ?? 2, isBackup: !!l.isBackup, cost: l.cost });
      if (Array.isArray(req.demands)) state.demands = req.demands.map((d) => ({ ...d }));
      if (req.constraints) Object.assign(state.constraints, req.constraints);
      if (req.weights) Object.assign(state.weights, req.weights);
      if (req.environment) Object.assign(state.environment, req.environment);
      if (data.fleet && Array.isArray(data.fleet) && data.fleet.length) { state.fleet = data.fleet; rebuildTreeFromFleet(); }
      if (data.oush && Array.isArray(data.oush) && data.oush.length) state.oushTree = data.oush;
      terrainKey = '';
      fitToObjects();
      renderTree();
      afterTopologyChange('import');
      toast('Сценарий загружен', `Узлов: ${state.objects.length} · линий: ${state.lines.length} · заявок: ${state.demands.length}`, 'ok');
      logMsg('ok', `Импорт JSON: ${state.objects.length} объектов, ${state.lines.length} линий, ${state.demands.length} заявок`);
      return;
    }
    // формат результата расчёта — только визуализация
    if (data.status && data.plan) {
      applyPlanToState(state, data);
      renderResults(data, { mode: 'file' });
      invalidate();
      toast('Загружен результат расчёта', `${data.status}`, 'ok');
      return;
    }
    throw new Error('Неизвестный формат JSON: ожидаются topology.nodes (запрос) или status+plan (результат)');
  }
  function compact(o) {
    const out = {};
    for (const k of ['txPowerDbm', 'txGainDbi', 'rxGainDbi', 'rxSensDbm', 'antHeightM', 'freqMhz', 'feederLossDb', 'noiseFigureDb', 'mobility', 'deployTimeMin', 'costDeploy'])
      if (typeof o[k] === 'number' && Number.isFinite(o[k])) out[k] = o[k];
    return out;
  }

  /* ── Демо-данные: ОШС подразделения связи (FR-RES-01/02) ────────────── */

  const ORG_TEMPLATE = [
    { name: 'Рота связи (в/ч 41001)', type: 'unit', children: [
      { name: 'Взвод управления', type: 'platoon', children: [
        { type: 'vehicle', nodeType: 'cp', kind: 'П-240И — командно-штабная машина', count: 1 },
        { type: 'vehicle', nodeType: 'relay', kind: 'Р-419Л1 — радиорелейная станция', count: 2 },
        { type: 'vehicle', nodeType: 'sat', kind: 'Р-438Н «Белозёр» — станция спутниковой связи', count: 1 },
      ] },
      { name: '1-й радиовзвод (УКВ)', type: 'platoon', children: [
        { type: 'vehicle', nodeType: 'radio', kind: 'Р-168-100К — радиостанция УКВ', count: 4 },
        { type: 'vehicle', nodeType: 'radio', kind: 'Р-187П1 «Азарт» — носимая радиостанция', count: 3 },
      ] },
      { name: '2-й радиовзвод (КВ)', type: 'platoon', children: [
        { type: 'vehicle', nodeType: 'radio', kind: 'Р-161А2М — радиостанция КВ', count: 2 },
      ] },
      { name: 'Взвод наблюдения и РЭБ', type: 'platoon', children: [
        { type: 'vehicle', nodeType: 'observer', kind: '1Л261 — пост радионаблюдения', count: 2 },
        { type: 'vehicle', nodeType: 'ew_jam', kind: 'РБ-341В «Леер-3» — комплекс РЭБ', count: 1 },
        { type: 'vehicle', nodeType: 'ew_rer', kind: 'Р-381Т1 «Таран» — станция РЭР', count: 1 },
      ] },
      { name: 'Кабельно-линейный взвод', type: 'platoon', children: [
        { type: 'vehicle', nodeType: 'relay', kind: 'П-238 — кабельная машина (ВОЛС)', count: 2 },
      ] },
    ] },
  ];

  function buildDemoFleet() {
    const fleet = [];
    const rnd = mulberry(20260116);
    let idx = 0;
    const walk = (node, subunit, parentId) => {
      if (node.type === 'vehicle') {
        for (let i = 0; i < (node.count || 1); i++) {
          idx++;
          const r = rnd();
          const condition = r < 0.08 ? 'red' : r < 0.22 ? 'yellow' : 'green';
          fleet.push({
            unitId: `U-${String(idx).padStart(5, '0')}`,
            kind: node.kind,
            shortName: node.kind.split('—')[0].trim(),
            nodeType: node.nodeType,
            condition,
            subunit,
            parentId,
            band: null,
            costDeploy: { cp: 30, relay: 18, radio: 6, sat: 60, observer: 4, ew_jam: 25, ew_rer: 20, ew_rlr: 25 }[node.nodeType] ?? 10,
            deployTimeMin: { cp: 45, relay: 25, radio: 10, sat: 60, observer: 15, ew_jam: 30, ew_rer: 20, ew_rlr: 30 }[node.nodeType] ?? 20,
            powerW: Math.round(200 + rnd() * 900),
            note: condition === 'red' ? 'в ремонте: отказ приёмопередатчика' : condition === 'yellow' ? 'требуется ЗИП: блок питания' : '',
          });
        }
        return;
      }
      for (const c of node.children || []) walk(c, node.name, node.type === 'unit' ? null : node.name);
    };
    for (const root of ORG_TEMPLATE) walk(root, root.name, null);
    return fleet;
  }

  function buildDemoTree(fleet) {
    const byKey = new Map();
    for (const u of fleet) {
      const k = `${u.subunit}|${u.kind}`;
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k).push(u);
    }
    const walk = (node) => {
      if (node.type === 'vehicle') return null;
      const out = { id: `org-${node.name}`, name: node.name, type: node.type === 'unit' ? 'unit' : 'squad', children: [] };
      for (const c of node.children || []) {
        if (c.type === 'vehicle') {
          const key = `${node.name}|${c.kind}`;
          const units = byKey.get(key) || [];
          for (const u of units) out.children.push({ id: `veh-${u.unitId}`, type: 'vehicle', name: `${u.kind} · ${u.unitId}`, unit: u });
        } else out.children.push(walk(c));
      }
      return out;
    };
    return ORG_TEMPLATE.map((r) => walk(r)).filter(Boolean);
  }

  function rebuildTreeFromFleet() { state.oushTree = buildDemoTree(state.fleet); }

  /**
   * Стартовое размещение: штатные позиции вокруг КП (демонстрация drag-and-drop
   * результата, а не замена ручного размещения).
   */
  function buildDemoPlacement() {
    const origin = { lat: 55.75, lng: 37.62 };
    const layout = [
      { t: 'cp', dx: 0, dy: 0 },
      { t: 'relay', dx: 6.5, dy: 4.2 }, { t: 'relay', dx: -7.5, dy: -5.0 },
      { t: 'radio', dx: -9.2, dy: 6.4 }, { t: 'radio', dx: 9.6, dy: -7.1 },
      { t: 'radio', dx: -3.5, dy: -11.5 }, { t: 'radio', dx: 12.5, dy: 6.0 },
      { t: 'sat', dx: 1.8, dy: -3.0 },
      { t: 'observer', dx: 15.0, dy: 10.5 }, { t: 'observer', dx: -14.5, dy: -3.5 },
    ];
    const used = new Set();
    for (const L of layout) {
      const unit = state.fleet.find((u) => u.nodeType === L.t && !used.has(u.unitId) && u.condition !== 'red');
      if (!unit) continue;
      used.add(unit.unitId);
      const p = { lat: origin.lat + L.dy / 111.32, lng: origin.lng + L.dx / (111.32 * Math.cos(origin.lat * DEG)) };
      placeUnit(unit, p.lat, p.lng, { quiet: true });
    }
    // узел связи из КП-группы (FR-MAP-04)
    const cp = state.objects.find((o) => o.nodeType === 'cp');
    const near = state.objects.filter((o) => o !== cp && haversineKm(cp, o) < 5).slice(0, 3);
    if (cp && near.length >= 2) makeNodeGroupQuiet([cp.id, ...near.map((o) => o.id)]);
    // линии связи (FR-MAP-06)
    const ids = state.objects.map((o) => o.id);
    const hub = state.objects.find((o) => o.kind === 'nodeGroup') || cp;
    for (const o of state.objects) {
      if (!hub || o.id === hub.id || o.groupId === hub.id) continue;
      const d = haversineKm(hub, o);
      if (d <= 95) addLineQuiet(hub.id, o.id);
    }
    // заявки
    state.demands = [];
    const nonHub = state.objects.filter((o) => o !== hub && o.groupId !== hub?.id);
    nonHub.forEach((o, i) => {
      state.demands.push({ id: `q${i + 1}`, from: hub ? hub.id : ids[0], to: o.id, rateKbps: [8, 16, 16, 32, 64][i % 5], maxDelayMs: 250, priority: 1 + (i % 4), protected: i % 4 === 0 });
    });
    void ids;
  }
  function makeNodeGroupQuiet(ids) {
    const members = ids.map((id) => objById(id)).filter(Boolean).filter((o) => o.kind === 'asset');
    if (members.length < 2) return;
    const c = centroid(members);
    const main = members[0];
    const g = { id: nextId('us', 'grp'), kind: 'nodeGroup', nodeType: main.nodeType, name: `Узел связи ${state.seq.grp}`, lat: c.lat, lng: c.lng, parentId: main.parentId || null, unitSub: main.unitSub || null, condition: 'green', spec: main.spec, status: 'active', isGroup: true };
    state.objects.push(g);
    for (const m of members) m.groupId = g.id;
    for (const l of state.lines) {
      if (ids.includes(l.from) && ids.includes(l.to)) l.internal = true;
      if (ids.includes(l.from)) l.from = g.id;
      if (ids.includes(l.to)) l.to = g.id;
    }
    state.lines = state.lines.filter((l) => !l.internal && l.from !== l.to);
  }
  function addLineQuiet(a, b) {
    if (a === b) return;
    if (state.lines.some((l) => (l.from === a && l.to === b) || (l.from === b && l.to === a))) return;
    const oa = objById(a), ob = objById(b);
    if (!oa || !ob) return;
    const dKm = haversineKm(oa, ob);
    const type = dKm <= 12 ? 'FOC' : dKm <= 25 ? 'VHF' : dKm <= 45 ? 'UHF' : dKm <= 90 ? 'RRL' : 'SAT';
    state.lines.push({ id: nextId('ln', 'line'), from: a, to: b, type, priority: 2, isBackup: false });
  }

  function showHelp() {
    const rows = [
      ['FR-UI-01', 'Рабочая область: карта ≈80 % + правая панель ≈20 % (сворачивается кнопкой «⇥ Панель»)'],
      ['FR-RES-01/02', 'Панель ОШС: Рота → Взвод → Единица техники → Оборудование (раскрытие кликом)'],
      ['FR-RES-03', 'Индикатор состояния: 🟢 исправна / 🟡 ограниченно пригодна / 🔴 неисправна. Красная техника не перетаскивается и исключается ядром'],
      ['FR-RES-04', 'Поиск и фильтры по диапазону (КВ/УКВ/РТ/спутниковая/ВОЛС) и состоянию'],
      ['FR-MAP-01/02/03', 'Drag-and-drop техники на геоподложку, «призрак» с тактическим знаком, жёсткая связь объекта с записью парка (unitId) и координатами'],
      ['FR-MAP-04/05', 'Группирование выделенных аппаратных в «Узел связи» — единый тактический знак; инструмент «⌘ Узел связи» или ПКМ'],
      ['FR-MAP-06', 'Инструмент «✎ Линия» (L) — соединение узлов/станций линиями связи'],
      ['FR-MAP-07', 'Генерализация по масштабу: z ≥ 12 — все единицы; 8…12 — узлы связи; < 8 — кластеры подразделений'],
      ['FR-MAP-08', 'Клик по генерализованному знаку — список вложенных объектов; двойной клик — раскрытие масштабом'],
      ['FR-CTX-01/02', 'ПКМ на объекте/линии/карте — контекстное меню; «Свойства» — карточка с вкладками: Состав оборудования / Личный состав / Техническое состояние (+ Расчёт ENG6)'],
      ['FR-INT-01/02/03', 'Изменения топологии → JSON-пакет (WGS-84, 6 знаков) → POST /api/v1/plan/sync|async → оверлеи покрытия и цветные линии качества'],
      ['NFR-01', 'Canvas2D + кэш слоя рельефа; счётчик FPS в правом верхнем углу (≥30 FPS при 1000 знаков)'],
      ['NFR-02', 'localStorage/sessionStorage/IndexedDB/cookie НЕ используются; внешние ресурсы не загружаются; координаты не кэшируются'],
      ['NFR-03', 'Координаты хранятся и передаются с точностью 6 знаков после запятой'],
    ];
    const w = el('div');
    w.appendChild(el('div', { class: 'card', html: '<h3>Назначение</h3><div style="font-size:11.5px;line-height:1.55;color:var(--dim)">Модуль «Визуальный картографический планировщик» — фронтенд headless-ядра ENG6-PLAN. Карта и панель ОШС являются «витриной»: все расчёты выполняет ядро по REST API, интерфейс лишь готовит входные данные и визуализирует результат (зоны покрытия, качество линий, маршруты, расписание развёртывания, живучесть).</div>' }));
    const tbl = el('table', { class: 'tbl' });
    tbl.appendChild(el('thead', { html: '<tr><th>Пункт ТЗ</th><th>Как реализовано</th></tr>' }));
    const tb = el('tbody');
    for (const [k, v] of rows) tb.appendChild(el('tr', { html: `<td style="white-space:nowrap;color:var(--accent2)">${esc(k)}</td><td style="font-family:inherit">${esc(v)}</td>` }));
    tbl.appendChild(tb);
    w.appendChild(el('div', { class: 'card' }, [el('h3', { text: 'Соответствие Дополнению к ТЗ №1' }), tbl]));
    w.appendChild(el('div', { class: 'card', html: `<h3>Горячие клавиши</h3><dl class="kv">
      <dt>V / L / M / N</dt><dd>выбор · линия · измерение · узел связи</dd>
      <dt>F</dt><dd>вписать все объекты</dd>
      <dt>Ctrl+R</dt><dd>запуск расчёта</dd>
      <dt>Ctrl+A / Del</dt><dd>выделить все / удалить</dd>
      <dt>Enter</dt><dd>карточка выбранного объекта</dd>
      <dt>Esc</dt><dd>сброс инструмента, закрытие окон</dd>
      <dt>Alt+ЛКМ / колесо</dt><dd>панорамирование / масштаб</dd>
    </dl>` }));
    openModal('Справка · Дополнение к ТЗ №1 (ГОСТ 34.602)', [{ label: 'Требования', render: () => w }], 0, [el('button', { class: 'tb-btn accent', text: 'Закрыть', onclick: closeModal })]);
  }

  function bindUi() {
    $('#btnPanel').addEventListener('click', () => {
      const rp = $('#rightpanel');
      rp.classList.toggle('collapsed');
      $('#mapzone').classList.toggle('collapsed', rp.classList.contains('collapsed'));
      setTimeout(() => { resize(); }, 60);
    });
    $('#btnHelp').addEventListener('click', showHelp);
    $('#btnCalc').addEventListener('click', () => runCalc({}));
    $('#btnCalcAsync').addEventListener('click', runAsync);
    $('#btnExport').addEventListener('click', exportScenario);
    $('#btnImport').addEventListener('click', () => $('#fileInput').click());
    $('#fileInput').addEventListener('change', (ev) => { const f = ev.target.files && ev.target.files[0]; if (f) importFile(f); ev.target.value = ''; });
    $('#btnNew').addEventListener('click', () => {
      if (!confirm('Очистить рабочее пространство? Размещённые объекты и линии будут удалены (парк техники сохранится).')) return;
      state.objects = []; state.lines = []; state.demands = []; state.selection.clear(); state.lastResult = null;
      state.linkById = new Map(); state.nodeById = new Map();
      renderTree(); renderResults({ status: '—', metrics: {}, plan: {}, diagnostics: {} }, { mode: 'reset' });
      invalidate(); logMsg('warn', 'Рабочее пространство очищено');
    });
    $('#btnAutoPlace').addEventListener('click', () => autoPlace());
    $$('.tb-btn.tool').forEach((b) => b.addEventListener('click', () => setTool(b.dataset.tool)));
    $$('.rpt').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
    ['#resSearch', '#fltBand', '#fltCond'].forEach((s) => $(s).addEventListener('input', renderTree));
    ['#lyBase', '#lyCoverage', '#lyLinks', '#lyRejected', '#lyLabels', '#lyGrid'].forEach((s) => {
      const key = s.slice(3, 4).toLowerCase() + s.slice(4);
      $(s).addEventListener('change', (ev) => { state.layers[key] = ev.target.checked; if (key === 'base') terrainKey = ''; invalidate(); });
    });
    $$('#zoomCtl button').forEach((b) => b.addEventListener('click', () => {
      const z = Number(b.dataset.z);
      if (z === 0) fitToObjects();
      else zoomAt(z > 0 ? 1.4 : 1 / 1.4, vw / 2, vh / 2);
      drawScaleBar(); updateStatusbar();
    }));
    $('#btnLogClear').addEventListener('click', () => { state.log = []; renderLog(); });
    $('#modalClose').addEventListener('click', closeModal);
    $('#modal').addEventListener('mousedown', (ev) => { if (ev.target.id === 'modal') closeModal(); });
    window.addEventListener('resize', () => { resize(); drawScaleBar(); });
    // отчёт о состоянии ядра при старте
    (async () => {
      try {
        const r = await api('GET', '/meta');
        if (r.status === 200 && r.data && r.data.core) {
          setConn('ok', `ядро ENG6 ${r.data.core.version} · planner ${r.data.planner}`);
          $('#connState').title = `sha256 ядра: ${r.data.core.sha256}\nлимиты: узлов ${r.data.apiLimits?.maxNodes}, рёбер ${r.data.apiLimits?.maxLinks}\nворкеров: ${r.data.pool?.workers}, очередь: ${r.data.pool?.maxQueue}`;
          logMsg('ok', `Соединение с ядром установлено: ENG6 ${r.data.core.version} (sha256 ${String(r.data.core.sha256).slice(0, 12)}…), воркеров ${r.data.pool?.workers}`);
        } else setConn('warn', 'ядро: неожиданный ответ');
      } catch (err) {
        setConn('bad', 'ядро: нет связи');
        logMsg('err', `API недоступен (${apiBase()}): ${err.message}`);
        if (state.demoResult) {
          applyPlanToState(state, state.demoResult);
          renderResults(state.demoResult, { mode: 'demo' });
          setConn('warn', 'ядро: демо-результат');
          toast('Работа без ядра', 'API недоступен — загружен встроенный демонстрационный результат расчёта ENG6-PLAN. Укажите адрес сервиса в поле «Ядро» и нажмите «▶ Расчёт».', 'warn', 14000);
        }
      }
    })();
  }

  function init() {
    // демо-результат расчёта (встроен при сборке scripts/build_frontend.mjs)

    resize();
    bindMapEvents();
    bindUi();
    const demoScenario = (function () {
      try {
        const raw = document.getElementById('demo-data').textContent.trim();
        if (raw && !raw.startsWith('/*__')) return JSON.parse(raw);
      } catch (err) { logMsg('warn', `Демо-сценарий не прочитан: ${err.message}`); }
      return null;
    })();
    if (demoScenario && (demoScenario.request || demoScenario.topology)) {
      // встроенный сценарий + готовый результат расчёта ядра (сборка offline)
      applyScenarioPayload(demoScenario);
    } else {
      state.fleet = buildDemoFleet();
      rebuildTreeFromFleet();
      buildDemoPlacement();
      fitToObjects();
      renderTree();
    }
    renderTree();
    drawScaleBar();
    renderLegend(null);
    updateStatusbar();
    setTool('select');
    logMsg('ok', `Инициализация: парк ${state.fleet.length} ед., размещено ${state.objects.length}, линий ${state.lines.length}, заявок ${state.demands.length}`);
    logMsg('info', 'NFR-02: локальное хранилище браузера не используется — состояние только в памяти вкладки');
    if (demoScenario && demoScenario.result) {
      state.demoResult = demoScenario.result;
      applyPlanToState(state, demoScenario.result);
      renderResults(demoScenario.result, { mode: 'demo' });
      setConn('warn', 'ядро: демо-результат');
      switchTab('osh');
      logMsg('ok', `Загружен встроенный результат расчёта: ${demoScenario.result.status}, ${demoScenario.result.plan?.activeLinksTotal ?? 0} активных линий`);
    }
    invalidate();
    loop();
    // контроль NFR-01: предупреждение при деградации ниже 30 FPS
    setInterval(() => {
      if (fps > 0 && fps < 30 && drawStats.signs > 50) {
        logMsg('warn', `NFR-01: FPS=${fps} при ${drawStats.signs} знаках (кадр ${nf(drawStats.ms, 1)} мс) — рекомендуется укрупнить масштаб или отключить слои`);
      }
    }, 15000);
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }
})();
