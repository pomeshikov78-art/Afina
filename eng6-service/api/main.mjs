/**
 * api/main.mjs — точка входа сервиса.
 *
 * Запуск:  node api/main.mjs          (или npm start)
 * Остановка: SIGINT/SIGTERM → graceful shutdown (дренаж очереди ≤ 10 с,
 * закрытие журнала аудита, terminate воркеров).
 */
import { ApiServer } from './server.mjs';
import { config, assertConfig } from './config.mjs';
import { loadEng6 } from '../core/eng6.mjs';

const problems = assertConfig();

function log(level, msg, extra) {
  const line = { ts: new Date().toISOString(), level, msg, ...(extra || {}) };
  process.stdout.write(JSON.stringify(line) + '\n');
}

async function main() {
  const core = loadEng6();
  log('info', 'ENG6 ядро загружено', { version: core.ENG6.version, sha256: core.sha256, keys: core.keys.length, loadMs: Math.round(core.loadMs), chars: core.chars });
  if (problems.length) for (const p of problems) log('warn', `конфигурация: ${p}`);

  const server = new ApiServer();
  const info = await server.listen();
  log('info', 'сервис запущен', {
    url: `${config.tls.enabled ? 'https' : 'http'}://${config.http.host}:${info.port}`,
    env: config.env,
    workers: config.pool.workers,
    maxQueue: config.pool.maxQueue,
    maxSyncConcurrency: config.pool.maxSyncConcurrency,
    taskTimeoutMs: config.pool.taskTimeoutMs,
    tls: config.tls.enabled,
    auth: config.auth.enabled,
    ratelimit: config.rateLimit.enabled,
    metrics: `${config.metrics.path}`,
  });

  let closing = false;
  const shutdown = async (signal) => {
    if (closing) return;
    closing = true;
    log('info', 'получен сигнал остановки', { signal });
    try {
      await server.close();
      log('info', 'остановка завершена');
      process.exit(0);
    } catch (err) {
      log('error', 'ошибка при остановке', { message: String((err && err.message) || err) });
      process.exit(1);
    }
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('unhandledRejection', (reason) => log('error', 'unhandledRejection', { message: String((reason && reason.message) || reason) }));
  process.on('uncaughtException', (err) => {
    log('error', 'uncaughtException', { message: String((err && err.message) || err), stack: process.env.ENG6_STRICT === '1' ? err.stack : undefined });
    // единственная допустимая реакция — аварийная остановка: состояние процесса
    // после неизвестного исключения недостоверно (требование промышленной эксплуатации)
    shutdown('uncaughtException');
  });

  // периодический снимок состояния в stdout-лог (структурированный, для ELK/Loki)
  const t = setInterval(() => {
    const st = server.pool.status();
    log('info', 'heartbeat', { pool: st, tasks: server.tasks.countByState(), mem: process.memoryUsage().rss, requests: server.counters.requests });
  }, 30_000);
  t.unref?.();
}

main().catch((err) => {
  log('error', 'не удалось запустить сервис', { message: String((err && err.message) || err), stack: String(err && err.stack) });
  process.exit(1);
});
