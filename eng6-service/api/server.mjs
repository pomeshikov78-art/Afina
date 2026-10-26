/**
 * api/server.mjs — REST/gRPC-слой сервиса (ЭТАП 3).
 *
 * Эндпоинты:
 *   POST /api/v1/plan/sync    — быстрый расчёт в одном HTTP-цикле (200/422/429/503)
 *   POST /api/v1/plan/async   — тяжёлая задача в очередь (202 + Location)
 *   GET  /api/v1/tasks/{id}   — статус и результат
 *   GET  /api/v1/tasks        — список задач (фильтр по состоянию)
 *   DELETE /api/v1/tasks/{id} — отмена задачи в очереди
 *   GET  /api/v1/meta         — контракт, лимиты, версия ядра
 *   GET  /api/v1/conformance  — протокол конформанса ENG6 (selfTest + регрессия)
 *   GET  /api/v1/audit        — журнал аудита (требование «Военпреда»)
 *   GET  /healthz, /readyz    — liveness/readiness
 *   GET  /metrics             — Prometheus text exposition
 *   GET  /                    — UI (single-file, без внешних ресурсов)
 *
 * Защита: лимит тела, структурная предпроверка, rate limit, семафор синхронных
 * расчётов, ограниченная очередь, таймаут задачи с принудительным перезапуском
 * воркера, аудит каждого запуска, security-заголовки (NFR-02).
 */
import http from 'node:http';
import https from 'node:https';
import { readFileSync, existsSync, statSync } from 'node:fs';
import path from 'node:path';
import { config, assertConfig } from './config.mjs';
import { M, render as renderMetrics, snapshot as metricsSnapshot } from './metrics.mjs';
import { logAudit, fingerprint, queryAudit, auditStats, closeAudit } from './audit.mjs';
import { RateLimiter, Semaphore } from './ratelimit.mjs';
import { TaskStore, TASK_STATE } from './tasks.mjs';
import { WorkerPool } from './pool.mjs';
import { requestId, clientId, apiKeyOf, actorOf, readBody, sendJson, sendText, sendError, precheck, SECURITY_HEADERS } from './http.mjs';
import { Eng6Planner, PLANNER_VERSION } from '../core/planner.mjs';
import { loadEng6, conformance as runConformance } from '../core/eng6.mjs';
import { LIMITS } from '../core/schema.mjs';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

export class ApiServer {
  constructor(opts = {}) {
    this.config = opts.config || config;
    this.pool = opts.pool || new WorkerPool();
    this.tasks = new TaskStore();
    this.limiter = new RateLimiter();
    this.syncSem = new Semaphore(this.config.pool.maxSyncConcurrency);
    this.core = loadEng6();
    this.plannerDescribe = Eng6Planner.describe(this.core);
    this.conformanceCache = null;
    this.conformanceCachedAt = 0;
    this.server = null;
    this.staticCache = new Map();
    this.counters = { started: Date.now(), requests: 0 };
    // backpressure по памяти: сколько байт тел запросов сейчас в обработке
    this.inflightBytes = 0;
    this.maxInflightBytes = this.config.pool.maxInflightBytes;
  }

  handler() {
    return (req, res) => this._handle(req, res).catch((err) => this._internalError(req, res, err));
  }

  async _internalError(req, res, err) {
    try {
      if (!res.headersSent) sendError(res, 500, 'INTERNAL_ERROR', String((err && err.message) || err));
      else res.end();
    } catch {}
    logAudit({ event: 'internal_error', path: req.url, message: String((err && err.message) || err), stack: process.env.ENG6_STRICT === '1' ? String(err && err.stack) : undefined });
  }

  async _handle(req, res) {
    const started = process.hrtime.bigint();
    const rid = requestId(req);
    res.setHeader('X-Request-Id', rid);
    res.setHeader('X-ENG6-Core-Sha256', this.core.sha256.slice(0, 16));
    res.setHeader('Access-Control-Allow-Origin', this.config.http.corsOrigin);
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Api-Key, X-Requestor, X-Request-Id');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
    res.setHeader('Access-Control-Max-Age', '600');
    this.counters.requests++;

    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const route = `${req.method} ${url.pathname}`;

    if (req.method === 'OPTIONS') {
      res.writeHead(204, SECURITY_HEADERS);
      res.end();
      return this._finish(req, res, route, rid, started, 204, 0);
    }

    try {
      switch (true) {
        case req.method === 'GET' && url.pathname === '/healthz':
          return this._healthz(req, res, route, rid, started);
        case req.method === 'GET' && url.pathname === '/readyz':
          return this._readyz(req, res, route, rid, started);
        case req.method === 'GET' && url.pathname === this.config.metrics.path:
          return this._metrics(req, res, route, rid, started);
        case req.method === 'GET' && url.pathname === '/api/v1/meta':
          return this._meta(req, res, route, rid, started);
        case req.method === 'GET' && url.pathname === '/api/v1/conformance':
          return this._conformance(req, res, route, rid, started);
        case req.method === 'GET' && url.pathname === '/api/v1/audit':
          return this._audit(req, res, url, route, rid, started);
        case req.method === 'GET' && url.pathname === '/api/v1/tasks':
          return this._taskList(req, res, url, route, rid, started);
        case req.method === 'POST' && url.pathname === '/api/v1/plan/sync':
          return await this._planSync(req, res, url, route, rid, started);
        case req.method === 'POST' && url.pathname === '/api/v1/plan/async':
          return await this._planAsync(req, res, url, route, rid, started);
        case req.method === 'GET' && /^\/api\/v1\/tasks\/[^/]+$/.test(url.pathname):
          return this._taskGet(req, res, url, route, rid, started);
        case req.method === 'DELETE' && /^\/api\/v1\/tasks\/[^/]+$/.test(url.pathname):
          return this._taskDelete(req, res, url, route, rid, started);
        case req.method === 'GET' && this.config.http.serveUi && !url.pathname.startsWith('/api'):
          return this._static(req, res, url, route, rid, started);
        default:
          sendError(res, 404, 'NOT_FOUND', `Маршрут не найден: ${route}`, { hint: 'См. GET /api/v1/meta или docs/03_API.md' });
          return this._finish(req, res, route, rid, started, 404, 0);
      }
    } catch (err) {
      if (err && err.code === 'PAYLOAD_TOO_LARGE') {
        const out = sendError(res, 413, err.code, err.message);
        return this._finish(req, res, route, rid, started, 413, out);
      }
      if (err && err.code === 'BODY_INVALID_JSON') {
        const out = sendError(res, 400, err.code, err.message);
        return this._finish(req, res, route, rid, started, 400, out);
      }
      throw err;
    }
  }

  _finish(req, res, route, rid, started, statusCode, bytes) {
    const ms = Number(process.hrtime.bigint() - started) / 1e6;
    const normalized = route.replace(/\/api\/v1\/tasks\/[^/]+/, '/api/v1/tasks/{id}');
    M.requestsTotal.inc({ method: req.method, path: normalized, code: String(statusCode) });
    M.requestDuration.observe({ method: req.method, path: normalized }, ms);
    // заголовок можно выставить только до отправки; после writeHead — недопустимо
    if (!res.headersSent) res.setHeader('X-Response-Time-Ms', ms.toFixed(2));
    if (!res.writableEnded) res.end();
    return { rid, statusCode, ms, bytes };
  }

  _healthz(req, res, route, rid, started) {
    const st = this.pool.status();
    sendJson(res, 200, { status: 'ok', uptimeS: Math.round((Date.now() - this.counters.started) / 1000), pool: st, memory: metricsSnapshot(), queue: { length: st.queueLength, max: st.maxQueue } });
    return this._finish(req, res, route, rid, started, 200, 0);
  }

  _readyz(req, res, route, rid, started) {
    const st = this.pool.status();
    const ready = st.ready > 0 && st.queueLength < st.maxQueue;
    const code = ready ? 200 : 503;
    sendJson(res, code, { ready, pool: st, reason: ready ? null : st.ready === 0 ? 'воркеры не готовы' : 'очередь переполнена' });
    return this._finish(req, res, route, rid, started, code, 0);
  }

  _metrics(req, res, route, rid, started) {
    if (!this.config.metrics.enabled) {
      sendError(res, 404, 'METRICS_DISABLED', 'Сбор метрик отключён конфигурацией');
      return this._finish(req, res, route, rid, started, 404, 0);
    }
    const text = renderMetrics();
    sendText(res, 200, text, 'text/plain; version=0.0.4; charset=utf-8');
    return this._finish(req, res, route, rid, started, 200, Buffer.byteLength(text));
  }

  _meta(req, res, route, rid, started) {
    const payload = {
      service: 'eng6-plan-service',
      version: this.config.version,
      env: this.config.env,
      planner: PLANNER_VERSION,
      core: { name: 'ENG6', version: this.core.ENG6.version, sha256: this.core.sha256, keys: this.core.keys.length, meta: this.core.meta, chars: this.core.chars, loadMs: round(this.core.loadMs) },
      limits: LIMITS,
      apiLimits: this.config.limits,
      pool: this.pool.status(),
      rateLimit: this.limiter.stats(),
      audit: auditStats(),
      endpoints: [
        { method: 'POST', path: '/api/v1/plan/sync', description: 'Синхронный расчёт (быстрые задачи)', responses: [200, 400, 413, 422, 429, 503] },
        { method: 'POST', path: '/api/v1/plan/async', description: 'Постановка тяжёлой задачи в очередь', responses: [202, 400, 413, 422, 429] },
        { method: 'GET', path: '/api/v1/tasks/{id}', description: 'Статус и результат задачи', responses: [200, 404] },
        { method: 'GET', path: '/api/v1/tasks', description: 'Список задач (?state=&limit=&actor=)', responses: [200] },
        { method: 'DELETE', path: '/api/v1/tasks/{id}', description: 'Отмена задачи в очереди', responses: [200, 404, 409] },
        { method: 'GET', path: '/api/v1/meta', description: 'Контракт, лимиты, версия ядра', responses: [200] },
        { method: 'GET', path: '/api/v1/conformance', description: 'Протокол конформанса ENG6', responses: [200] },
        { method: 'GET', path: '/api/v1/audit', description: 'Журнал аудита (?limit=&requestId=&actor=)', responses: [200, 403] },
        { method: 'GET', path: '/metrics', description: 'Метрики Prometheus', responses: [200] },
        { method: 'GET', path: '/healthz', description: 'Liveness', responses: [200] },
        { method: 'GET', path: '/readyz', description: 'Readiness', responses: [200, 503] },
      ],
      headers: { required: this.config.auth.requireRequestor ? ['X-Requestor'] : [], auth: this.config.auth.enabled ? ['Authorization: Bearer <key> | X-Api-Key: <key>'] : [], optional: ['X-Request-Id', 'X-Result-Format (summary|full)'] },
      statuses: this.plannerDescribe.statuses,
    };
    sendJson(res, 200, payload);
    return this._finish(req, res, route, rid, started, 200, 0);
  }

  _conformance(req, res, route, rid, started) {
    const url = new URL(req.url, 'http://localhost');
    const fresh = url.searchParams.get('refresh') === '1';
    const age = Date.now() - this.conformanceCachedAt;
    if (!this.conformanceCache || fresh || age > 3_600_000) {
      this.conformanceCache = runConformance();
      this.conformanceCachedAt = Date.now();
    }
    sendJson(res, 200, { ...this.conformanceCache, coreSha256: this.core.sha256, cached: !fresh, cacheAgeS: Math.round(age / 1000) });
    return this._finish(req, res, route, rid, started, 200, 0);
  }

  _audit(req, res, url, route, rid, started) {
    if (!this._authorized(req, res, route, rid, started, { admin: true })) return undefined;
    const limit = clampInt(url.searchParams.get('limit'), 50, 1, 1000);
    const items = queryAudit({ limit, requestId: url.searchParams.get('requestId'), actor: url.searchParams.get('actor'), status: url.searchParams.get('status') });
    sendJson(res, 200, { total: items.length, stats: auditStats(), items });
    return this._finish(req, res, route, rid, started, 200, 0);
  }

  _taskList(req, res, url, route, rid, started) {
    const limit = clampInt(url.searchParams.get('limit'), 50, 1, 500);
    const state = url.searchParams.get('state');
    const items = this.tasks.list({ limit, state: state && TASK_STATE[state.toUpperCase()] ? TASK_STATE[state.toUpperCase()] : null, actor: url.searchParams.get('actor') });
    sendJson(res, 200, { total: items.length, byState: this.tasks.countByState(), items });
    return this._finish(req, res, route, rid, started, 200, 0);
  }

  _taskGet(req, res, url, route, rid, started) {
    const id = decodeURIComponent(url.pathname.split('/').pop());
    const rec = this.tasks.get(id);
    if (!rec) {
      sendError(res, 404, 'TASK_NOT_FOUND', `Задача «${id}» не найдена или её результат уже удалён по TTL (${Math.round(this.tasks.ttlMs / 60000)} мин)`);
      return this._finish(req, res, route, rid, started, 404, 0);
    }
    const fmt = (req.headers['x-result-format'] || url.searchParams.get('format') || 'full').toLowerCase();
    const includeResult = fmt !== 'none' && fmt !== 'summary';
    const view = TaskStore.view(rec, { includeResult });
    if (fmt === 'summary' && rec.resultRef) view.result = summarizeResult(rec.resultRef);
    if (rec.state === TASK_STATE.QUEUED || rec.state === TASK_STATE.RUNNING) {
      const st = this.pool.status();
      view.position = Math.max(0, st.queueLength);
      view.estimatedWaitMs = st.busy >= st.workers && st.queueLength ? Math.round((st.queueLength / Math.max(1, st.workers)) * 1000) : 0;
    }
    const code = rec.state === TASK_STATE.SUCCEEDED || rec.state === TASK_STATE.RUNNING || rec.state === TASK_STATE.QUEUED ? 200 : rec.state === TASK_STATE.FAILED ? 200 : 200;
    sendJson(res, code, view);
    return this._finish(req, res, route, rid, started, code, view.resultBytes || 0);
  }

  _taskDelete(req, res, url, route, rid, started) {
    const id = decodeURIComponent(url.pathname.split('/').pop());
    const rec = this.tasks.get(id);
    if (!rec) {
      sendError(res, 404, 'TASK_NOT_FOUND', `Задача «${id}» не найдена`);
      return this._finish(req, res, route, rid, started, 404, 0);
    }
    if (rec.state !== TASK_STATE.QUEUED) {
      sendError(res, 409, 'TASK_NOT_CANCELLABLE', `Задача в состоянии ${rec.state} — отмена невозможна`, { state: rec.state });
      return this._finish(req, res, route, rid, started, 409, 0);
    }
    this.tasks.cancel(id);
    logAudit({ event: 'task_cancelled', taskId: id, actor: actorOf(req), requestId: rid });
    sendJson(res, 200, { taskId: id, state: TASK_STATE.CANCELLED });
    return this._finish(req, res, route, rid, started, 200, 0);
  }

  /** Общая часть для /plan/sync и /plan/async: чтение, авторизация, предпроверка. */
  async _preparePlan(req, res, rid, mode) {
    if (!this._authorized(req, res, `POST ${req.url}`, rid, null, { admin: false })) return null;
    // оценка размера ДО чтения тела: Content-Length известен заранее
    const declared = Number(req.headers['content-length'] || 0);
    if (declared > 0 && this.inflightBytes + declared > this.maxInflightBytes) {
      M.memoryRejected.inc({ mode });
      sendError(
        res, 503, 'MEMORY_BACKPRESSURE',
        `Сервис временно не принимает тяжёлые запросы: в обработке ${(this.inflightBytes / 1048576).toFixed(0)} МБ из лимита ${(this.maxInflightBytes / 1048576).toFixed(0)} МБ. Повторите позже.`,
        { inflightBytes: this.inflightBytes, limitBytes: this.maxInflightBytes, declaredBytes: declared, suggest: 'POST /api/v1/plan/async' },
        { 'Retry-After': '5' },
      );
      logAudit({ event: 'memory_backpressure', mode, actor: actorOf(req), requestId: rid, declaredBytes: declared, inflightBytes: this.inflightBytes });
      return null;
    }
    const buf = await readBody(req);
    this.inflightBytes += buf.length;
    M.inflightBytes.set({}, this.inflightBytes);
    M.payloadBytes.observe({ mode }, buf.length);
    if (buf.length === 0) {
      sendError(res, 400, 'EMPTY_BODY', 'Пустое тело запроса');
      return null;
    }
    let body;
    try {
      body = JSON.parse(buf.toString('utf8'));
    } catch (err) {
      const e = Object.assign(new Error(`Тело запроса не является корректным JSON: ${err.message}`), { code: 'BODY_INVALID_JSON' });
      throw e;
    }
    const pre = precheck(body, this.config.limits);
    if (!pre.ok) {
      sendError(res, 422, pre.code, pre.message, pre.sizes || null);
      M.validationFailed.inc({ mode, code: pre.code });
      logAudit({ event: 'plan_rejected', mode, actor: actorOf(req), requestId: rid, code: pre.code, message: pre.message, sizes: pre.sizes });
      return null;
    }
    if (this.config.auth.requireRequestor && !req.headers['x-requestor']) {
      sendError(res, 400, 'REQUESTOR_REQUIRED', 'Требуется заголовок X-Requestor (кто запросил расчёт) — требование аудита');
      return null;
    }
    return { body, sizes: pre.sizes, bytes: buf.length, raw: buf.toString('utf8') };
  }

  _releaseInflight(n) {
    this.inflightBytes = Math.max(0, this.inflightBytes - (n || 0));
    M.inflightBytes.set({}, this.inflightBytes);
  }

  async _planSync(req, res, url, route, rid, started) {
    const prepared = await this._preparePlan(req, res, rid, 'sync');
    if (!prepared) return this._finish(req, res, route, rid, started, res.statusCode || 400, 0);
    const { body, sizes, bytes } = prepared;
    // тело прочитано и разобрано — буфер можно освободить сразу
    this._releaseInflight(bytes);
    const fp = fingerprint(body);
    const cid = clientId(req);
    const actor = actorOf(req);

    const rl = this.limiter.take(cid, RateLimiter.costFor(sizes.nodes));
    if (!rl.allowed) {
      M.rateLimited.inc({ mode: 'sync' });
      logAudit({ event: 'rate_limited', mode: 'sync', actor, clientId: cid, requestId: rid, inputSha256: fp.sha256, sizes });
      sendError(res, 429, 'RATE_LIMITED', `Превышен лимит ${rl.limit} запросов в ${Math.round(this.config.rateLimit.windowMs / 1000)} с`, { remaining: rl.remaining, resetMs: rl.resetMs }, { 'Retry-After': String(Math.max(1, Math.ceil(rl.retryAfterMs / 1000))) });
      return this._finish(req, res, route, rid, started, 429, 0);
    }
    if (!this.syncSem.tryAcquire()) {
      const st = this.pool.status();
      logAudit({ event: 'sync_overloaded', mode: 'sync', actor, requestId: rid, inputSha256: fp.sha256, sizes, queue: st.queueLength });
      sendError(
        res,
        429,
        'SYNC_BUSY',
        `Все ${this.config.pool.maxSyncConcurrency} слотов синхронного расчёта заняты. Повторите позже или используйте POST /api/v1/plan/async (202 Accepted).`,
        { busy: st.busy, workers: st.workers, queueLength: st.queueLength, maxQueue: st.maxQueue, suggest: 'POST /api/v1/plan/async' },
        { 'Retry-After': '5' },
      );
      return this._finish(req, res, route, rid, started, 429, 0);
    }

    const taskId = this.tasks.newId('sync');
    const rec = this.tasks.create({ taskId, actor, requestId: rid, endpoint: '/api/v1/plan/sync', inputSha256: fp.sha256, inputBytes: bytes, nodes: sizes.nodes, links: sizes.links, demands: sizes.demands, mode: 'sync' });
    const tStart = Date.now();
    rec.startedAt = tStart;
    rec.state = TASK_STATE.RUNNING;
    logAudit({ event: 'plan_started', mode: 'sync', taskId, actor, clientId: cid, requestId: rid, inputSha256: fp.sha256, inputBytes: bytes, sizes, timeLimitMs: body?.constraints?.timeLimitMs ?? null });

    try {
      const timeoutMs = Math.min(Number(body?.constraints?.timeLimitMs) > 0 ? Number(body.constraints.timeLimitMs) + 5000 : this.config.pool.taskTimeoutMs, this.config.pool.taskTimeoutMs);
      const r = await this.pool.submit(taskId, body, { timeoutMs, rawJson: prepared.raw });
      const result = r.result;
      const computeMs = r.computeMs;
      const waitMs = Date.now() - tStart - computeMs;
      this.tasks.update(taskId, { state: TASK_STATE.SUCCEEDED, status: result?.status, finishedAt: Date.now(), computeMs: round(computeMs), queueWaitMs: round(Math.max(0, waitMs)), durationMs: Date.now() - tStart, workerId: r.workerIdx });
      logAudit({ event: 'plan_finished', mode: 'sync', taskId, actor, requestId: rid, inputSha256: fp.sha256, sizes, status: result?.status, computeMs: round(computeMs), queueWaitMs: round(Math.max(0, waitMs)), workerId: r.workerIdx, httpStatus: result?.status === 'INVALID' ? 422 : result?.status === 'ERROR' ? 500 : 200 });
      M.planTotal.inc({ mode: 'sync', status: String(result?.status) });
      M.planDuration.observe({ mode: 'sync' }, computeMs);
      M.planNodes.observe({}, sizes.nodes);
      M.planLinks.observe({}, sizes.links);
      M.planDemands.observe({}, sizes.demands);
      const fmt = (req.headers['x-result-format'] || url.searchParams.get('format') || 'full').toLowerCase();
      const payload = fmt === 'summary' ? { ...summarizeResult(result), requestId: rid, taskId } : { ...result, requestId: rid, taskId };
      // INVALID = некорректные входные данные → 422; ERROR = сбой ядра → 500;
      // остальные статусы (OPTIMAL/FEASIBLE/PARTIAL/INFEASIBLE/TIMEOUT) — это
      // РЕЗУЛЬТАТ работы алгоритма, они отдаются с кодом 200
      const code = result?.status === 'INVALID' ? 422 : result?.status === 'ERROR' ? 500 : 200;
      if (code !== 200) M.validationFailed.inc({ mode: 'sync', code: String(result?.status) });
      const out = sendJson(res, code, payload, { 'X-Compute-Ms': String(round(computeMs)), 'X-Queue-Wait-Ms': String(round(Math.max(0, waitMs))) });
      M.resultBytes.observe({ mode: 'sync' }, out);
      this.tasks.update(taskId, { state: code === 200 ? TASK_STATE.SUCCEEDED : TASK_STATE.FAILED });
      return this._finish(req, res, route, rid, started, code, out);
    } catch (err) {
      const code = err && err.code ? err.code : 'COMPUTE_FAILED';
      const httpCode = code === 'QUEUE_FULL' ? 429 : code === 'TASK_TIMEOUT' ? 504 : code === 'WORKER_OOM' ? 500 : 500;
      this.tasks.update(taskId, { state: code === 'TASK_TIMEOUT' ? TASK_STATE.TIMEOUT : TASK_STATE.FAILED, finishedAt: Date.now(), durationMs: Date.now() - tStart, error: { code, message: String(err && err.message) } });
      logAudit({ event: 'plan_failed', mode: 'sync', taskId, actor, requestId: rid, inputSha256: fp.sha256, sizes, code, message: String(err && err.message), httpStatus: httpCode });
      M.planTotal.inc({ mode: 'sync', status: code });
      const headers = httpCode === 429 ? { 'Retry-After': String(Math.max(1, Math.ceil((err.retryAfterMs || 5000) / 1000))) } : {};
      sendError(res, httpCode, code, String((err && err.message) || err), { taskId, suggest: httpCode === 429 ? 'POST /api/v1/plan/async' : undefined }, headers);
      return this._finish(req, res, route, rid, started, httpCode, 0);
    } finally {
      this.syncSem.release();
    }
  }

  async _planAsync(req, res, url, route, rid, started) {
    const prepared = await this._preparePlan(req, res, rid, 'async');
    if (!prepared) return this._finish(req, res, route, rid, started, res.statusCode || 400, 0);
    const { body, sizes, bytes } = prepared;
    this._releaseInflight(bytes);
    const fp = fingerprint(body);
    const cid = clientId(req);
    const actor = actorOf(req);

    const rl = this.limiter.take(cid, RateLimiter.costFor(sizes.nodes));
    if (!rl.allowed) {
      M.rateLimited.inc({ mode: 'async' });
      logAudit({ event: 'rate_limited', mode: 'async', actor, clientId: cid, requestId: rid, inputSha256: fp.sha256, sizes });
      sendError(res, 429, 'RATE_LIMITED', `Превышен лимит ${rl.limit} запросов в ${Math.round(this.config.rateLimit.windowMs / 1000)} с`, { remaining: rl.remaining, resetMs: rl.resetMs }, { 'Retry-After': String(Math.max(1, Math.ceil(rl.retryAfterMs / 1000))) });
      return this._finish(req, res, route, rid, started, 429, 0);
    }
    const st = this.pool.status();
    if (st.queueLength >= st.maxQueue) {
      M.queueRejected.inc({});
      logAudit({ event: 'queue_full', mode: 'async', actor, requestId: rid, inputSha256: fp.sha256, sizes, queueLength: st.queueLength });
      sendError(res, 429, 'QUEUE_FULL', `Очередь расчёта переполнена (${st.queueLength}/${st.maxQueue}). Задача не принята — повторите позже.`, { queueLength: st.queueLength, maxQueue: st.maxQueue, busy: st.busy, workers: st.workers }, { 'Retry-After': '10' });
      return this._finish(req, res, route, rid, started, 429, 0);
    }

    const taskId = this.tasks.newId('plan');
    const rec = this.tasks.create({ taskId, actor, requestId: rid, endpoint: '/api/v1/plan/async', inputSha256: fp.sha256, inputBytes: bytes, nodes: sizes.nodes, links: sizes.links, demands: sizes.demands, mode: 'async' });
    logAudit({ event: 'plan_queued', mode: 'async', taskId, actor, clientId: cid, requestId: rid, inputSha256: fp.sha256, inputBytes: bytes, sizes, queueLength: st.queueLength });

    // ответ 202 ДО завершения расчёта — клиент опрашивает GET /api/v1/tasks/{id}
    const out = sendJson(
      res,
      202,
      { taskId, state: rec.state, statusUrl: `/api/v1/tasks/${taskId}`, cancelUrl: `/api/v1/tasks/${taskId}`, position: st.queueLength + 1, estimatedWaitMs: Math.round(((st.queueLength + 1) / Math.max(1, st.workers)) * 1000), inputSha256: fp.sha256, sizes, requestId: rid },
      { Location: `/api/v1/tasks/${taskId}` },
    );
    this._finish(req, res, route, rid, started, 202, out);

    // расчёт — уже после ответа клиенту
    const timeoutMs = Math.min(Number(body?.constraints?.timeLimitMs) > 0 ? Number(body.constraints.timeLimitMs) + 5000 : this.config.pool.taskTimeoutMs, this.config.pool.taskTimeoutMs);
    this.pool
      .submit(taskId, body, { timeoutMs, rawJson: prepared.raw })
      .then((r) => {
        const result = r.result;
        const waitMs = (r.queueWaitMs ?? 0) || Math.max(0, Date.now() - rec.queuedAt - r.computeMs);
        this.tasks.update(taskId, { state: TASK_STATE.SUCCEEDED, status: result?.status, startedAt: rec.startedAt || Date.now() - r.computeMs, finishedAt: Date.now(), computeMs: round(r.computeMs), queueWaitMs: round(waitMs), durationMs: Date.now() - rec.createdAt, workerId: r.workerIdx });
        this.tasks.setResult(taskId, result);
        logAudit({ event: 'plan_finished', mode: 'async', taskId, actor, requestId: rid, inputSha256: fp.sha256, sizes, status: result?.status, computeMs: round(r.computeMs), queueWaitMs: round(waitMs), workerId: r.workerIdx, resultBytes: this.tasks.get(taskId)?.resultBytes || 0 });
        M.planTotal.inc({ mode: 'async', status: String(result?.status) });
        M.planDuration.observe({ mode: 'async' }, r.computeMs);
        M.planNodes.observe({}, sizes.nodes);
        M.planLinks.observe({}, sizes.links);
        M.planDemands.observe({}, sizes.demands);
        M.resultBytes.observe({ mode: 'async' }, this.tasks.get(taskId)?.resultBytes || 0);
      })
      .catch((err) => {
        const code = err && err.code ? err.code : 'COMPUTE_FAILED';
        const state = code === 'TASK_TIMEOUT' ? TASK_STATE.TIMEOUT : TASK_STATE.FAILED;
        this.tasks.update(taskId, { state, finishedAt: Date.now(), durationMs: Date.now() - rec.createdAt, error: { code, message: String(err && err.message) } });
        this.tasks.setResult(taskId, { status: code === 'TASK_TIMEOUT' ? 'TIMEOUT' : 'ERROR', statusReasons: [{ code, message: String(err && err.message) }], plan: null, metrics: null, diagnostics: null });
        logAudit({ event: 'plan_failed', mode: 'async', taskId, actor, requestId: rid, inputSha256: fp.sha256, sizes, code, message: String(err && err.message) });
        M.planTotal.inc({ mode: 'async', status: code });
      });
    return undefined;
  }

  _authorized(req, res, route, rid, started, { admin = false } = {}) {
    if (!this.config.auth.enabled) {
      if (admin && this.config.env === 'production') {
        sendError(res, 403, 'ADMIN_FORBIDDEN', 'Журнал аудита в production доступен только при включённой аутентификации (ENG6_AUTH_ENABLED=true)');
        if (started !== null) this._finish(req, res, route, rid, started, 403, 0);
        return false;
      }
      return true;
    }
    const key = apiKeyOf(req);
    if (!key || !this.config.auth.apiKeys.includes(key)) {
      sendError(res, 401, 'UNAUTHORIZED', 'Требуется действительный API-ключ (Authorization: Bearer <key> или X-Api-Key)', null, { 'WWW-Authenticate': 'Bearer' });
      if (started !== null) this._finish(req, res, route, rid, started, 401, 0);
      return false;
    }
    return true;
  }

  _static(req, res, url, route, rid, started) {
    const root = path.resolve(this.config.http.staticDir);
    let rel = decodeURIComponent(url.pathname);
    if (rel === '/' || rel === '') rel = '/index.html';
    const filePath = path.resolve(path.join(root, rel));
    if (!filePath.startsWith(root)) {
      sendError(res, 403, 'FORBIDDEN', 'Выход за пределы каталога статистики запрещён');
      return this._finish(req, res, route, rid, started, 403, 0);
    }
    try {
      if (!existsSync(filePath) || !statSync(filePath).isFile()) {
        // SPA-fallback: любой неизвестный путь UI отдаёт index.html
        const fallback = path.join(root, 'index.html');
        if (!existsSync(fallback)) {
          sendError(res, 404, 'NOT_FOUND', `Ресурс не найден: ${rel}`);
          return this._finish(req, res, route, rid, started, 404, 0);
        }
        return this._serveFile(res, fallback, req, route, rid, started, { immutable: false });
      }
      return this._serveFile(res, filePath, req, route, rid, started, { immutable: false });
    } catch (err) {
      sendError(res, 500, 'STATIC_ERROR', String((err && err.message) || err));
      return this._finish(req, res, route, rid, started, 500, 0);
    }
  }

  _serveFile(res, filePath, req, route, rid, started) {
    const ext = path.extname(filePath).toLowerCase();
    let buf = this.staticCache.get(filePath);
    if (!buf) {
      buf = readFileSync(filePath);
      if (buf.length < 8 * 1024 * 1024) this.staticCache.set(filePath, buf);
    }
    res.writeHead(200, {
      ...SECURITY_HEADERS,
      // UI — не секрет, но и не кэш-совместим: координаты в нём не хранятся (NFR-02)
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Content-Length': buf.length,
      'Cache-Control': 'no-cache',
    });
    res.end(buf);
    return this._finish(req, res, route, rid, started, 200, buf.length);
  }

  listen() {
    const problems = assertConfig();
    if (problems.length && this.config.env === 'production') throw new Error(`Конфигурация некорректна для production: ${problems.join('; ')}`);
    for (const p of problems) process.emitWarning?.(`config: ${p}`);
    const handler = this.handler();
    this.server = this.config.tls.enabled
      ? https.createServer(
          {
            cert: readFileSync(this.config.tls.cert),
            key: readFileSync(this.config.tls.key),
            ca: this.config.tls.ca ? readFileSync(this.config.tls.ca) : undefined,
            requestCert: this.config.tls.requireClientCert,
            rejectUnauthorized: this.config.tls.requireClientCert,
          },
          handler,
        )
      : http.createServer(handler);
    this.server.maxHeaderBytes = this.config.http.maxHeaderBytes;
    this.server.requestTimeout = this.config.http.requestTimeoutMs;
    this.server.keepAliveTimeout = this.config.http.keepAliveTimeoutMs;
    this.server.headersTimeout = this.config.http.keepAliveTimeoutMs + 5000;
    return new Promise((resolve, reject) => {
      this.server.once('error', reject);
      this.server.listen(this.config.http.port, this.config.http.host, () => {
        const addr = this.server.address();
        resolve({ port: typeof addr === 'object' ? addr.port : this.config.http.port, address: addr, problems });
      });
    });
  }

  async close() {
    clearInterval(this._metricsTimer);
    if (this.server) await new Promise((r) => this.server.close(r));
    await this.pool.close();
    await closeAudit();
  }
}

function summarizeResult(result) {
  if (!result) return null;
  const p = result.plan || {};
  return {
    status: result.status,
    statusReasons: result.statusReasons,
    metrics: result.metrics,
    timings: result.timings,
    core: result.core,
    plan: p
      ? {
          activeLinksTotal: p.activeLinksTotal,
          deployedUnitsTotal: p.deployedUnitsTotal,
          routesTotal: p.routesTotal,
          routes: (p.routes || []).slice(0, 50),
          activeLinks: (p.activeLinks || []).slice(0, 50),
          schedule: { makespanMin: p.schedule?.makespanMin, waves: p.schedule?.waves?.length, crews: p.schedule?.crews, criticalPath: p.schedule?.criticalPath },
          survivability: p.survivability,
          rejectedLinksHistogram: p.rejectedLinksHistogram,
        }
      : null,
    diagnostics: result.diagnostics
      ? {
          issues: (result.diagnostics.issues || []).slice(0, 20),
          recommendations: (result.diagnostics.recommendations || []).slice(0, 10),
          infeasibility: result.diagnostics.infeasibility
            ? { causes: result.diagnostics.infeasibility.causes, minimalRelaxations: (result.diagnostics.infeasibility.minimalRelaxations || []).slice(0, 10), partialSolution: result.diagnostics.infeasibility.partialSolution }
            : null,
          approximation: result.diagnostics.approximation,
          eng6Analysis: result.diagnostics.eng6Analysis ? { skipped: result.diagnostics.eng6Analysis.skipped, verdict: result.diagnostics.eng6Analysis.verdict, ratings: result.diagnostics.eng6Analysis.ratings } : null,
        }
      : null,
  };
}

function clampInt(v, def, min, max) {
  const n = Number(v);
  if (!Number.isFinite(n)) return def;
  return Math.max(min, Math.min(max, Math.round(n)));
}

function round(x, d = 2) {
  if (!Number.isFinite(x)) return x;
  const p = 10 ** d;
  return Math.round(x * p) / p;
}
