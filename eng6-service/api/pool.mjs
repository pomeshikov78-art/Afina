/**
 * api/pool.mjs — пул воркеров с ограниченной очередью (backpressure).
 *
 * Поведение под нагрузкой (требование Критика):
 *   • очередь ограничена maxQueue; при переполнении submit() немедленно
 *     возвращает ошибку QUEUE_FULL → HTTP 429 + Retry-After (сервер не «виснет»);
 *   • задачи исполняются по порядку FIFO, не более pool.workers одновременно;
 *   • превышение taskTimeoutMs → воркер принудительно завершается
 *     (worker.terminate()) и пересоздаётся: синхронный JS-расчёт невозможно
 *     прервать извне, это единственный честный способ не потерять сервис;
 *   • после workerRestartAfterTasks воркер перезапускается для гигиены кучи
 *     (защита от фрагментации на серии крупных графов).
 *
 * Метрики: eng6_queue_length, eng6_workers_busy, eng6_queue_wait_ms,
 *          eng6_worker_restarts_total.
 */
import { Worker } from 'node:worker_threads';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { config } from './config.mjs';
import { M } from './metrics.mjs';

const WORKER_PATH = fileURLToPath(new URL('./worker.mjs', import.meta.url));

/**
 * Автоматический лимит кучи воркера: доступная память / число воркеров / 2,
 * не менее 192 МБ и не более 1536 МБ. Делитель 2 оставляет место родителю
 * (HTTP-буферы, хранилище задач) и под страничный кэш.
 */
export function autoWorkerHeapMb(workers) {
  const total = os.totalmem();
  const per = Math.floor(total / Math.max(1, workers) / 2 / 1048576);
  return Math.max(192, Math.min(1536, per));
}

export class WorkerPool {
  constructor(opts = {}) {
    this.size = opts.workers ?? config.pool.workers;
    this.maxQueue = opts.maxQueue ?? config.pool.maxQueue;
    this.taskTimeoutMs = opts.taskTimeoutMs ?? config.pool.taskTimeoutMs;
    this.restartAfter = opts.workerRestartAfterTasks ?? config.pool.workerRestartAfterTasks;
    this.maxOldGenerationSizeMb = opts.maxOldGenerationSizeMb ?? config.pool.workerHeapMb ?? 0;
    if (!this.maxOldGenerationSizeMb) this.maxOldGenerationSizeMb = autoWorkerHeapMb(this.size);
    this.workers = [];
    this.queue = [];
    this.pending = new Map(); // taskId → {resolve, reject, timer, workerIdx, submittedAt}
    this.closed = false;
    this.stats = { submitted: 0, completed: 0, failed: 0, timedOut: 0, rejectedQueueFull: 0, workerRestarts: 0, oom: 0 };
    for (let i = 0; i < this.size; i++) this._spawn(i);
    M.workersTotal.set({}, this.size);
    this._metricsTimer = setInterval(() => this._publishMetrics(), 1000);
    this._metricsTimer.unref?.();
  }

  _spawn(idx) {
    const w = new Worker(WORKER_PATH, {
      workerData: { workerId: idx },
      resourceLimits: { maxOldGenerationSizeMb: this.maxOldGenerationSizeMb, maxYoungGenerationSizeMb: 128 },
      // воркеру не нужны права на сеть/ФС — но worker_threads не даёт sandbox,
      // поэтому изоляция обеспечивается кодом (core/eng6.mjs) и отсутствием
      // импортов node:net/node:fs в ядре; проверка — tests/conformance/isolation.test.mjs
    });
    const slot = { idx, worker: w, busy: false, tasksDone: 0, ready: false, heapMb: this.maxOldGenerationSizeMb };
    this.workers[idx] = slot;

    w.on('message', (msg) => this._onMessage(slot, msg));
    w.on('error', (err) => this._onWorkerFailure(slot, 'WORKER_ERROR', String(err && err.message)));
    w.on('exit', (code) => {
      if (this.closed) return;
      if (code !== 0) this._onWorkerFailure(slot, 'WORKER_EXIT', `воркер завершился с кодом ${code}`);
      else this._respawn(slot);
    });
    w.on('online', () => {});
    return slot;
  }

  _onMessage(slot, msg) {
    if (!msg) return;
    if (msg.type === 'ready') {
      slot.ready = true;
      slot.tasksDone = 0;
      this._drain();
      return;
    }
    if (msg.type === 'pong' || msg.type === 'describe') return;
    if (msg.type !== 'result') return;
    const p = this.pending.get(msg.taskId);
    slot.busy = false;
    slot.tasksDone = msg.tasksDone ?? slot.tasksDone + 1;
    if (!p) return; // задача уже снята по таймауту
    this.pending.delete(msg.taskId);
    clearTimeout(p.timer);
    if (msg.ok) {
      this.stats.completed++;
      let result = msg.result;
      if (result === undefined && msg.raw !== undefined) {
        try {
          result = JSON.parse(msg.raw);
        } catch {
          result = null;
        }
      }
      p.resolve({ ...msg, result, rawBytes: msg.raw ? msg.raw.length : 0, workerIdx: slot.idx });
    } else {
      this.stats.failed++;
      p.reject(Object.assign(new Error(msg.error?.message || 'worker error'), { code: 'WORKER_EXCEPTION', detail: msg.error }));
    }
    if (this.restartAfter > 0 && slot.tasksDone >= this.restartAfter) this._respawn(slot);
    else this._drain();
  }

  _onWorkerFailure(slot, code, message) {
    // все задачи этого воркера — в ошибку; сам воркер пересоздаётся
    for (const [taskId, p] of [...this.pending.entries()]) {
      if (p.workerIdx !== slot.idx) continue;
      this.pending.delete(taskId);
      clearTimeout(p.timer);
      this.stats.failed++;
      if (code === 'WORKER_OOM' || /out of memory/i.test(message)) this.stats.oom++;
      p.reject(Object.assign(new Error(message), { code }));
    }
    slot.busy = false;
    this._respawn(slot, code);
  }

  _respawn(slot, reason = null) {
    if (this.closed) return;
    this.stats.workerRestarts++;
    M.workerRestarts.inc({ reason: reason || 'scheduled' });
    try {
      slot.worker.removeAllListeners();
      slot.worker.terminate();
    } catch {}
    const fresh = this._spawn(slot.idx);
    this.workers[slot.idx] = fresh;
    this._drain();
  }

  _publishMetrics() {
    let busy = 0;
    for (const s of this.workers) if (s && s.busy) busy++;
    M.workersBusy.set({}, busy);
    M.queueLength.set({}, this.queue.length);
  }

  _drain() {
    while (this.queue.length) {
      const slot = this.workers.find((s) => s && s.ready && !s.busy);
      if (!slot) return;
      const item = this.queue.shift();
      slot.busy = true;
      item.workerIdx = slot.idx;
      const p = this.pending.get(item.taskId);
      if (p) p.workerIdx = slot.idx;
      const waitMs = Date.now() - item.queuedAt;
      M.queueWait.observe({}, waitMs);
      try {
        slot.worker.postMessage(
          item.rawJson !== undefined
            ? { type: 'task', taskId: item.taskId, raw: item.rawJson, opts: item.opts }
            : { type: 'task', taskId: item.taskId, request: item.request, opts: item.opts },
        );
      } catch (err) {
        slot.busy = false;
        const pp = this.pending.get(item.taskId);
        if (pp) {
          this.pending.delete(item.taskId);
          clearTimeout(pp.timer);
          pp.reject(Object.assign(new Error(String((err && err.message) || err)), { code: 'POST_MESSAGE_FAILED' }));
        }
      }
    }
  }

  /**
   * Поставить задачу в очередь.
   * @returns {Promise<{result:object, computeMs:number, queueWaitMs:number, workerIdx:number}>}
   * @throws {{code:'QUEUE_FULL'}} при переполнении очереди (→ HTTP 429)
   */
  submit(taskId, request, opts = {}) {
    if (this.closed) return Promise.reject(Object.assign(new Error('пул остановлен'), { code: 'POOL_CLOSED' }));
    if (this.queue.length >= this.maxQueue) {
      this.stats.rejectedQueueFull++;
      M.queueRejected.inc({});
      const err = new Error(`Очередь переполнена (${this.queue.length}/${this.maxQueue})`);
      err.code = 'QUEUE_FULL';
      err.retryAfterMs = Math.max(1000, Math.ceil((this.taskTimeoutMs / Math.max(1, this.size)) * 0.5));
      return Promise.reject(err);
    }
    this.stats.submitted++;
    const queuedAt = Date.now();
    const timeoutMs = opts.timeoutMs ?? this.taskTimeoutMs;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const p = this.pending.get(taskId);
        this.pending.delete(taskId);
        if (!p) return;
        this.stats.timedOut++;
        const slot = this.workers[p.workerIdx];
        const qi = this.queue.findIndex((x) => x.taskId === taskId);
        if (qi >= 0) this.queue.splice(qi, 1);
        reject(Object.assign(new Error(`Превышен лимит времени задачи: ${timeoutMs} мс`), { code: 'TASK_TIMEOUT', timeoutMs }));
        if (slot && slot.busy) {
          // синхронный расчёт нельзя прервать — завершаем воркер и пересоздаём
          this._respawn(slot, 'timeout');
        }
      }, timeoutMs);
      timer.unref?.();
      this.pending.set(taskId, { resolve, reject, timer, workerIdx: -1, queuedAt });
      this.queue.push({ taskId, request, rawJson: opts.rawJson, opts: { ...opts, rawJson: undefined }, queuedAt });
      M.queueLength.set({}, this.queue.length);
      this._drain();
    }).then((r) => ({ ...r, queueWaitMs: r.queueWaitMs ?? 0 }));
  }

  /** Число задач в очереди и занятых воркеров — для /healthz и метрик. */
  status() {
    let busy = 0;
    let ready = 0;
    for (const s of this.workers) {
      if (!s) continue;
      if (s.busy) busy++;
      if (s.ready) ready++;
    }
    return {
      workers: this.size, busy, idle: this.size - busy, ready, queueLength: this.queue.length, maxQueue: this.maxQueue,
      pending: this.pending.size, workerHeapMb: this.maxOldGenerationSizeMb, stats: { ...this.stats },
      cpus: os.cpus().length, totalMemMb: Math.round(os.totalmem() / 1048576), freeMemMb: Math.round(os.freemem() / 1048576),
    };
  }

  async close({ timeoutMs = 10_000 } = {}) {
    this.closed = true;
    clearInterval(this._metricsTimer);
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(Object.assign(new Error('сервис останавливается'), { code: 'POOL_CLOSED' }));
    }
    this.pending.clear();
    this.queue.length = 0;
    await Promise.all(
      this.workers.map(async (s) => {
        if (!s) return;
        try {
          await Promise.race([s.worker.terminate(), new Promise((r) => setTimeout(r, timeoutMs))]);
        } catch {}
      }),
    );
  }
}
