/**
 * api/worker.mjs — воркер расчётного пула (worker_threads).
 *
 * Задача воркера: исполнить plan(request) вне event loop HTTP-сервера, чтобы
 * тяжёлый расчёт (10^5 узлов) не блокировал приём запросов, метрики и health-check.
 *
 * Особенности:
 *  • ENG6 загружается ОДИН раз на воркер (37 мс) и переиспользуется;
 *  • воркер не имеет доступа к сети и ФС — plan() чистая функция (проверяется
 *    изоляцией вендора в core/eng6.mjs);
 *  • лимит памяти задаётся снаружи (resourceLimits) — при превышении воркер
 *    умирает с ERR_WORKER_OUT_OF_MEMORY, пул перезапускает его и возвращает
 *    задачу со статусом FAILED/OOM, а не роняет весь сервис;
 *  • прогресс не передаётся (расчёт атомарен), но воркер сообщает фазу старта.
 */
import { parentPort, workerData } from 'node:worker_threads';
import { plan, PLANNER_VERSION } from '../core/planner.mjs';
import { loadEng6 } from '../core/eng6.mjs';

const workerId = workerData?.workerId ?? 0;

let core = null;
let tasksDone = 0;

function ensureCore() {
  if (!core) core = loadEng6();
  return core;
}

parentPort.on('message', async (msg) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'ping') {
    parentPort.postMessage({ type: 'pong', workerId, tasksDone, ts: Date.now() });
    return;
  }
  if (msg.type === 'describe') {
    const c = ensureCore();
    parentPort.postMessage({ type: 'describe', workerId, planner: PLANNER_VERSION, engine: c.ENG6.version, sha256: c.sha256, keys: c.keys.length });
    return;
  }
  if (msg.type !== 'task') return;

  const { taskId, opts } = msg;
  const t0 = process.hrtime.bigint();
  try {
    ensureCore();
    // запрос передаётся СЫРОЙ СТРОКОЙ: structured-clone глубокого объекта
    // на 10^5 узлов стоит сотни миллисекунд и удваивает пиковую память,
    // тогда как JSON.parse в воркере дешевле и не плодит копии в родителе
    const request = msg.raw !== undefined ? JSON.parse(msg.raw) : msg.request;
    const parseMs = Number(process.hrtime.bigint() - t0) / 1e6;
    const result = plan(request, opts || {});
    const ms = Number(process.hrtime.bigint() - t0) / 1e6;
    tasksDone++;
    // результат тоже отдаём строкой — по той же причине
    parentPort.postMessage({ type: 'result', taskId, ok: true, raw: JSON.stringify(result), workerId, computeMs: ms, parseMs, tasksDone, rss: process.memoryUsage().rss });
  } catch (err) {
    const ms = Number(process.hrtime.bigint() - t0) / 1e6;
    parentPort.postMessage({
      type: 'result',
      taskId,
      ok: false,
      workerId,
      computeMs: ms,
      error: { message: String((err && err.message) || err), name: err && err.name, stack: process.env.ENG6_STRICT === '1' ? String(err && err.stack) : undefined },
    });
  }
});

// воркер готов — сообщаем родителю (пул не отправляет задачи до этой точки)
try {
  ensureCore();
  parentPort.postMessage({ type: 'ready', workerId, engine: core.ENG6.version, sha256: core.sha256, loadMs: core.loadMs });
} catch (err) {
  parentPort.postMessage({ type: 'fatal', workerId, error: String((err && err.message) || err) });
}
