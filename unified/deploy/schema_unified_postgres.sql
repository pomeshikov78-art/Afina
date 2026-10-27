-- unified/deploy/schema_unified_postgres.sql
-- Целевая схема PostgreSQL 15 для персистентности единой программы (UNIF-22).
-- Логически идентична SQLite-схеме bridge/platform_db.py (принцип AUT-001
-- линии A: «единое ядро — две СУБД») и таблице леджера audit_bridge.
-- Применение на стенде:
--   psql -U afina -d afina -f schema_unified_postgres.sql
-- Загрузка данных: bridge/pg_export.py формирует INSERT-скрипт из SQLite-БД
-- платформы и NDJSON-леджера (детерминированный, с sha256-манифестом).

BEGIN;

CREATE TABLE IF NOT EXISTS plan_runs (
  run_id              TEXT PRIMARY KEY,
  created_at          TEXT NOT NULL,
  request_sha256      TEXT NOT NULL,
  response_sha256     TEXT NOT NULL,
  engine_task_id      TEXT,
  engine_input_sha256 TEXT,
  engine_backend      TEXT,
  engine_core_sha256  TEXT,
  status              TEXT NOT NULL,
  grade               TEXT,
  objective_value     DOUBLE PRECISION,
  makespan_min        DOUBLE PRECISION,
  demands_satisfied   INTEGER,
  demands_total       INTEGER,
  passport_json       JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS map_object (
  run_id      TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  object_id   TEXT,
  object_type TEXT,
  unit_id     TEXT,
  condition   TEXT,
  geo_lat     DOUBLE PRECISION,   -- WGS-84, 6 знаков (NFR-03); PostGIS-геометрия —
  geo_lng     DOUBLE PRECISION,   -- на стороне целевой схемы линии A (AUT-001)
  cost        DOUBLE PRECISION,
  wave_index  INTEGER,
  start_min   DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS link_line (
  run_id           TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  line_id          TEXT,
  a                TEXT,
  b                TEXT,
  kind             TEXT,
  length_km        DOUBLE PRECISION,
  margin_db        DOUBLE PRECISION,
  capacity_kbps    DOUBLE PRECISION,
  load_kbps        DOUBLE PRECISION,
  util_pct         DOUBLE PRECISION,
  delay_ms         DOUBLE PRECISION,
  availability_pct DOUBLE PRECISION,
  cost             DOUBLE PRECISION,
  role             TEXT
);

CREATE TABLE IF NOT EXISTS link_rejected (
  run_id    TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  line_id   TEXT,
  a         TEXT,
  b         TEXT,
  kind      TEXT,
  code      TEXT,
  reason    TEXT,
  margin_db DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS route (
  run_id          TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  demand_id       TEXT,
  status          TEXT,
  granted_kbps    DOUBLE PRECISION,
  deficit_kbps    DOUBLE PRECISION,
  delay_ms        DOUBLE PRECISION,
  hops            INTEGER,
  reason          TEXT,
  redundant       BOOLEAN,
  path_nodes_json JSONB,
  path_lines_json JSONB
);

CREATE TABLE IF NOT EXISTS deployment_wave (
  run_id       TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  wave_index   INTEGER,
  t_start_min  DOUBLE PRECISION,
  t_end_min    DOUBLE PRECISION,
  duration_min DOUBLE PRECISION,
  crews_used   INTEGER,
  items_json   JSONB
);

CREATE TABLE IF NOT EXISTS schedule_summary (
  run_id              TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  makespan_min        DOUBLE PRECISION,
  crews               INTEGER,
  crew_util_pct       DOUBLE PRECISION,
  critical_path_json  JSONB
);

CREATE TABLE IF NOT EXISTS issue (
  run_id  TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  sev     TEXT,
  code    TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
  run_id       TEXT NOT NULL REFERENCES plan_runs(run_id) ON DELETE CASCADE,
  metrics_json JSONB
);

-- Единый аудит (audit_bridge): NDJSON остаётся операционным журналом;
-- таблица — долговременное хранилище цепочки для SQL-верификации и выборок.
CREATE TABLE IF NOT EXISTS unified_audit_entry (
  seq            BIGINT PRIMARY KEY,
  ts             TEXT NOT NULL,
  event          TEXT NOT NULL,
  run_id         TEXT,
  engine_task_id TEXT,
  input_sha256   TEXT,
  actor          TEXT,
  status         TEXT,
  prev_hash      TEXT NOT NULL,
  hash           TEXT NOT NULL,
  extra_json     JSONB
);

CREATE INDEX IF NOT EXISTS idx_map_object_run   ON map_object(run_id);
CREATE INDEX IF NOT EXISTS idx_link_line_run    ON link_line(run_id);
CREATE INDEX IF NOT EXISTS idx_route_run        ON route(run_id);
CREATE INDEX IF NOT EXISTS idx_wave_run         ON deployment_wave(run_id);
CREATE INDEX IF NOT EXISTS idx_issue_run        ON issue(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_run        ON unified_audit_entry(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_input_sha  ON unified_audit_entry(input_sha256);

COMMIT;
