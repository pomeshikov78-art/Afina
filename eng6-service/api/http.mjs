/**
 * api/http.mjs — низкоуровневые помощники HTTP (чтение тела, ответы, заголовки).
 * Без внешних зависимостей: только node:http/node:crypto.
 */
import crypto from 'node:crypto';
import { config } from './config.mjs';

export const SECURITY_HEADERS = Object.freeze({
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'no-referrer',
  'Cache-Control': 'no-store',
  'Pragma': 'no-cache',
  // NFR-02: координаты не кэшируются ни браузером, ни промежуточными прокси.
  // CSP запрещает UI любые внешние соединения, кроме собственного API.
  'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'self'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'",
});

export function requestId(req) {
  const incoming = req.headers['x-request-id'];
  if (typeof incoming === 'string' && /^[A-Za-z0-9._-]{8,128}$/.test(incoming)) return incoming;
  return crypto.randomUUID();
}

/** Идентификатор клиента для rate limit: API-ключ → иначе IP. */
export function clientId(req) {
  const key = apiKeyOf(req);
  if (key) return `key:${crypto.createHash('sha256').update(key).digest('hex').slice(0, 16)}`;
  const xf = config.http.trustProxy ? req.headers['x-forwarded-for'] : null;
  const ip = (typeof xf === 'string' ? xf.split(',')[0].trim() : null) || req.socket?.remoteAddress || 'unknown';
  return `ip:${ip}`;
}

export function apiKeyOf(req) {
  const auth = req.headers['authorization'];
  if (typeof auth === 'string' && auth.toLowerCase().startsWith('bearer ')) return auth.slice(7).trim();
  const k = req.headers['x-api-key'];
  return typeof k === 'string' && k ? k.trim() : null;
}

/**
 * Заказчик расчёта (для аудита). Заголовок HTTP обязан быть ASCII, поэтому
 * кириллица передаётся в percent-encoding (RFC 3986): X-Requestor: %D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2.
 * Здесь выполняется обратное декодирование; при ошибке значение сохраняется как есть.
 */
export function decodeRequestor(raw) {
  if (typeof raw !== 'string') return null;
  const v = raw.trim();
  if (!v) return null;
  if (v.includes('%')) {
    try {
      return decodeURIComponent(v).slice(0, 128);
    } catch {
      /* не percent-encoding — оставляем сырое значение */
    }
  }
  return v.slice(0, 128);
}

export function actorOf(req) {
  const name = decodeRequestor(req.headers['x-requestor']);
  const ua = typeof req.headers['user-agent'] === 'string' ? req.headers['user-agent'].slice(0, 128) : null;
  return name || (apiKeyOf(req) ? 'api-key' : ua || 'anonymous');
}

/**
 * Прочитать тело запроса с жёстким лимитом размера.
 * Отклоняет oversized-запросы ДО накопления в памяти (защита от OOM).
 */
export function readBody(req, maxBytes = config.http.maxBodyBytes) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let aborted = false;
    const onData = (c) => {
      size += c.length;
      if (size > maxBytes) {
        if (aborted) return;
        aborted = true;
        const err = new Error(`Тело запроса превышает лимит ${maxBytes} байт`);
        err.code = 'PAYLOAD_TOO_LARGE';
        reject(err);
        req.destroy();
        return;
      }
      chunks.push(c);
    };
    req.on('data', onData);
    req.on('end', () => {
      if (aborted) return;
      resolve(Buffer.concat(chunks));
    });
    req.on('error', (err) => {
      if (!aborted) reject(err);
    });
  });
}

export function sendJson(res, statusCode, payload, extraHeaders = {}) {
  let body;
  try {
    body = JSON.stringify(payload);
  } catch (err) {
    body = JSON.stringify({ error: { code: 'SERIALIZATION_FAILED', message: String((err && err.message) || err) } });
    statusCode = 500;
  }
  const buf = Buffer.byteLength(body);
  res.writeHead(statusCode, {
    ...SECURITY_HEADERS,
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': buf,
    ...extraHeaders,
  });
  res.end(body);
  return buf;
}

export function sendText(res, statusCode, text, contentType = 'text/plain; charset=utf-8', extraHeaders = {}) {
  const buf = Buffer.byteLength(text);
  res.writeHead(statusCode, { ...SECURITY_HEADERS, 'Content-Type': contentType, 'Content-Length': buf, ...extraHeaders });
  res.end(text);
  return buf;
}

export function sendError(res, statusCode, code, message, details = null, extraHeaders = {}) {
  return sendJson(res, statusCode, { error: { code, message, ...(details ? { details } : {}) } }, extraHeaders);
}

/**
 * Дешёвая структурная предпроверка запроса на уровне API.
 * Полная валидация выполняется ядром (core/validate.mjs) — здесь отсекается
 * только то, что нельзя даже поставить в очередь (мусор, превышение лимитов).
 * @returns {{ok:boolean, code?:string, message?:string, sizes?:object}}
 */
export function precheck(body, limits) {
  if (!body || typeof body !== 'object' || Array.isArray(body))
    return { ok: false, code: 'BODY_NOT_OBJECT', message: 'Тело запроса должно быть JSON-объектом' };
  const topo = body.topology;
  if (!topo || typeof topo !== 'object' || !Array.isArray(topo.nodes))
    return { ok: false, code: 'TOPOLOGY_MISSING', message: 'Отсутствует topology.nodes' };
  const nodes = topo.nodes.length;
  const links = Array.isArray(topo.links) ? topo.links.length : 0;
  const demands = Array.isArray(body.demands) ? body.demands.length : 0;
  if (nodes > limits.maxNodes) return { ok: false, code: 'LIMIT_NODES', message: `Узлов ${nodes} > лимита ${limits.maxNodes}`, sizes: { nodes, links, demands } };
  if (links > limits.maxLinks) return { ok: false, code: 'LIMIT_LINKS', message: `Рёбер ${links} > лимита ${limits.maxLinks}`, sizes: { nodes, links, demands } };
  if (demands > limits.maxDemands) return { ok: false, code: 'LIMIT_DEMANDS', message: `Заявок ${demands} > лимита ${limits.maxDemands}`, sizes: { nodes, links, demands } };
  return { ok: true, sizes: { nodes, links, demands } };
}
