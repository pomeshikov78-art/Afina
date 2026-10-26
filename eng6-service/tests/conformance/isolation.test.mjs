/**
 * tests/conformance/isolation.test.mjs — проверка «изолированности ядра».
 *
 * Требование «Айтишника»: ядро — чистая функция Input Data → Math → Output Data,
 * без зависимостей от БД, сети, файловой системы и окружения.
 *
 * Проверяется статически (сканирование исходников) и динамически (tripwire
 * в V8-контексте вендора + отсутствие побочных эффектов plan()).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import vm0 from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { plan } from '../../core/planner.mjs';
import { loadEng6 } from '../../core/eng6.mjs';
import { battalionScenario } from '../../scripts/gen_topology.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const coreDir = path.join(root, 'core');

function coreFiles(dir = coreDir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) out.push(...coreFiles(p));
    else if (p.endsWith('.mjs') || (p.endsWith('.js') && !p.includes('vendor'))) out.push(p);
  }
  return out;
}

/** Разрешённые встроенные модули Node в ядре: только то, что не даёт side-effects. */
const ALLOWED_IMPORTS = new Set(['node:vm', 'node:fs', 'node:path', 'node:url', 'node:crypto', 'node:module', 'node:os']);
const FORBIDDEN_IMPORTS = ['node:net', 'node:http', 'node:https', 'node:dgram', 'node:tls', 'node:child_process', 'node:worker_threads', 'node:sqlite', 'pg', 'mysql', 'redis', 'mongodb', 'amqplib', 'kafkajs', 'axios', 'node-fetch', 'undici'];

test('Ядро не импортирует сетевые,процессныеные и БД-модули', () => {
  const files = coreFiles();
  assert.ok(files.length >= 10, `подозрительно мало файлов ядра: ${files.length}`);
  const violations = [];
  for (const f of files) {
    const src = readFileSync(f, 'utf8');
    const imports = [...src.matchAll(/(?:^|\n)\s*import\s+(?:[\s\S]*?)\s+from\s+['"]([^'"]+)['"]/g)].map((m) => m[1]);
    const dyn = [...src.matchAll(/import\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]);
    for (const mod of [...imports, ...dyn]) {
      if (FORBIDDEN_IMPORTS.some((b) => mod === b || mod.startsWith(b + '/'))) violations.push(`${path.relative(root, f)} → ${mod}`);
      else if (mod.startsWith('node:') && !ALLOWED_IMPORTS.has(mod)) violations.push(`${path.relative(root, f)} → ${mod} (не в белом списке)`);
      else if (!mod.startsWith('node:') && !mod.startsWith('.') && !mod.startsWith('/')) violations.push(`${path.relative(root, f)} → ${mod} (сторонний пакет)`);
    }
  }
  assert.deepEqual(violations, [], 'нарушения изоляции ядра');
});

test('Ядро не обращается к глобальному состоянию процесса и окружению', () => {
  const files = coreFiles();
  const bad = [];
  for (const f of files) {
    const src = readFileSync(f, 'utf8');
    // process.env допустим только для флага отладки ENG6_STRICT
    for (const m of src.matchAll(/process\.env\.([A-Z0-9_]+)/g)) {
      if (m[1] !== 'ENG6_STRICT') bad.push(`${path.relative(root, f)} → process.env.${m[1]}`);
    }
    // core/eng6.mjs — модуль, КОТОРЫЙ ЗАПРЕЩАЕТ эти API (tripwire-список),
    // поэтому упоминания имён в нём являются частью защиты, а не использованием
    const isGuard = path.basename(f) === 'eng6.mjs';
    for (const pat of ['fetch(', 'XMLHttpRequest', 'require(']) {
      const lines = src.split('\n').filter((l) => l.includes(pat) && !/^\s*(\*|\/\/|\/\*)/.test(l.trim()) && !(isGuard && /ENG6_ISOLATION_VIOLATION|'document','fetch'/.test(l)));
      for (const l of lines) bad.push(`${path.relative(root, f)} → ${pat}: ${l.trim().slice(0, 90)}`);
    }
  }
  assert.deepEqual(bad, [], 'ядро использует внешние ресурсы');
});

test('Запись на диск в ядре отсутствует (node:fs — только чтение вендора)', () => {
  for (const f of coreFiles()) {
    const src = readFileSync(f, 'utf8');
    if (!/from 'node:fs'/.test(src)) continue;
    const rel = path.relative(root, f);
    assert.equal(rel, path.join('core', 'eng6.mjs'), `fs импортируется в ${rel}`);
    assert.ok(/readFileSync/.test(src), 'eng6.mjs читает вендор');
    assert.ok(!/writeFileSync|appendFile|createWriteStream|mkdir|unlink/.test(src), 'в ядре найдена запись на диск');
  }
});

test('Вендор ENG6 исполняется в изолированном V8-контексте с tripwire на браузерные API', () => {
  const core = loadEng6();
  assert.ok(core.context, 'контекст не сохранён');
  const vm = vm0;
  // обращение к запрещённому API внутри контекста ядра → исключение
  for (const api of ['document', 'fetch', 'localStorage', 'XMLHttpRequest', 'WebSocket', 'navigator']) {
    let threw = null;
    try {
      vm.runInContext(`(${api})`, core.context);
    } catch (err) {
      threw = err;
    }
    assert.ok(threw, `${api} доступен внутри ядра — изоляция нарушена`);
    assert.match(String(threw.message), /ENG6_ISOLATION_VIOLATION/, `${api}: ${threw.message}`);
  }
  // indexedDB определён как undefined (вендор проверяет typeof) — но не доступен как объект
  assert.equal(vm.runInContext('typeof indexedDB', core.context), 'undefined');
  // фасад ядра заморожен
  assert.ok(Object.isFrozen(core.ENG6));
});

test('plan() не мутирует входной запрос (чистая функция)', () => {
  const req = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  req.constraints.eng6Analysis = false;
  const before = JSON.stringify(req);
  const frozen = JSON.parse(before);
  const r1 = plan(req);
  assert.equal(JSON.stringify(req), before, 'входной запрос изменён ядром');
  const r2 = plan(frozen);
  const strip = (x) => {
    const c = JSON.parse(JSON.stringify(x));
    delete c.timings;
    return JSON.stringify(c);
  };
  assert.equal(strip(r1), strip(r2), 'результат зависит от истории вызовов');
});

test('plan() не оставляет глобального состояния между вызовами', () => {
  const before = Object.keys(globalThis).length;
  for (let i = 0; i < 5; i++) {
    const req = battalionScenario({ origin: { lat: 55.7 + i * 0.01, lng: 37.62 } });
    req.constraints.eng6Analysis = false;
    plan(req);
  }
  const after = Object.keys(globalThis).length;
  assert.ok(after - before <= 1, `глобальное пространство загрязнено: +${after - before} ключей`);
  // window внутри основного контекста не появляется
  assert.equal(typeof globalThis.window, 'undefined', 'ядро не должно публиковать window в основном контексте');
});

test('Результат plan() сериализуется в JSON без потерь (нет циклов и функций)', () => {
  const req = battalionScenario({ origin: { lat: 55.75, lng: 37.62 } });
  const r = plan(req);
  const json = JSON.stringify(r);
  const back = JSON.parse(json);
  assert.equal(back.status, r.status);
  assert.equal(back.metrics.linksActive, r.metrics.linksActive);
  // отсутствие undefined-значений, ломающих контракт
  assert.ok(!json.includes('undefined'), 'в JSON попали undefined');
});
