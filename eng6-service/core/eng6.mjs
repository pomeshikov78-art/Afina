/**
 * core/eng6.mjs — загрузчик нативного ядра ENG6 (вендор, дословно из изделия v8.0).
 *
 * Принцип «Айтишника»: ядро изолировано. Вендорный код исполняется в отдельном
 * V8-контексте (node:vm) без доступа к DOM, сети, ФС, localStorage и IndexedDB.
 * Любая попытка обращения к браузерным API приводит к исключению
 * ENG6_ISOLATION_VIOLATION — это tripwire, а не «тихая» деградация.
 *
 * Единственная разрешённая «нестандартная» подсистема ENG6 — кэш DEM (indexedDB)
 * и онлайн-рельеф (fetch). Планировщик ENG6-PLAN всегда использует
 * environment.terrain.source = 'synth' | 'flat', поэтому эти ветви недостижимы;
 * тем не менее indexedDB выставлен в undefined (вендор проверяет typeof),
 * а fetch/document/Image — в бросающие геттеры.
 */
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const VENDOR_PATH = path.join(__dirname, 'vendor', 'eng6_core.js');

let CACHED = null;

/**
 * Загружает ENG6 один раз на процесс (модуль тяжёлый — 677 КБ исходника).
 * @returns {{ENG6: object, meta: object, sha256: string, bytes: number, loadMs: number}}
 */
export function loadEng6({ force = false } = {}) {
  if (CACHED && !force) return CACHED;
  const t0 = process.hrtime.bigint();
  const src = readFileSync(VENDOR_PATH, 'utf8');
  const sha256 = createHash('sha256').update(readFileSync(VENDOR_PATH)).digest('hex');

  const sandbox = {
    // вендор обращается к window/self только для публикации фасада __ENG6__
    indexedDB: undefined,
    __ISOLATION_LOG__: [],
  };
  const ctx = vm.createContext(sandbox, { codeGeneration: { strings: false, wasm: false } });

  // tripwire: любые браузерные/сетевые API внутри ядра запрещены
  vm.runInContext(
    `
    var window = globalThis;
    var self = globalThis;
    ['document','fetch','Image','XMLHttpRequest','WebSocket','localStorage','sessionStorage',
     'navigator','Worker','SharedWorker','ServiceWorker','caches','importScripts','eval'
    ].forEach(function (k) {
      try {
        Object.defineProperty(globalThis, k, {
          configurable: false,
          get: function () {
            throw new Error('ENG6_ISOLATION_VIOLATION: доступ к запрещённому API «' + k + '»');
          }
        });
      } catch (e) { /* eval уже определён — не критично */ }
    });
    `,
    ctx,
    { filename: 'eng6_isolation_shim.js' },
  );

  vm.runInContext(src, ctx, { filename: 'eng6_core.js', timeout: 30_000 });

  const ENG6 = vm.runInContext('window.__ENG6__', ctx);
  const META = vm.runInContext('window.__ENG6_META__ || null', ctx);
  if (!ENG6 || typeof ENG6.analyzeScenario !== 'function') {
    throw new Error('ENG6 не загрузился: фасад __ENG6__ отсутствует или неполон');
  }
  // ядро должно остаться внутри своего контекста — наружу отдаём freeze-копию фасада
  Object.freeze(ENG6);

  const loadMs = Number(process.hrtime.bigint() - t0) / 1e6;
  CACHED = Object.freeze({
    ENG6,
    meta: META ? { ...META } : null,
    sha256,
    bytes: Buffer.byteLength(src, 'utf8'),
    chars: src.length,
    keys: Object.keys(ENG6),
    loadMs,
    context: ctx,
  });
  return CACHED;
}

/** Быстрый доступ к фасаду ENG6. */
export function eng6() {
  return loadEng6().ENG6;
}

/**
 * Конформанс-протокол вендора: встроенный selfTest (эталонные значения
 * FSPL/Q-функция/Шеннон/BER/дифракция/Френель/Hata/дождь/ГСО) + 5 регрессионных
 * сценариев. Используется как приёмочный тест при любой пересборке изделия.
 */
export function conformance() {
  const E = eng6();
  const selfTest = E.selfTest();
  const failedSelf = selfTest.filter((t) => !t.ok);
  const regression = E.runRegression();
  const failedReg = regression.filter((r) => !r.passed);
  return {
    ok: failedSelf.length === 0 && failedReg.length === 0,
    selfTestTotal: selfTest.length,
    selfTestFailed: failedSelf.map((f) => ({ name: f.name, got: f.got, want: f.want })),
    regressionTotal: regression.length,
    regressionFailed: failedReg.map((f) => ({ caseId: f.caseId, name: f.name, problems: f.problems })),
    regression: regression.map((r) => ({ caseId: r.caseId, name: r.name, passed: r.passed })),
  };
}
