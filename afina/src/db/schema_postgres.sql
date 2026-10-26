-- СПО «Афина NextGen» — целевая схема PostgreSQL 15 + PostGIS 3.4 (Приложение Г)
-- Логическая схема идентична SQLite-режиму (src/afina/server/core.py) — единое ядро, два профиля хранения (§3.3, AUT-001).
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE users(id SERIAL PRIMARY KEY, login TEXT UNIQUE NOT NULL, pass_hash TEXT NOT NULL, salt TEXT NOT NULL,
  role TEXT NOT NULL, display TEXT, created DOUBLE PRECISION, active SMALLINT DEFAULT 1);
CREATE TABLE sessions(token TEXT PRIMARY KEY, user_id INT REFERENCES users(id), issued DOUBLE PRECISION, expires DOUBLE PRECISION);
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT, crs TEXT, created DOUBLE PRECISION, created_by TEXT, baseline TEXT);
CREATE TABLE objects(id TEXT PRIMARY KEY, project TEXT, class TEXT, name TEXT,
  x DOUBLE PRECISION, y DOUBLE PRECISION, z DOUBLE PRECISION,
  geom geometry(Point, 4326),  -- пространственный столбец PostGIS (ГИС-ядро)
  attrs JSONB, ts_code TEXT DEFAULT 'TS-0', dat_status TEXT DEFAULT 'Не заполнено',
  source TEXT, recorded_at DOUBLE PRECISION, created_by TEXT);
CREATE INDEX objects_geom_idx ON objects USING GIST(geom);
CREATE TABLE tech_states(id SERIAL PRIMARY KEY, object_id TEXT, ts_code TEXT, basis TEXT, responsible TEXT, recorded_at DOUBLE PRECISION);
CREATE TABLE calcs(run_id TEXT PRIMARY KEY, function TEXT, model TEXT, mode TEXT, level TEXT,
  inputs_hash TEXT, payload JSONB, status TEXT, result_ref TEXT, passport JSONB,
  created DOUBLE PRECISION, finished DOUBLE PRECISION, created_by TEXT);
CREATE TABLE notifications(id SERIAL PRIMARY KEY, kind TEXT, title TEXT, body TEXT, created DOUBLE PRECISION,
  status TEXT DEFAULT 'Создано', dedup_key TEXT, escalated SMALLINT DEFAULT 0);
CREATE TABLE zam(id TEXT PRIMARY KEY, title TEXT, crit TEXT, org TEXT, req TEXT, status TEXT, basis TEXT, own_user TEXT, created DOUBLE PRECISION);
CREATE TABLE locks(object_id TEXT PRIMARY KEY, user_login TEXT, session TEXT, since DOUBLE PRECISION);
CREATE TABLE telemetry(id BIGSERIAL PRIMARY KEY, object_id TEXT, ts DOUBLE PRECISION, metric TEXT, value DOUBLE PRECISION,
  quality TEXT DEFAULT 'наблюдение');  -- целевой профиль: partitioning по ts / TimescaleDB
CREATE TABLE audit(id BIGSERIAL PRIMARY KEY, ts DOUBLE PRECISION, user_login TEXT, role TEXT, action TEXT,
  object TEXT, class TEXT, result TEXT, prev_hash TEXT, hash TEXT);  -- защищённое журнал-хранилище: append-only, ротация регламентом
CREATE TABLE equipment(id TEXT PRIMARY KEY, class TEXT, name TEXT, maker TEXT, country TEXT, year_from INT, year_to INT,
  bands TEXT, power_w DOUBLE PRECISION, sensitivity_dbm DOUBLE PRECISION, sens_state TEXT, status_kind TEXT,
  data_quality TEXT, source TEXT, attrs JSONB);
CREATE TABLE org_units(id TEXT PRIMARY KEY, parent TEXT, name TEXT, kind TEXT, oshc_version TEXT,
  staff INT, fact INT, flags JSONB, subordination TEXT);
CREATE TABLE baselines(id TEXT PRIMARY KEY, project TEXT, status TEXT, created DOUBLE PRECISION,
  created_by TEXT, content_hash TEXT, content JSONB);
-- G3: качество данных и конфигурация
CREATE TABLE conflicts(id TEXT PRIMARY KEY, object_id TEXT, field TEXT, vals JSONB,
  status TEXT, created DOUBLE PRECISION, resolution TEXT, resolved_by TEXT, basis TEXT);
CREATE TABLE changes(id TEXT PRIMARY KEY, title TEXT, reason TEXT, status TEXT, author TEXT,
  created DOUBLE PRECISION, affected JSONB, approver TEXT, version_new TEXT);
-- G4: сценарии DT-5, суррогаты MOD-006
CREATE TABLE scenarios(id TEXT PRIMARY KEY, object_id TEXT, kind TEXT, params JSONB,
  base_state JSONB, result JSONB, marked TEXT DEFAULT 'оценка (не факт)', validated INT DEFAULT 0,
  validation_basis TEXT, created DOUBLE PRECISION, created_by TEXT);
CREATE TABLE surrogates(id TEXT PRIMARY KEY, model_ref TEXT, kind TEXT, payload JSONB,
  status TEXT DEFAULT 'Разрабатывается', disabled INT DEFAULT 0, compare_result JSONB,
  validated_by TEXT, validation_basis TEXT, created DOUBLE PRECISION, created_by TEXT);
-- G4: военно-технический профиль ВТ-01…08
CREATE TABLE vt_participants(id TEXT PRIMARY KEY, org TEXT, role TEXT, zone TEXT,
  contact TEXT, created DOUBLE PRECISION, created_by TEXT);
CREATE TABLE vt_transfers(id TEXT PRIMARY KEY, package TEXT, from_org TEXT, to_org TEXT,
  category TEXT, sha256 TEXT, items INT, created DOUBLE PRECISION, created_by TEXT);
CREATE TABLE vt_stages(id TEXT PRIMARY KEY, contract TEXT, title TEXT, due DOUBLE PRECISION,
  product INT DEFAULT 0, docs INT DEFAULT 0, tests INT DEFAULT 0, kit INT DEFAULT 0,
  evidence JSONB, commission TEXT, commission_by TEXT, commission_basis TEXT, created DOUBLE PRECISION);
CREATE TABLE vt_benches(id TEXT PRIMARY KEY, name TEXT, kind TEXT, passport TEXT,
  calib_due DOUBLE PRECISION, status TEXT, created DOUBLE PRECISION);
CREATE TABLE vt_tests(id TEXT PRIMARY KEY, object_id TEXT, bench_id TEXT, basis TEXT,
  config_ref TEXT, method_ref TEXT, software TEXT, conditions JSONB, raw JSONB, results JSONB,
  deviations JSONB, conclusion TEXT, approval TEXT DEFAULT 'Не согласован', created DOUBLE PRECISION, created_by TEXT);
CREATE TABLE vt_conditions(id TEXT PRIMARY KEY, object_id TEXT, factor TEXT,
  project_value DOUBLE PRECISION, actual_value DOUBLE PRECISION, unit TEXT, source TEXT, created DOUBLE PRECISION);
-- G4: геймификация (§4.11)
CREATE TABLE game_awards(id SERIAL PRIMARY KEY, user_login TEXT, event TEXT, points INT,
  dedup_key TEXT UNIQUE, ref TEXT, basis TEXT, created DOUBLE PRECISION,
  revoked INT DEFAULT 0, revoke_reason TEXT, revoked_by TEXT);
-- Объектное хранилище (MinIO/файловый fallback): бакеты afina-results, afina-docs, afina-models, afina-tiles.
-- Шина событий (RabbitMQ/локальная Kafka): топики afina.calc, afina.ntf, afina.telemetry, afina.audit.
