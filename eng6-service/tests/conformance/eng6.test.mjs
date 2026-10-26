/**
 * tests/conformance/eng6.test.mjs — конформанс вендорного ядра ENG6.
 *
 * Это ПРИЁМОЧНЫЙ тест изделия: он обязан выполняться при любой пересборке,
 * замене вендора или обновлении Node.js. Проверяются:
 *   1) контрольная сумма вендорного файла (защита от незаметной подмены);
 *   2) встроенный selfTest ENG6 — 33 эталонные проверки (FSPL, Q-функция,
 *      Шеннон, BER, дифракция J(v), зона Френеля, Hata, дождь ITU-R P.838,
 *      наклонная трасса ГСО, ВОЛС, нормальное распределение);
 *   3) 5 регрессионных сценариев ENG6 (батальон, бригада, спутниковый резерв,
 *      ВОЛС, деградация при отказах) — связность, рейтинг, число ЕТО;
 *   4) полнота публичной поверхности (69 ключей фасада);
 *   5) детерминированность: один и тот же сценарий → идентичный результат.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { loadEng6, conformance, VENDOR_PATH } from '../../core/eng6.mjs';

const core = loadEng6();
const E = core.ENG6;

test('Контрольная сумма вендора совпадает с зафиксированной в репозитории', () => {
  const expected = readFileSync(new URL('../../core/vendor/VENDOR_SHA256.txt', import.meta.url), 'utf8').trim().split(/\s+/)[0];
  const actual = createHash('sha256').update(readFileSync(VENDOR_PATH)).digest('hex');
  assert.equal(actual, expected, 'вендорный файл ENG6 изменён — требуется повторный конформанс и запись в docs/06_DEFECTS.md');
  assert.equal(core.sha256, actual);
  assert.match(actual, /^[0-9a-f]{64}$/);
});

test('ENG6 selfTest: 33 эталонные проверки, 0 расхождений', () => {
  const st = E.selfTest();
  assert.equal(st.length, 33, `число проверок изменилось: ${st.length}`);
  const failed = st.filter((t) => !t.ok);
  // массивы приходят из V8-контекста вендора (другой realm) — сравнение через JSON
  assert.equal(JSON.stringify(failed.map((f) => ({ name: f.name, got: f.got, want: f.want }))), '[]', 'расхождения с эталоном');
  // выборочный контроль значений, важных для планировщика
  const byName = (frag) => st.find((t) => t.name.includes(frag));
  assert.ok(Math.abs(byName('FSPL 1 км').got - 75.96) < 0.05);
  assert.ok(Math.abs(byName('Шеннон 25 кГц').got - 166.46) < 1);
  assert.ok(Math.abs(byName('Q(0)').got - 0.5) < 1e-6);
});

test('ENG6 runRegression: 5 сценариев, все пройдены', () => {
  const rg = E.runRegression();
  assert.equal(rg.length, 5);
  const failed = rg.filter((r) => !r.passed);
  assert.equal(JSON.stringify(failed.map((f) => ({ caseId: f.caseId, problems: f.problems }))), '[]');
  assert.equal(JSON.stringify(rg.map((r) => r.caseId)), JSON.stringify(['rg1', 'rg2', 'rg3', 'rg4', 'rg5']));
});

test('conformance(): сводный протокол ok=true', () => {
  const c = conformance();
  assert.equal(c.ok, true, JSON.stringify({ self: c.selfTestFailed, reg: c.regressionFailed }));
  assert.equal(c.selfTestTotal, 33);
  assert.equal(c.regressionTotal, 5);
  assert.equal(c.selfTestFailed.length, 0);
  assert.equal(c.regressionFailed.length, 0);
});

test('Публичная поверхность ядра: 69 ключей, ключевые функции на месте', () => {
  assert.equal(core.keys.length, 69);
  const required = [
    'analyzeScenario', 'analyzeCore', 'calcLink', 'validateScenario', 'validateNode', 'validateLink',
    'defaultScenario', 'cloneSc', 'buildTemplate', 'scenarioChecksum', 'selfTest', 'runRegression',
    'optimizePlacement', 'sampleGrid', 'recommendations', 'noiseDbm', 'shannonKbps', 'practicalRate',
    'berCoded', 'berUncoded', 'fecRate', 'buildModelChain', 'createMC', 'evalRelay', 'collectJammers',
    'PROP', 'TERR', 'NET', 'CLUTTER', 'MODS', 'FECS', 'LINK_TYPES', 'NODE_TYPES', 'NODE_PRESETS',
    'LINK_PRESETS', 'TEMPLATES', 'MLIB', 'SAT', 'HF', 'FIBER', 'EXTREME', 'MISSION', 'ENGAGEM', 'THREATLIB',
  ];
  for (const k of required) assert.ok(k in E, `в фасаде ENG6 отсутствует ключ «${k}»`);
  assert.equal(E.version, '5.1.0');
  assert.equal(core.meta.stage, 'E5');
  assert.equal(core.meta.release, '7.0');
  assert.match(core.meta.legacyCoreSha256, /^[0-9a-f]{64}$/);
});

test('Математические примитивы ENG6 соответствуют эталонным формулам', () => {
  // FSPL = 32.44 + 20·lg(F[МГц]) + 20·lg(d[км])
  const fspl = (f, d) => 32.44 + 20 * Math.log10(f) + 20 * Math.log10(d);
  for (const [f, d] of [[150, 1], [150, 10], [450, 5], [7000, 30], [12000, 38000]]) {
    assert.ok(Math.abs(E.PROP.fspl(d, f) - fspl(f, d)) < 1e-9, `FSPL(${f} МГц, ${d} км)`);
  }
  // N = −174 + 10·lg(B[Гц]) + NF
  for (const [bw, nf] of [[25, 6], [8000, 5], [64, 1.2]]) {
    const want = -174 + 10 * Math.log10(bw * 1000) + nf;
    assert.ok(Math.abs(E.noiseDbm(bw, nf) - want) < 1e-9, `noiseDbm(${bw}, ${nf})`);
  }
  // Шеннон: C = B·log2(1+SNR)
  for (const [bw, snr] of [[25, 20], [8000, 30], [64, 10]]) {
    const want = snr < -5 ? 0 : bw * Math.log2(1 + 10 ** (snr / 10));
    assert.ok(Math.abs(E.shannonKbps(bw, snr) - want) < 1e-9, `shannonKbps(${bw}, ${snr})`);
  }
  // BER BPSK = Q(sqrt(2·SNR)): при SNR = 0 дБ → Q(√2) ≈ 0,0786
  const qRef = (x) => 0.5 * erfc(x / Math.SQRT2);
  const erfc = (z) => {
    const t = Math.abs(z), n = 2 / (2 + t);
    const v = n * Math.exp(-t * t - 1.26551223 + n * (1.00002368 + n * (0.37409196 + n * (0.09678418 + n * (-0.18628806 + n * (0.27886807 + n * (-1.13520398 + n * (1.48851587 + n * (-0.82215223 + n * 0.17087277)))))))));
    return z >= 0 ? v : 2 - v;
  };
  assert.ok(Math.abs(E.berUncoded('BPSK', 0) - qRef(Math.SQRT2)) < 1e-6, `BER BPSK(0 дБ) = ${E.berUncoded('BPSK', 0)}`);
  // при очень высоком SNR BER стремится к нулю, при отрицательном — к 0.5
  assert.ok(E.berUncoded('BPSK', 20) < 1e-8);
  assert.ok(E.berUncoded('QPSK', 0) > 0.05 && E.berUncoded('QPSK', 0) < 0.12);
  // 16QAM требует большего SNR при той же BER
  assert.ok(E.berUncoded('16QAM', 15) > E.berUncoded('BPSK', 15));
  // Зона Френеля: r1 = sqrt(λ·d1·d2/(d1+d2))
  const r = E.PROP.fresnelR(5, 5, 150);
  const want = Math.sqrt((300 / 150) * 5000 * 5000 / 10000);
  assert.ok(Math.abs(r - want) < 1e-6, `fresnelR: ${r} vs ${want}`);
  // Дифракция: J(−1) = 0 (просвет больше −0,78 v)
  assert.equal(E.PROP.knifeEdgeJ(-1), 0);
  assert.ok(Math.abs(E.PROP.knifeEdgeJ(0) - 6.03) < 0.35, 'J(0) ≈ 6 дБ');
  // ГСО: наклонная дальность при 30° ≈ 38 600 км
  assert.ok(Math.abs(E.SAT.slantKm(30, 35786) - 38600) < 200);
  // ВОЛС: 100 км × 0,22 дБ/км + муфты
  const fl = E.FIBER.loss({ attDbKm: 0.22, spliceDb: 0.05, connDb: 0.3 }, 100);
  assert.ok(Math.abs(fl - (22 + 25 * 0.05 + 0.6)) < 1e-9, `ВОЛС 100 км: ${fl}`);
  assert.ok(Math.abs(E.FIBER.delayMs(100) - 491) < 1e-9, 'задержка ВОЛС 4,9 мкс/м');
});

test('Детерминированность ENG6: один сценарий → идентичный результат', () => {
  const sc = E.buildTemplate('brigade', { lat: 55.75, lng: 37.62 });
  const base = E.defaultScenario('determinism');
  base.nodes = JSON.parse(JSON.stringify(sc.nodes));
  base.links = JSON.parse(JSON.stringify(sc.links));
  base.threats = [];
  const strip = (a) => {
    const c = JSON.parse(JSON.stringify(a));
    delete c.timingMs;
    if (Array.isArray(c.assumptions)) c.assumptions = c.assumptions.map((x, i) => (i === 0 ? 'ts' : x));
    return JSON.stringify(c);
  };
  const a = strip(E.analyzeScenario(base, { quality: 'balanced' }));
  const b = strip(E.analyzeScenario(JSON.parse(JSON.stringify(base)), { quality: 'balanced' }));
  assert.equal(a, b, 'результаты анализа различаются при идентичном входе');
});

test('Контрольная сумма сценария (scenarioChecksum) устойчива к метаданным времени', () => {
  const a = E.defaultScenario('x');
  const b = E.defaultScenario('x');
  // meta.id у defaultScenario содержит случайный суффикс (uidSfx) — нормализуем,
  // иначе сумма различается по не относящейся к делу причине
  a.meta.id = 'sc-fixed';
  b.meta.id = 'sc-fixed';
  a.meta.datetime = '2026-01-16T12:00';
  b.meta.datetime = '2026-01-16T12:00';
  a.meta.createdAt = '2020-01-01T00:00:00.000Z';
  b.meta.createdAt = '2030-01-01T00:00:00.000Z';
  a.meta.updatedAt = '2020-01-01T00:00:00.000Z';
  b.meta.updatedAt = '2030-06-01T00:00:00.000Z';
  assert.equal(E.scenarioChecksum(a), E.scenarioChecksum(b), 'createdAt/updatedAt не должны влиять на контрольную сумму');
  b.environment.miscLossDb = 9;
  assert.notEqual(E.scenarioChecksum(a), E.scenarioChecksum(b), 'изменение параметров меняет сумму');
});

test('MLIB: каталог моделей доступен и самодостаточен (151 модель)', () => {
  assert.ok(Array.isArray(E.MLIB.ALL_MODELS));
  assert.equal(E.MLIB.ALL_MODELS.length, 151);
  const ids = new Set(E.MLIB.ALL_MODELS.map((m) => m.id));
  assert.equal(ids.size, E.MLIB.ALL_MODELS.length, 'идентификаторы моделей уникальны');
  for (const m of E.MLIB.ALL_MODELS.slice(0, 20)) {
    assert.ok(m.id && m.name && m.stage, `модель без метаданных: ${JSON.stringify(m).slice(0, 80)}`);
  }
});
