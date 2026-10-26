/**
 * api/audit.mjs — журнал аудита (требование «Военпреда»).
 *
 * «Каждый запуск алгоритма должен логироваться: кто запросил, какие входные
 * параметры (хэш), время старта, время завершения, статус».
 *
 * Реализация:
 *  • append-only NDJSON-файл (var/audit.ndjson) — внешний аудит, расследование;
 *  • кольцевой буфер в памяти — быстрый GET /api/v1/audit без чтения диска;
 *  • входные данные НЕ логируются целиком (только SHA-256 канонического JSON,
 *    размер и размеры графа) — это и требование режима, и защита от раздувания;
 *  • запись асинхронная (write stream), отказ диска не останавливает расчёты,
 *    но фиксируется счётчиком eng6_audit_write_errors_total.
 */
import { createWriteStream, mkdirSync } from 'node:fs';
import path from 'node:path';
import { config } from './config.mjs';
import { canonicalHash } from '../core/validate.mjs';

let stream = null;
let writeErrors = 0;
const ring = [];
let ringPos = 0;

function ensureStream() {
  if (stream || !config.audit.enabled) return stream;
  try {
    mkdirSync(path.dirname(config.audit.file), { recursive: true });
    stream = createWriteStream(config.audit.file, { flags: 'a', encoding: 'utf8' });
    stream.on('error', (err) => {
      writeErrors++;
      stream = null;
      process.emitWarning?.(`audit: ошибка записи журнала: ${err.message}`);
    });
  } catch (err) {
    writeErrors++;
    process.emitWarning?.(`audit: не удалось открыть журнал ${config.audit.file}: ${err.message}`);
    stream = null;
  }
  return stream;
}

/**
 * Хэш и «отпечаток» запроса: SHA-256 канонического JSON + размеры.
 * Отпечаток позволяет доказать, что два запуска получили идентичный вход,
 * не раскрывая содержимого (координаты — охраняемые данные, NFR-02).
 */
export function fingerprint(request) {
  try {
    const nodes = request?.topology?.nodes?.length ?? 0;
    const links = request?.topology?.links?.length ?? 0;
    const demands = request?.demands?.length ?? 0;
    const fleet = request?.fleet?.length ?? 0;
    return {
      sha256: canonicalHash(request ?? null),
      bytes: Buffer.byteLength(JSON.stringify(request ?? null)),
      nodes,
      links,
      demands,
      fleet,
    };
  } catch (err) {
    return { sha256: null, error: String((err && err.message) || err), bytes: 0, nodes: 0, links: 0, demands: 0, fleet: 0 };
  }
}

/**
 * Записать событие аудита.
 * @param {object} rec
 */
export function logAudit(rec) {
  const entry = {
    ts: new Date().toISOString(),
    ...rec,
    service: 'eng6-plan',
    version: config.version,
    env: config.env,
    pid: process.pid,
  };
  if (config.audit.logPayload && rec.payload !== undefined) entry.payloadLogged = true;
  else delete entry.payload;
  ring.push(entry);
  if (ring.length > config.audit.ringSize) ring.shift();
  const s = ensureStream();
  if (s) {
    try {
      s.write(JSON.stringify(entry) + '\n');
    } catch {
      writeErrors++;
    }
  }
  return entry;
}

/** Последние записи журнала (новейшие первыми). */
export function queryAudit({ limit = 50, requestId = null, actor = null, status = null } = {}) {
  const out = [];
  for (let i = ring.length - 1; i >= 0 && out.length < Math.min(limit, ring.length); i--) {
    const e = ring[i];
    if (requestId && e.requestId !== requestId) continue;
    if (actor && e.actor !== actor) continue;
    if (status && e.status !== status) continue;
    out.push(e);
  }
  return out;
}

export function auditStats() {
  return { ringSize: ring.length, ringCapacity: config.audit.ringSize, file: config.audit.file, writeErrors, enabled: config.audit.enabled };
}

void ringPos;

/** Закрыть журнал (graceful shutdown). */
export async function closeAudit() {
  return new Promise((resolve) => {
    if (!stream) return resolve();
    stream.end(() => resolve());
    stream = null;
  });
}
