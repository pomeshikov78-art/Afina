#!/usr/bin/env python3
"""Резервное копирование и восстановление (ТЗ §4.15 OPS, §13.2 шаг 10, AUT-T05, G5.3).

Снимок БД делается через sqlite3.Connection.backup — консисентно даже под нагрузкой.
Манифест хранит sha256 каждого файла: подмена/повреждение комплекта обнаруживается
до восстановления (AUT-T07: подменный пакет отклоняется, событие логируется).
RTO измеряется как «восстановление + проверка целостности связей»; RPO определяется
регламентной периодичностью (фиксируется в манифесте).

CLI:
  python3 tools/backup.py backup  --db data/afina.db --dest backups [--extra data archives]
  python3 tools/backup.py verify  --manifest backups/MANIFEST_*.json
  python3 tools/backup.py restore --manifest backups/MANIFEST_*.json --target data/restored.db
  python3 tools/backup.py drill   --db data/afina.db --dest backups   # полный цикл + замер RTO
"""
import argparse, hashlib, json, os, shutil, sqlite3, sys, time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

VERSION = "1.0.0"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_manifest(dest_dir: str) -> str:
    ms = sorted(f for f in os.listdir(dest_dir) if f.startswith("MANIFEST_") and f.endswith(".json"))
    if not ms:
        raise FileNotFoundError(f"в {dest_dir} нет манифестов резервных копий")
    return os.path.join(dest_dir, ms[-1])


def backup(db_path: str, dest_dir: str, extra_dirs=None, rpo_policy_s: int = 3600) -> dict:
    os.makedirs(dest_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    db_name = f"afina_{ts}.db"
    db_dest = os.path.join(dest_dir, db_name)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(db_dest)
    with dst:
        src.backup(dst)                     # консисентный снимок под нагрузкой
    dst.close(); src.close()
    manifest = {"tool": f"afina-backup {VERSION}", "created": time.time(), "ts": ts,
                "source_db": os.path.abspath(db_path),
                "rpo_policy_s": rpo_policy_s,
                "rpo_note": "RPO = регламентная периодичность копирования; снимок консисентен (sqlite backup API)",
                "db": {"file": db_name, "sha256": sha256_file(db_dest), "bytes": os.path.getsize(db_dest)},
                "queue": None, "extra": []}
    q = os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "queue.json")
    if os.path.exists(q):
        qd = os.path.join(dest_dir, f"queue_{ts}.json")
        shutil.copy2(q, qd)
        manifest["queue"] = {"file": os.path.basename(qd), "sha256": sha256_file(qd)}
    for d in extra_dirs or []:
        if not os.path.isdir(d):
            continue
        base = os.path.basename(d.rstrip("/"))
        td = os.path.join(dest_dir, f"{base}_{ts}")
        shutil.copytree(d, td)
        files = []
        for r, _, fs in os.walk(td):
            for f in fs:
                fp = os.path.join(r, f)
                files.append({"path": os.path.relpath(fp, td), "sha256": sha256_file(fp), "bytes": os.path.getsize(fp)})
        manifest["extra"].append({"dir": base, "target": os.path.basename(td), "files": files})
    mpath = os.path.join(dest_dir, f"MANIFEST_{ts}.json")
    json.dump(manifest, open(mpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"manifest": mpath, "db": db_dest, "sha256": manifest["db"]["sha256"], "created": manifest["created"]}


def verify(manifest_path: str) -> dict:
    """Контроль целостности комплекта: sha256 каждого файла против манифеста."""
    m = json.load(open(manifest_path, encoding="utf-8"))
    dest = os.path.dirname(os.path.abspath(manifest_path))
    problems = []
    dbp = os.path.join(dest, m["db"]["file"])
    if not os.path.exists(dbp):
        problems.append(f"файл БД отсутствует: {m['db']['file']}")
    elif sha256_file(dbp) != m["db"]["sha256"]:
        problems.append(f"sha256 БД не совпадает с манифестом (повреждение или подмена)")
    if m.get("queue"):
        qp = os.path.join(dest, m["queue"]["file"])
        if not os.path.exists(qp) or sha256_file(qp) != m["queue"]["sha256"]:
            problems.append("очередь: файл отсутствует или хэш не совпадает")
    for ex in m.get("extra", []):
        td = os.path.join(dest, ex["target"])
        for f in ex["files"]:
            fp = os.path.join(td, f["path"])
            if not os.path.exists(fp) or sha256_file(fp) != f["sha256"]:
                problems.append(f"{ex['dir']}/{f['path']}: отсутствует или хэш не совпадает")
    return {"ok": not problems, "problems": problems, "manifest": manifest_path,
            "policy": "подменный/повреждённый комплект отклоняется ДО восстановления; событие подлежит регистрации (AUT-T07)"}


def restore(manifest_path: str, target_db: str, restore_queue_to: str = None) -> dict:
    v = verify(manifest_path)
    if not v["ok"]:
        return {"status": "refused", "problems": v["problems"],
                "policy": v["policy"]}
    m = json.load(open(manifest_path, encoding="utf-8"))
    dest = os.path.dirname(os.path.abspath(manifest_path))
    os.makedirs(os.path.dirname(os.path.abspath(target_db)) or ".", exist_ok=True)
    shutil.copy2(os.path.join(dest, m["db"]["file"]), target_db)
    out = {"status": "restored", "target": target_db, "sha256": sha256_file(target_db)}
    if m.get("queue") and restore_queue_to:
        shutil.copy2(os.path.join(dest, m["queue"]["file"]), restore_queue_to)
        out["queue"] = restore_queue_to
    return out


def integrity_check(db_path: str) -> dict:
    """Целостность связей после восстановления: аудит-цепочка + счётчики реестров."""
    from afina.server.core import Store
    st = Store(db_path)
    av = st.audit_verify()
    counts = {t: st.q(f"SELECT COUNT(*) c FROM {t}")[0]["c"]
              for t in ("objects", "calcs", "baselines", "zam", "audit", "telemetry")}
    return {"audit_chain_ok": bool(av.get("ok")), "audit": av, "counts": counts}


def drill(db_path: str, dest_dir: str) -> dict:
    """Полный цикл: снимок → (имитация потери) → проверка → восстановление → замер RTO."""
    t0 = time.time()
    b = backup(db_path, dest_dir)
    t_backup = time.time() - t0
    t1 = time.time()
    target = os.path.join(dest_dir, "restored_drill.db")
    r = restore(b["manifest"], target)
    ic = integrity_check(target) if r["status"] == "restored" else None
    t_rto = time.time() - t1
    os.remove(target) if os.path.exists(target) else None
    return {"backup_s": round(t_backup, 3), "rto_s": round(t_rto, 3),
            "restore": r["status"], "integrity": ic,
            "sha256_match": r.get("sha256") == b["sha256"],
            "note": "RTO = восстановление + проверка целостности связей; RPO = регламент (rpo_policy_s в манифесте)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backup", "verify", "restore", "drill"])
    ap.add_argument("--db", default="data/afina.db")
    ap.add_argument("--dest", default="backups")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--target", default=None)
    ap.add_argument("--extra", nargs="*", default=[])
    a = ap.parse_args()
    if a.cmd == "backup":
        print(json.dumps(backup(a.db, a.dest, a.extra), ensure_ascii=False, indent=1))
    elif a.cmd == "verify":
        mp = a.manifest or latest_manifest(a.dest)
        print(json.dumps(verify(mp), ensure_ascii=False, indent=1))
    elif a.cmd == "restore":
        mp = a.manifest or latest_manifest(a.dest)
        print(json.dumps(restore(mp, a.target or os.path.join(a.dest, "restored.db")), ensure_ascii=False, indent=1))
    elif a.cmd == "drill":
        print(json.dumps(drill(a.db, a.dest), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
