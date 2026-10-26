"""unified/bridge/platform_db.py — персистентность результатов планирования (UNIF-12).

Хранит прогон PLAN-01 в БД платформы (SQLite stdlib; целевая СУБД — PostgreSQL,
схема логически идентична src/db/schema_postgres.sql, AUT-001 «единое ядро»):
  * plan_runs      — запуск: run_id (паспорт ENG-001), sha256 запроса и ответа,
                     ядро (backend + sha256 вендора), статус, грейд, makespan, паспорт JSON;
  * таблицы К-03   — map_object / link_line / link_rejected / route / deployment_wave /
                     schedule_summary / issues (метрики — JSON), идентичны выгрузке Пути А;
  * контроль целостности — sha256 канонического JSON ответа: verify_run() пересчитывает
                     хэш и сверяет (подмена строки таблицы обнаруживается).

Детерминированно, stdlib-only. Координаты — 6 знаков (NFR-03).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from backend_arc002 import stable_hash

VERSION = "1.0.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_runs (
  run_id            TEXT PRIMARY KEY,
  created_at        TEXT NOT NULL,
  request_sha256    TEXT NOT NULL,
  response_sha256   TEXT NOT NULL,
  engine_task_id    TEXT,
  engine_input_sha256 TEXT,
  engine_backend    TEXT,
  engine_core_sha256 TEXT,
  status            TEXT NOT NULL,
  grade             TEXT,
  objective_value   REAL,
  makespan_min      REAL,
  demands_satisfied INTEGER,
  demands_total     INTEGER,
  passport_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS map_object (
  run_id TEXT NOT NULL, object_id TEXT, object_type TEXT, unit_id TEXT, condition TEXT,
  geo_lat REAL, geo_lng REAL, cost REAL, wave_index INTEGER, start_min REAL
);
CREATE TABLE IF NOT EXISTS link_line (
  run_id TEXT NOT NULL, line_id TEXT, a TEXT, b TEXT, kind TEXT, length_km REAL,
  margin_db REAL, capacity_kbps REAL, load_kbps REAL, util_pct REAL,
  delay_ms REAL, availability_pct REAL, cost REAL, role TEXT
);
CREATE TABLE IF NOT EXISTS link_rejected (
  run_id TEXT NOT NULL, line_id TEXT, a TEXT, b TEXT, kind TEXT, code TEXT, reason TEXT, margin_db REAL
);
CREATE TABLE IF NOT EXISTS route (
  run_id TEXT NOT NULL, demand_id TEXT, status TEXT, granted_kbps REAL, deficit_kbps REAL,
  delay_ms REAL, hops INTEGER, reason TEXT, redundant INTEGER,
  path_nodes_json TEXT, path_lines_json TEXT
);
CREATE TABLE IF NOT EXISTS deployment_wave (
  run_id TEXT NOT NULL, wave_index INTEGER, t_start_min REAL, t_end_min REAL,
  duration_min REAL, crews_used INTEGER, items_json TEXT
);
CREATE TABLE IF NOT EXISTS schedule_summary (
  run_id TEXT NOT NULL, makespan_min REAL, crews INTEGER, crew_util_pct REAL, critical_path_json TEXT
);
CREATE TABLE IF NOT EXISTS issue (
  run_id TEXT NOT NULL, sev TEXT, code TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
  run_id TEXT NOT NULL, metrics_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_map_object_run ON map_object(run_id);
CREATE INDEX IF NOT EXISTS idx_link_line_run ON link_line(run_id);
CREATE INDEX IF NOT EXISTS idx_route_run ON route(run_id);
"""

# Таблицы К-03: имя → (колонки, порядок как в plan_to_platform_tables)
_TABLE_COLS = {
    "map_object": ["run_id", "object_id", "object_type", "unit_id", "condition",
                   "geo_lat", "geo_lng", "cost", "wave_index", "start_min"],
    "link_line": ["run_id", "line_id", "a", "b", "kind", "length_km", "margin_db",
                  "capacity_kbps", "load_kbps", "util_pct", "delay_ms",
                  "availability_pct", "cost", "role"],
    "link_rejected": ["run_id", "line_id", "a", "b", "kind", "code", "reason", "margin_db"],
    "deployment_wave": ["run_id", "wave_index", "t_start_min", "t_end_min",
                        "duration_min", "crews_used", "items_json"],
    "schedule_summary": ["run_id", "makespan_min", "crews", "crew_util_pct", "critical_path_json"],
    "issue": ["run_id", "sev", "code", "message"],
    "metrics": ["run_id", "metrics_json"],
}
_ROUTE_COLS = ["run_id", "demand_id", "status", "granted_kbps", "deficit_kbps", "delay_ms",
               "hops", "reason", "redundant", "path_nodes_json", "path_lines_json"]


def connect(db_path: str) -> sqlite3.Connection:
    """Открытие (или создание) БД платформы; схема создаётся идемпотентно."""
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_run(conn: sqlite3.Connection, run_id: str, request: Dict[str, Any],
             response: Dict[str, Any], passport: Dict[str, Any],
             tables: Dict[str, List[Dict[str, Any]]],
             engine_task_id: Optional[str] = None,
             engine_input_sha256: Optional[str] = None) -> Dict[str, str]:
    """Сохранение прогона: план + таблицы К-03 + паспорт. Возвращает хэши."""
    req_sha = stable_hash(request)
    resp_sha = stable_hash(response)
    core = response.get("core") or {}
    m = response.get("metrics") or {}
    sch = (response.get("plan") or {}).get("schedule") or {}
    demands_total = m.get("demandsTotal")
    demands_sat = m.get("demandsSatisfied")

    conn.execute(
        """INSERT INTO plan_runs (run_id, created_at, request_sha256, response_sha256,
               engine_task_id, engine_input_sha256, engine_backend, engine_core_sha256, status, grade,
               objective_value, makespan_min, demands_satisfied, demands_total, passport_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, time.strftime("%Y-%m-%d %H:%M:%S"), req_sha, resp_sha, engine_task_id,
         engine_input_sha256,
         f"ENG6-JS {core.get('engineVersion') or core.get('version') or '?'}",
         core.get("engineSha256") or core.get("sha256"),
         response.get("status"), m.get("grade"), m.get("objectiveValue"),
         sch.get("makespanMin"), demands_sat, demands_total,
         json.dumps(passport, ensure_ascii=False, sort_keys=True)))

    def _rows(key: str, cols: List[str]) -> List[tuple]:
        out = []
        for r in tables.get(key, []):
            row = dict(r)
            row["run_id"] = run_id
            # JSON-поля (вложенные структуры)
            if key == "deployment_wave":
                row["items_json"] = json.dumps(r.get("items") or [], ensure_ascii=False, sort_keys=True)
            if key == "schedule_summary":
                row["critical_path_json"] = json.dumps(r.get("critical_path") or [], ensure_ascii=False)
            if key == "metrics":
                row = {"run_id": run_id,
                       "metrics_json": json.dumps(r, ensure_ascii=False, sort_keys=True, default=str)}
            out.append(tuple(row.get(c) for c in cols))
        return out

    for table, cols in _TABLE_COLS.items():
        src = "issue" if table == "issue" else table
        conn.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            _rows("issues" if table == "issue" else src, cols))
    # route — с JSON-полями путей
    rrows = []
    for r in tables.get("route", []):
        rrows.append((run_id, r.get("demand_id"), r.get("status"), r.get("granted_kbps"),
                      r.get("deficit_kbps"), r.get("delay_ms"), r.get("hops"), r.get("reason"),
                      1 if r.get("redundant") else 0,
                      json.dumps(r.get("path_nodes") or [], ensure_ascii=False),
                      json.dumps(r.get("path_lines") or [], ensure_ascii=False)))
    conn.executemany(
        f"INSERT INTO route ({','.join(_ROUTE_COLS)}) VALUES ({','.join('?' * len(_ROUTE_COLS))})", rrows)
    conn.commit()
    return {"request_sha256": req_sha, "response_sha256": resp_sha}


def load_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    """Чтение прогона: заголовок + все таблицы К-03."""
    head = conn.execute("SELECT * FROM plan_runs WHERE run_id=?", (run_id,)).fetchone()
    if head is None:
        return None
    out: Dict[str, Any] = {k: head[k] for k in head.keys()}
    out["passport"] = json.loads(out.pop("passport_json"))
    for table in list(_TABLE_COLS) + ["route"]:
        rows = conn.execute(f"SELECT * FROM {table} WHERE run_id=?", (run_id,)).fetchall()
        out[table] = [dict(r) for r in rows]
    return out


def list_runs(conn: sqlite3.Connection, limit: int = 100) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT run_id, created_at, status, grade, makespan_min, engine_backend "
        "FROM plan_runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def verify_run(conn: sqlite3.Connection, run_id: str,
               response: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Контроль целостности: sha256 сохранённого ответа + сверка таблиц К-03.

    Если response передан — сверяется хэш (подмена ответа обнаруживается) и
    пересчитываются плоские таблицы: состав строк в БД обязан совпасть с
    результатом plan_to_platform_tables(response) (подмена строк обнаруживается).
    """
    head = conn.execute("SELECT response_sha256, status FROM plan_runs WHERE run_id=?",
                        (run_id,)).fetchone()
    if head is None:
        return {"ok": False, "reason": "RUN_NOT_FOUND"}
    n_tables = {t: conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE run_id=?", (run_id,)).fetchone()["c"]
                for t in list(_TABLE_COLS) + ["route"]}
    if response is not None:
        actual = stable_hash(response)
        if actual != head["response_sha256"]:
            return {"ok": False, "reason": "HASH_MISMATCH",
                    "expected": head["response_sha256"], "actual": actual}
        from datamodel_map import plan_to_platform_tables
        tables = plan_to_platform_tables(response, run_id=run_id)
        expect = {"map_object": len(tables["map_object"]), "link_line": len(tables["link_line"]),
                  "link_rejected": len(tables["link_rejected"]), "route": len(tables["route"]),
                  "deployment_wave": len(tables["deployment_wave"]),
                  "schedule_summary": len(tables["schedule_summary"]),
                  "issue": len(tables["issues"]), "metrics": len(tables["metrics"])}
        # содержательная сверка маршрутов (идентификаторы + статусы + выданная полоса)
        db_routes = {(r["demand_id"], r["status"], r["granted_kbps"]) for r in
                     conn.execute("SELECT demand_id, status, granted_kbps FROM route WHERE run_id=?",
                                  (run_id,)).fetchall()}
        exp_routes = {(r["demand_id"], r["status"], r["granted_kbps"]) for r in tables["route"]}
        if db_routes != exp_routes:
            return {"ok": False, "reason": "TABLES_MISMATCH", "table": "route"}
        for t, n in expect.items():
            if n_tables.get(t, 0) != n:
                return {"ok": False, "reason": "TABLES_MISMATCH", "table": t,
                        "db_rows": n_tables.get(t), "expected": n}
    return {"ok": True, "run_id": run_id, "status": head["status"], "rows": n_tables}
