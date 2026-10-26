/**
 * api/metrics.mjs — реестр метрик Prometheus (text exposition format 0.0.4).
 *
 * Без внешних зависимостей: свой Counter/Gauge/Histogram ровно в том объёме,
 * который нужен сервису (ЭТАП 5: время расчёта, размер очереди, загрузка воркеров).
 * Формат совместим с Prometheus ≥ 2.0 и VictoriaMetrics.
 */
import os from 'node:os';
import { config } from './config.mjs';

const HELP = {};
const TYPE = {};
const metrics = new Map();

function key(name, labels) {
  if (!labels) return name;
  return name + '|' + Object.keys(labels).sort().map((k) => `${k}="${escapeLabel(labels[k])}"`).join(',');
}

function escapeLabel(v) {
  return String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');
}

function labelStr(labels) {
  const ks = Object.keys(labels || {}).sort();
  if (!ks.length) return '';
  return `{${ks.map((k) => `${k}="${escapeLabel(labels[k])}"`).join(',')}}`;
}

function register(name, help, type) {
  if (!HELP[name]) {
    HELP[name] = help;
    TYPE[name] = type;
  }
}

class Counter {
  constructor(name, help) {
    register(name, help, 'counter');
    this.name = name;
    this.values = new Map();
  }

  inc(labels, v = 1) {
    const k = key(this.name, labels);
    this.values.set(k, (this.values.get(k) || 0) + v);
    if (!this.labels) this.labels = new Map();
    this.labels.set(k, labels || {});
  }

  collect() {
    const out = [];
    for (const [k, v] of this.values) out.push(`${this.name}${labelStr(this.labels.get(k))} ${formatNum(v)}`);
    return out;
  }
}

class Gauge {
  constructor(name, help) {
    register(name, help, 'gauge');
    this.name = name;
    this.values = new Map();
    this.labels = new Map();
  }

  set(labels, v) {
    const k = key(this.name, labels);
    this.values.set(k, v);
    this.labels.set(k, labels || {});
  }

  inc(labels, v = 1) {
    const k = key(this.name, labels);
    this.values.set(k, (this.values.get(k) || 0) + v);
    this.labels.set(k, labels || {});
  }

  collect() {
    const out = [];
    for (const [k, v] of this.values) out.push(`${this.name}${labelStr(this.labels.get(k))} ${formatNum(v)}`);
    return out;
  }
}

class Histogram {
  constructor(name, help, buckets = config.metrics.bucketsMs) {
    register(name, help, 'histogram');
    this.name = name;
    this.buckets = [...buckets].sort((a, b) => a - b);
    this.series = new Map();
  }

  _get(labels) {
    const k = key(this.name, labels);
    let s = this.series.get(k);
    if (!s) {
      s = { labels: labels || {}, counts: new Array(this.buckets.length).fill(0), sum: 0, count: 0, min: Infinity, max: -Infinity };
      this.series.set(k, s);
    }
    return s;
  }

  observe(labels, value) {
    const s = this._get(labels);
    s.sum += value;
    s.count++;
    if (value < s.min) s.min = value;
    if (value > s.max) s.max = value;
    for (let i = 0; i < this.buckets.length; i++) if (value <= this.buckets[i]) s.counts[i]++;
  }

  collect() {
    const out = [];
    for (const s of this.series.values()) {
      const ls = labelStr(s.labels);
      for (let i = 0; i < this.buckets.length; i++) {
        const le = this.buckets[i];
        let cum = 0;
        for (let j = 0; j <= i; j++) cum += s.counts[j];
        out.push(`${this.name}_bucket${insertLe(s.labels, le)} ${cum}`);
      }
      out.push(`${this.name}_bucket${insertLe(s.labels, '+Inf')} ${s.count}`);
      out.push(`${this.name}_sum${ls} ${formatNum(s.sum)}`);
      out.push(`${this.name}_count${ls} ${s.count}`);
      if (s.count) {
        out.push(`${this.name}_min${ls} ${formatNum(s.min)}`);
        out.push(`${this.name}_max${ls} ${formatNum(s.max)}`);
      }
      void ls;
    }
    return out;
  }
}

function insertLe(labels, le) {
  const l = { ...(labels || {}), le: String(le) };
  return labelStr(l);
}

function formatNum(v) {
  if (!Number.isFinite(v)) return 'NaN';
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(6).replace(/0+$/, '').replace(/\.$/, '.0');
}

// ── конкретные метрики сервиса ────────────────────────────────────────────
export const M = {
  requestsTotal: new Counter('eng6_http_requests_total', 'Всего HTTP-запросов по эндпоинтам и кодам ответа'),
  requestDuration: new Histogram('eng6_http_request_duration_ms', 'Длительность HTTP-запроса, мс'),
  planTotal: new Counter('eng6_plan_total', 'Число расчётов по статусам результата'),
  planDuration: new Histogram('eng6_plan_duration_ms', 'Длительность расчёта ядра, мс'),
  planNodes: new Histogram('eng6_plan_graph_nodes', 'Размер графа (узлы) в расчёте', [10, 100, 1000, 5000, 10000, 50000, 100000, 200000]),
  planLinks: new Histogram('eng6_plan_graph_links', 'Размер графа (рёбра) в расчёте', [10, 100, 1000, 10000, 50000, 100000, 500000, 1000000]),
  planDemands: new Histogram('eng6_plan_demands', 'Число заявок в расчёте', [1, 10, 100, 1000, 5000, 20000]),
  queueLength: new Gauge('eng6_queue_length', 'Текущая длина очереди асинхронных задач'),
  queueWait: new Histogram('eng6_queue_wait_ms', 'Время ожидания задачи в очереди, мс'),
  queueRejected: new Counter('eng6_queue_rejected_total', 'Задачи, отклонённые из-за переполнения очереди (429)'),
  memoryRejected: new Counter('eng6_memory_rejected_total', 'Запросы, отклонённые из-за исчерпания лимита памяти в обработке (503)'),
  inflightBytes: new Gauge('eng6_inflight_bytes', 'Суммарный объём тел запросов в обработке, байт'),
  workersBusy: new Gauge('eng6_workers_busy', 'Занятые воркеры расчёта'),
  workersTotal: new Gauge('eng6_workers_total', 'Всего воркеров расчёта'),
  workerRestarts: new Counter('eng6_worker_restarts_total', 'Перезапуски воркеров (по лимиту задач или из-за сбоя)'),
  rateLimited: new Counter('eng6_ratelimited_total', 'Запросы, отклонённые ограничителем частоты'),
  validationFailed: new Counter('eng6_validation_failed_total', 'Запросы, не прошедшие валидацию (422)'),
  payloadBytes: new Histogram('eng6_request_payload_bytes', 'Размер тела запроса, байт', [1024, 65536, 1048576, 8388608, 33554432, 67108864]),
  resultBytes: new Histogram('eng6_result_bytes', 'Размер результата, байт', [1024, 65536, 1048576, 8388608, 33554432, 67108864]),
  memoryRss: new Gauge('eng6_process_memory_rss_bytes', 'RSS процесса, байт'),
  memoryHeap: new Gauge('eng6_process_memory_heap_bytes', 'Heap Used, байт'),
  uptimeSeconds: new Gauge('eng6_process_uptime_seconds', 'Аптайм процесса, с'),
  tasksByState: new Gauge('eng6_tasks_by_state', 'Задачи по состояниям хранилища'),
  coreInfo: new Gauge('eng6_core_info', 'Информация о версии ядра (метка), значение всегда 1'),
};

const started = Date.now();

/** Снять показания процесса и отдать текст для /metrics. */
export function render() {
  const mu = process.memoryUsage();
  M.memoryRss.set({}, mu.rss);
  M.memoryHeap.set({}, mu.heapUsed);
  M.uptimeSeconds.set({}, Math.round((Date.now() - started) / 1000));
  const lines = [];
  lines.push('# HELP eng6_build_info Сборка сервиса');
  lines.push('# TYPE eng6_build_info gauge');
  lines.push(`eng6_build_info{version="${config.version}",env="${config.env}",node="${process.version}",pid="${process.pid}"} 1`);
  lines.push(`eng6_host_cpu_count ${os.cpus().length}`);
  lines.push(`eng6_host_loadavg_1m ${os.loadavg()[0].toFixed(3)}`);
  for (const m of Object.values(M)) {
    if (!metrics.has(m.name)) metrics.set(m.name, m);
    lines.push(`# HELP ${m.name} ${HELP[m.name] || ''}`);
    lines.push(`# TYPE ${m.name} ${TYPE[m.name]}`);
    lines.push(...m.collect());
  }
  return lines.join('\n') + '\n';
}

export function snapshot() {
  const mu = process.memoryUsage();
  return { rssBytes: mu.rss, heapBytes: mu.heapUsed, uptimeS: (Date.now() - started) / 1000, loadavg: os.loadavg() };
}
