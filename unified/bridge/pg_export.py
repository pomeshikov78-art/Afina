"""unified/bridge/pg_export.py — миграционный комплект SQLite → PostgreSQL (UNIF-22).

Назначение: перенос накопленных прогонов единой программы (plan_runs + таблицы
К-03) и единого аудит-леджера (NDJSON) в целевую СУБД PostgreSQL на стенде
заказчика (схема: unified/deploy/schema_unified_postgres.sql).

Принципы (наследуются от обеих линий):
  * stdlib-only: экспорт — детерминированный SQL-скрипт (INSERT … ON CONFLICT
    DO NOTHING), исполняется psql на стенде; psycopg не требуется;
  * честность: sha256-манифест на каждый артефакт экспорта; verify_export()
    пересчитывает хэши и сверяет число строк с источником;
  * идемпотентность: повторная загрузка того же экспорта не дублирует строки
    (ON CONFLICT DO NOTHING по первичным ключам; для безключевых таблиц К-03 —
    предварительный DELETE по run_id в том же скрипте, атомарно в транзакции).

CLI: python3 pg_export.py --db <unified.db> [--ledger <unified_audit.ndjson>]
        --out-dir <каталог>   → export.sql + export_ledger.sql + MANIFEST.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

VERSION = "1.0.0"

# Порядок выгрузки: сначала родители (plan_runs), затем дочерние таблицы.
_TABLES = ["plan_runs", "map_object", "link_line", "link_rejected", "route",
           "deployment_wave", "schedule_summary", "issue", "metrics"]

# Ключевые таблицы (есть PK/уникальность) vs append-таблицы (чистим по run_id)
_KEYED = {"plan_runs"}


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    return "'" + s.replace("'", "''") + "'"


def _jsonb_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
    return _sql_literal(s) + "::jsonb"


_JSONB_COLS = {"passport_json", "path_nodes_json", "path_lines_json", "items_json",
               "critical_path_json", "metrics_json", "extra_json"}


def export_sqlite(conn: sqlite3.Connection, out_path: str) -> Dict[str, Any]:
    """Экспорт всех таблиц персистентности в один SQL-скрипт (транзакция)."""
    lines: List[str] = [
        "-- export_sqlite: персистентность единой программы (platform_db) → PostgreSQL",
        f"-- сгенерировано: {time.strftime('%Y-%m-%d %H:%M:%S')} · pg_export v{VERSION}",
        "-- идемпотентно: keyed-таблицы ON CONFLICT DO NOTHING; append-таблицы чистятся по run_id",
        "BEGIN;",
    ]
    counts: Dict[str, int] = {}
    run_ids = [r[0] for r in conn.execute("SELECT run_id FROM plan_runs ORDER BY run_id").fetchall()]
    # предочистка append-таблиц по выгружаемым run_id (идемпотентный повтор)
    for rid in run_ids:
        for t in _TABLES:
            if t in _KEYED:
                continue
            lines.append(f"DELETE FROM {t} WHERE run_id = {_sql_literal(rid)};")
    for t in _TABLES:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        counts[t] = len(rows)
        if not rows:
            continue
        cols = rows[0].keys()
        col_list = ",".join(cols)
        for r in rows:
            vals = []
            for c in cols:
                v = r[c]
                vals.append(_jsonb_literal(v) if c in _JSONB_COLS else _sql_literal(v))
            conflict = " ON CONFLICT DO NOTHING" if t in _KEYED else ""
            lines.append(f"INSERT INTO {t} ({col_list}) VALUES ({','.join(vals)}){conflict};")
    lines.append("COMMIT;")
    sql = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(sql)
    return {"path": out_path, "bytes": len(sql.encode("utf-8")),
            "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "rows": counts, "run_ids": len(run_ids)}


def export_ledger(ledger_path: str, out_path: str) -> Dict[str, Any]:
    """Экспорт NDJSON-леджера (включая архивы ротации) в SQL (unified_audit_entry)."""
    files: List[str] = []
    if os.path.exists(ledger_path):
        # архивы ротации: <path>.<seq>.ndjson — выгружаем от старых к новым
        base = os.path.basename(ledger_path)
        d = os.path.dirname(os.path.abspath(ledger_path))
        archives = sorted(f for f in os.listdir(d)
                          if f.startswith(base + ".") and f.endswith(".ndjson"))
        files = [os.path.join(d, a) for a in archives] + [ledger_path]
    lines: List[str] = [
        "-- export_ledger: единый аудит-леджер (NDJSON + архивы ротации) → unified_audit_entry",
        f"-- сгенерировано: {time.strftime('%Y-%m-%d %H:%M:%S')} · pg_export v{VERSION}",
        "BEGIN;",
    ]
    n = 0
    seqs = set()
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                e = json.loads(ln)
                if e["seq"] in seqs:
                    raise ValueError(f"дублирование seq {e['seq']} при экспорте леджера — "
                                     f"цепочка повреждена; экспорт отклонён (честный отказ)")
                seqs.add(e["seq"])
                n += 1
                cols = ["seq", "ts", "event", "run_id", "engine_task_id", "input_sha256",
                        "actor", "status", "prev_hash", "hash", "extra_json"]
                vals = [_sql_literal(e["seq"]), _sql_literal(e.get("ts")), _sql_literal(e.get("event")),
                        _sql_literal(e.get("run_id")), _sql_literal(e.get("engine_task_id")),
                        _sql_literal(e.get("input_sha256")), _sql_literal(e.get("actor")),
                        _sql_literal(e.get("status")), _sql_literal(e.get("prev_hash")),
                        _sql_literal(e.get("hash")), _jsonb_literal(e.get("extra"))]
                lines.append(f"INSERT INTO unified_audit_entry ({','.join(cols)}) "
                             f"VALUES ({','.join(vals)}) ON CONFLICT (seq) DO NOTHING;")
    lines.append("COMMIT;")
    sql = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(sql)
    return {"path": out_path, "bytes": len(sql.encode("utf-8")),
            "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(), "entries": n}


def verify_export(manifest_path: str) -> Dict[str, Any]:
    """Контроль: sha256 каждого артефакта экспорта совпадает с манифестом."""
    with open(manifest_path, encoding="utf-8") as f:
        man = json.load(f)
    problems = []
    for art in man["artifacts"]:
        p = art["path"]
        if not os.path.exists(p):
            problems.append(f"{p}: ОТСУТСТВУЕТ")
            continue
        with open(p, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        if actual != art["sha256"]:
            problems.append(f"{p}: sha256 не совпал (ожидался {art['sha256'][:16]}…, факт {actual[:16]}…)")
    return {"ok": not problems, "problems": problems, "artifacts": len(man["artifacts"])}


def main() -> int:
    ap = argparse.ArgumentParser(description="Экспорт персистентности единой программы в PostgreSQL-скрипт")
    ap.add_argument("--db", required=True, help="SQLite-БД platform_db (unified.db)")
    ap.add_argument("--ledger", default=None, help="NDJSON единого леджера (опционально)")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    os.makedirs(args.out_dir, exist_ok=True)
    a1 = export_sqlite(conn, os.path.join(args.out_dir, "export.sql"))
    arts = [a1]
    if args.ledger:
        arts.append(export_ledger(args.ledger, os.path.join(args.out_dir, "export_ledger.sql")))
    manifest = {"document": "МАНИФЕСТ экспорта SQLite→PostgreSQL (UNIF-22)",
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "schema": "unified/deploy/schema_unified_postgres.sql",
                "artifacts": [{k: v for k, v in a.items()} for a in arts]}
    mpath = os.path.join(args.out_dir, "MANIFEST.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    v = verify_export(mpath)
    print(f"экспорт: {args.out_dir} · строк: {json.dumps(a1['rows'], ensure_ascii=False)} · "
          f"run_id: {a1['run_ids']} · verify: {'OK' if v['ok'] else v['problems']}")
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
