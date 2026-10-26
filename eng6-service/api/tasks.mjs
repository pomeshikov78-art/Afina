/**
 * api/tasks.mjs — хранилище состояний асинхронных задач.
 *
 * Состояния: QUEUED → RUNNING → SUCCEEDED | FAILED | TIMEOUT | CANCELLED.
 * Хранилище in-memory (промышленная замена — Redis/PostgreSQL через тот же
 * интерфейс, см. ADR-006): ключевое требование — НЕ хранить тело запроса
 * (только хэш), а результат держать не дольше resultTtlMs и не больше
 * maxStoredResults штук (LRU-вытеснение), иначе 100 задач по 50 000 узлов
 * вынесут память процесса.
 */
import { config } from './config.mjs';

export const TASK_STATE = Object.freeze({
  QUEUED: 'QUEUED',
  RUNNING: 'RUNNING',
  SUCCEEDED: 'SUCCEEDED',
  FAILED: 'FAILED',
  TIMEOUT: 'TIMEOUT',
  CANCELLED: 'CANCELLED',
});

export class TaskStore {
  constructor(opts = {}) {
    this.ttlMs = opts.ttlMs ?? config.pool.resultTtlMs;
    this.maxStored = opts.maxStoredResults ?? config.pool.maxStoredResults;
    this.map = new Map(); // taskId → record (Map сохраняет порядок вставки = LRU)
    this.seq = 0;
  }

  newId(prefix = 'task') {
    this.seq++;
    const t = Date.now().toString(36);
    const r = Math.floor(Math.random() * 1e6).toString(36).padStart(4, '0');
    return `${prefix}_${t}_${r}_${this.seq.toString(36)}`;
  }

  create({ taskId, actor, requestId, endpoint, inputSha256, inputBytes, nodes, links, demands, mode }) {
    const now = Date.now();
    const rec = {
      taskId,
      state: TASK_STATE.QUEUED,
      actor: actor || 'anonymous',
      requestId,
      endpoint,
      mode,
      inputSha256,
      inputBytes,
      sizes: { nodes, links, demands },
      createdAt: now,
      queuedAt: now,
      startedAt: null,
      finishedAt: null,
      durationMs: null,
      queueWaitMs: null,
      computeMs: null,
      workerId: null,
      status: null,
      error: null,
      resultRef: null,
      resultBytes: 0,
      progress: { phase: 'queued', percent: 0 },
    };
    this.map.set(taskId, rec);
    this.evict();
    return rec;
  }

  get(taskId) {
    return this.map.get(taskId) || null;
  }

  touch(taskId) {
    const rec = this.map.get(taskId);
    if (rec) {
      this.map.delete(taskId);
      this.map.set(taskId, rec);
    }
    return rec;
  }

  update(taskId, patch) {
    const rec = this.map.get(taskId);
    if (!rec) return null;
    Object.assign(rec, patch);
    return rec;
  }

  setResult(taskId, result) {
    const rec = this.map.get(taskId);
    if (!rec) return null;
    rec.resultRef = result;
    try {
      rec.resultBytes = Buffer.byteLength(JSON.stringify(result));
    } catch {
      rec.resultBytes = 0;
    }
    this.evict();
    return rec;
  }

  /** Удалить результат, оставив метаданные (экономия памяти по TTL). */
  dropResult(taskId) {
    const rec = this.map.get(taskId);
    if (!rec) return false;
    rec.resultRef = null;
    rec.resultBytes = 0;
    rec.resultExpired = true;
    return true;
  }

  evict() {
    const now = Date.now();
    for (const [id, rec] of this.map) {
      if (rec.finishedAt && now - rec.finishedAt > this.ttlMs) {
        if (rec.resultRef) this.dropResult(id);
        else this.map.delete(id);
      }
    }
    while (this.size > this.maxStored) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  get size() {
    return this.map.size;
  }

  list({ limit = 50, state = null, actor = null } = {}) {
    const out = [];
    const entries = [...this.map.values()].reverse();
    for (const rec of entries) {
      if (state && rec.state !== state) continue;
      if (actor && rec.actor !== actor) continue;
      out.push(publicView(rec));
      if (out.length >= limit) break;
    }
    return out;
  }

  countByState() {
    const out = {};
    for (const s of Object.values(TASK_STATE)) out[s] = 0;
    for (const rec of this.map.values()) out[rec.state] = (out[rec.state] || 0) + 1;
    return out;
  }

  cancel(taskId) {
    const rec = this.map.get(taskId);
    if (!rec) return null;
    if (rec.state === TASK_STATE.QUEUED) {
      rec.state = TASK_STATE.CANCELLED;
      rec.finishedAt = Date.now();
      rec.durationMs = rec.finishedAt - rec.createdAt;
      return rec;
    }
    return rec;
  }

  /** Публичное представление задачи (без тела результата). */
  static view(rec, { includeResult = false } = {}) {
    const v = publicView(rec);
    if (includeResult && rec.resultRef) v.result = rec.resultRef;
    else if (rec.resultRef) v.resultAvailable = true;
    if (rec.resultExpired) v.resultExpired = true;
    return v;
  }
}

function publicView(rec) {
  return {
    taskId: rec.taskId,
    state: rec.state,
    status: rec.status,
    actor: rec.actor,
    requestId: rec.requestId,
    mode: rec.mode,
    inputSha256: rec.inputSha256,
    inputBytes: rec.inputBytes,
    sizes: rec.sizes,
    createdAt: new Date(rec.createdAt).toISOString(),
    startedAt: rec.startedAt ? new Date(rec.startedAt).toISOString() : null,
    finishedAt: rec.finishedAt ? new Date(rec.finishedAt).toISOString() : null,
    queueWaitMs: rec.queueWaitMs,
    computeMs: rec.computeMs,
    durationMs: rec.durationMs,
    workerId: rec.workerId,
    resultBytes: rec.resultBytes,
    progress: rec.progress,
    error: rec.error || null,
  };
}
