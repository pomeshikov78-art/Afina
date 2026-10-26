/**
 * api/ratelimit.mjs — ограничитель частоты (token bucket на фиксированном окне).
 *
 * Требование Критика: «Если придёт 100 запросов на расчёт графа из 50 000 узлов
 * одновременно, ядро должно не упасть, а поставить их в очередь и честно
 * отработать по одному, возвращая 429 Too Many Requests или 202 Accepted».
 *
 * Три уровня защиты (по порядку срабатывания):
 *   1) rate limit на клиента (API-ключ или IP)          → 429 + Retry-After;
 *   2) лимит одновременных синхронных расчётов          → 429 + подсказка async;
 *   3) переполнение очереди асинхронных задач           → 429 + Retry-After.
 *
 * Тяжёлые задачи (узлов больше heavyNodeThreshold) тарифицируются с весом
 * heavyWeight — иначе один клиент может «съесть» окно сотней мелких запросов
 * и одним запросом на 200 000 узлов.
 */
import { config } from './config.mjs';

class Bucket {
  constructor(tokens, ts) {
    this.tokens = tokens;
    this.windowStart = ts;
  }
}

export class RateLimiter {
  constructor(opts = {}) {
    this.enabled = opts.enabled ?? config.rateLimit.enabled;
    this.capacity = opts.requestsPerWindow ?? config.rateLimit.requestsPerWindow;
    this.windowMs = opts.windowMs ?? config.rateLimit.windowMs;
    this.maxClients = opts.maxTrackedClients ?? config.rateLimit.maxTrackedClients;
    this.buckets = new Map();
    this.rejected = 0;
    this.lastSweep = Date.now();
  }

  /**
   * @param {string} clientId
   * @param {number} cost вес запроса (обычно 1, для тяжёлых — heavyWeight)
   * @returns {{allowed:boolean, remaining:number, retryAfterMs:number, limit:number, resetMs:number}}
   */
  take(clientId, cost = 1) {
    if (!this.enabled) return { allowed: true, remaining: this.capacity, retryAfterMs: 0, limit: this.capacity, resetMs: 0 };
    const now = Date.now();
    if (now - this.lastSweep > this.windowMs * 4) this.sweep(now);
    let b = this.buckets.get(clientId);
    if (!b) {
      if (this.buckets.size >= this.maxClients) {
        // защита от исчерпания памяти при сканировании ключей: вытесняем старейший
        const oldest = this.buckets.keys().next().value;
        this.buckets.delete(oldest);
      }
      b = new Bucket(this.capacity, now);
      this.buckets.set(clientId, b);
    }
    if (now - b.windowStart >= this.windowMs) {
      b.tokens = this.capacity;
      b.windowStart = now;
    }
    const resetMs = Math.max(0, this.windowMs - (now - b.windowStart));
    if (b.tokens < cost) {
      this.rejected++;
      const need = cost - b.tokens;
      const retryAfterMs = Math.ceil((need / this.capacity) * this.windowMs);
      return { allowed: false, remaining: Math.max(0, b.tokens), retryAfterMs: Math.max(retryAfterMs, Math.min(resetMs, this.windowMs)), limit: this.capacity, resetMs };
    }
    b.tokens -= cost;
    return { allowed: true, remaining: b.tokens, retryAfterMs: 0, limit: this.capacity, resetMs };
  }

  sweep(now) {
    for (const [id, b] of this.buckets) if (now - b.windowStart >= this.windowMs * 2) this.buckets.delete(id);
    this.lastSweep = now;
  }

  /** Вес запроса по размеру графа. */
  static costFor(nodeCount) {
    if (!config.rateLimit.enabled) return 1;
    return nodeCount > config.rateLimit.heavyNodeThreshold ? config.rateLimit.heavyWeight : 1;
  }

  stats() {
    return { trackedClients: this.buckets.size, rejected: this.rejected, capacity: this.capacity, windowMs: this.windowMs, enabled: this.enabled };
  }
}

/**
 * Семафор на N одновременно выполняемых операций — для /plan/sync.
 * Не путать с очередью: здесь превышение даёт немедленный 429, а не ожидание.
 */
export class Semaphore {
  constructor(n) {
    this.n = Math.max(1, n);
    this.used = 0;
    this.waiters = [];
  }

  tryAcquire() {
    if (this.used < this.n) {
      this.used++;
      return true;
    }
    return false;
  }

  acquire() {
    if (this.tryAcquire()) return Promise.resolve(true);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  release() {
    const next = this.waiters.shift();
    if (next) next(true);
    else this.used = Math.max(0, this.used - 1);
  }

  get available() {
    return Math.max(0, this.n - this.used);
  }
}
