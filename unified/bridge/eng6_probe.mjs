/**
 * unified/bridge/eng6_probe.mjs — зонд прямого доступа к вендору ENG6 v5.1.0.
 *
 * Назначение (шаг 6 плана совмещения, ARC-002/XVER):
 *   вызывает примитивы ENG6.PROP.* напрямую (без сервера, без планировщика)
 *   и возвращает значения в Python-стенд кросс-верификации.
 *
 * Принцип изоляции соблюдается: вендор грузится штатным загрузчиком
 * eng6-service/core/eng6.mjs (V8-контекст с tripwire). Зонд только ЧИТАЕТ
 * значения примитивов; глобальное состояние не мутирует.
 *
 * Протокол: JSON в stdin → JSON в stdout.
 *   {"mode":"meta"}
 *   {"mode":"batch","points":[{"model":"fspl","f_mhz":900,"d_km":5}, …]}
 * Ответ:
 *   {"ok":true,"core":{…},"results":[{"ok":true,"model":"fspl","value":…} | {"ok":false,"error":…}]}
 *
 * Поддерживаемые model:
 *   fspl(f_mhz,d_km) · hata(f_mhz,d_km,h_bs_m,h_ms_m,env) · cost231_hata(f_mhz,d_km,h_bs_m,h_ms_m)
 *   two_ray(d_km,h_bs_m,h_ms_m) · log_distance(d_km,f_mhz,n) · fresnel(d1_km,d2_km,f_ghz)
 *   noise_dbm(bw_khz,nf_db) — тепловой шум kTB (пара для RF-07, единицы ENG6: кГц)
 * Порядок аргументов ENG6 учтён внутри (PROP.fspl(d,f), PROP.hata(f,d,h1,h2,env),
 * PROP.fresnelR(d1,d2,f)). Значение — в поле "value" (единицы: дБ, кроме fresnel — метры·√1000,
 * см. документированное отклонение XVER-FRESNEL).
 */
import { loadEng6 } from '../../eng6-service/core/eng6.mjs';
import { canonicalHash } from '../../eng6-service/core/validate.mjs';
import { buildScenario, normalizeEnvironment } from '../../eng6-service/core/scenario.mjs';

// Широта узла B так, что haversine(R=6371) даёт ровно d_km от узла A (55.0, 37.0)
const KM_PER_DEG_LAT = (Math.PI * 6371) / 180; // 111.19492664455874

function calcLinkPoint(E, p) {
  const d = Number(p.d_km);
  const a = { id: 'xv-a', type: 'relay', status: 'active', lat: 55.0, lng: 37.0,
              txPowerDbm: Number(p.tx_dbm), txGainDbi: Number(p.gain_dbi), rxGainDbi: Number(p.gain_dbi),
              rxSensDbm: -110, antHeightM: 30, feederLossDb: 0, noiseFigureDb: Number(p.nf_db ?? 6) };
  const b = { id: 'xv-b', type: 'relay', status: 'active', lat: 55.0 + d / KM_PER_DEG_LAT, lng: 37.0,
              txPowerDbm: Number(p.tx_dbm), txGainDbi: Number(p.gain_dbi), rxGainDbi: Number(p.gain_dbi),
              rxSensDbm: -110, antHeightM: 30, feederLossDb: 0, noiseFigureDb: Number(p.nf_db ?? 6) };
  const links = [{ id: 'xv-l', from: 'xv-a', to: 'xv-b', type: 'RRL',
                   freqMhz: Number(p.f_mhz), bwKhz: Number(p.bw_khz ?? 25), mod: 'QPSK', fec: 'none' }];
  // среда: нулевые дождь/туман, open-clutter (loss=0), плоский рельеф, misc=0 —
  // остаётся только prop (+gas, который при f≤900 МГц равен 0 — см. XVER-GRID)
  const env = normalizeEnvironment({ clutterType: 'open', miscLossDb: 0,
    weather: { rainRateMmHr: 0, fogDensity: 0, temperatureC: 15, humidityPercent: 60 },
    terrain: { source: 'flat' } });
  const out = buildScenario({ nodes: [a, b], links, environment: env, meta: { id: 'xver-calc-link' }, quality: 'balanced' });
  const r = E.calcLink(out.scenario.links[0], out.scenario, { quality: 'balanced', miscLossDb: 0 });
  if (r.status !== 'ok') throw new Error('calcLink status=' + r.status);
  let prop = 0;
  for (const k of Object.keys(r.breakdown || {})) {
    const v = (r.breakdown || {})[k];
    if (typeof v === 'number' && Number.isFinite(v) && k === 'prop') prop = v;
  }
  return { prop, rx: r.rxPowerDbm, noise: r.noiseDbm, distKm: r.distKm, model: r.model };
}

function readStdin() {
  return new Promise((resolve) => {
    let buf = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => { buf += c; });
    process.stdin.on('end', () => resolve(buf));
  });
}

function evaluate(E, p) {
  const f = Number(p.f_mhz);
  const d = Number(p.d_km);
  const h1 = Number(p.h_bs_m ?? 30);
  const h2 = Number(p.h_ms_m ?? 2);
  switch (p.model) {
    case 'fspl':
      // ENG6: PROP.fspl(d_km, f_mhz)
      return E.PROP.fspl(d, f);
    case 'hata':
      // ENG6: PROP.hata(f_mhz, d_km, hTx_m, hRx_m, env); env≠'open' трактуется вендором как urban
      return E.PROP.hata(f, d, h1, h2, p.env || 'open');
    case 'cost231_hata':
      // ENG6: PROP.cost231(f,d,h1,h2[,env]) — при переданном env всегда добавляет CM=+3 дБ
      return E.PROP.cost231(f, d, h1, h2, p.env || 'urban');
    case 'two_ray':
      return E.PROP.twoRay(d, h1, h2);
    case 'log_distance':
      return E.PROP.logDistance(d, f, Number(p.n ?? 3.5));
    case 'fresnel':
      // ENG6: PROP.fresnelR(d1_km, d2_km, f_ghz) — единицы вендора (см. XVER-FRESNEL)
      return E.PROP.fresnelR(Number(p.d1_km), Number(p.d2_km), Number(p.f_ghz));
    case 'noise_dbm':
      // ENG6: noiseDbm(bw_khz, nf_db) = -174 + 10lg(bw_кГц·1000) + NF — kTB, как RF-07
      return E.noiseDbm(Number(p.bw_khz), Number(p.nf_db));
    case 'knife_j':
      // ENG6: PROP.knifeEdgeJ(v) — дифракционные потери J(v), ITU-R P.526 аппроксимация
      return E.PROP.knifeEdgeJ(Number(p.v));
    case 'fiber_loss':
      // ENG6: FIBER.loss({attDbKm,spliceDb,connDb}, lenKm) = km·att + ⌊km/4⌋·splice + 2·conn
      return E.FIBER.loss({ attDbKm: Number(p.att_db_km), spliceDb: Number(p.splice_db), connDb: Number(p.conn_db) },
                          Number(p.len_km));
    case 'calc_link_prop':
      return calcLinkPoint(E, p).prop;
    case 'calc_link_rx':
      return calcLinkPoint(E, p).rx;
    case 'calc_link_noise':
      return calcLinkPoint(E, p).noise;
    case 'gas_gamma':
      // ENG6: PROP.gasGamma(f_mhz, tempC, hum) — удельное поглощение, дБ/км
      return E.PROP.gasGamma(Number(p.f_mhz), Number(p.temp_c ?? 15), Number(p.hum ?? 60));
    default:
      throw new Error('MODEL_UNSUPPORTED: ' + p.model);
  }
}

async function main() {
  const req = JSON.parse((await readStdin()) || '{}');

  // канонический SHA-256 запроса — ТОЧНАЯ функция ядра (core/validate.mjs),
  // используется для сквозной связки аудитов (inputSha256 линии B)
  if (req.mode === 'canonical_hash') {
    process.stdout.write(JSON.stringify({ ok: true, sha256: canonicalHash(req.value) }));
    return;
  }

  const loaded = loadEng6();
  const E = loaded.ENG6;

  const core = {
    sha256: loaded.sha256,
    bytes: loaded.bytes,
    version: (loaded.meta && (loaded.meta.version || loaded.meta.v)) || '5.1.0',
    propKeys: E.PROP ? Object.keys(E.PROP).sort() : [],
    facadeKeys: Object.keys(E).length,
  };

  if (req.mode === 'meta') {
    process.stdout.write(JSON.stringify({ ok: true, core }));
    return;
  }
  if (req.mode !== 'batch' || !Array.isArray(req.points)) {
    process.stdout.write(JSON.stringify({ ok: false, error: 'PROTOCOL: ожидается {"mode":"meta"} или {"mode":"batch","points":[…]}' }));
    process.exitCode = 2;
    return;
  }

  const results = req.points.map((p) => {
    try {
      const v = evaluate(E, p);
      if (!Number.isFinite(v)) throw new Error('NON_FINITE: ' + v);
      return { ok: true, model: p.model, value: v, point: p };
    } catch (e) {
      return { ok: false, model: p.model, error: String(e && e.message ? e.message : e), point: p };
    }
  });
  process.stdout.write(JSON.stringify({ ok: true, core, results }));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, error: 'PROBE_FATAL: ' + String(e && e.message ? e.message : e) }));
  process.exitCode = 1;
});
