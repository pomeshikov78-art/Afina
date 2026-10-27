"""unified/rtm/build_unified_manifest.py — единый манифест поставки (UNIF-16, шаг 10).

Объединяет три раздела с пересчётом sha256 каждого файла ЗДЕСЬ И СЕЙЧАС:
  A. линия A — 18 позиций `afina/releases/КОМПЛЕКТ_ПОСТАВКИ.json` (сверка хэшей);
  B. линия B — состав поставки Дополнения №2 (Приложение Б) + контрольная сумма
     вендора ENG6 (эталон adb610c9…);
  C. unified — артефакты единой программы (мост, UI, CI, deploy, RTM, протоколы,
     документация) — последние протоколы каждого типа.

Честность: статус позиции пересчитывается — «соответствует» только при совпадении
sha256; изменения/отсутствия фиксируются явно. Итоговый вердикт: COMPLETE /
DEGRADED (есть расхождения) / INCOMPLETE (нет обязательных файлов).

Запуск: python3 build_unified_manifest.py [--out artifacts/MANIFEST_UNIFIED.json]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNIFIED = os.path.dirname(HERE)
REPO = os.path.dirname(UNIFIED)
AFINA = os.path.join(REPO, "afina")
ENG6 = os.path.join(REPO, "eng6-service")
VENDOR_SHA256_ETALON = "adb610c9eee1ca17b257cbc6087e2278fdde3eef92f3b6a360cfb51968d48b20"

VERSION = "1.0.0"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_entry(path: str, base: str, expected_sha: str | None = None) -> dict:
    full = os.path.join(base, path) if not os.path.isabs(path) else path
    e: dict = {"path": os.path.relpath(full, REPO), "kind": "dir" if os.path.isdir(full) else "file"}
    if os.path.isdir(full):
        n, total = 0, 0
        for root, _dirs, files in os.walk(full):
            for fn in files:
                n += 1
                total += os.path.getsize(os.path.join(root, fn))
        e.update({"files": n, "bytes": total, "present": n > 0})
        return e
    if not os.path.exists(full):
        e.update({"present": False})
        if expected_sha:
            e.update({"sha256_expected": expected_sha, "verdict": "ОТСУТСТВУЕТ"})
        return e
    sha = sha256_file(full)
    e.update({"present": True, "bytes": os.path.getsize(full), "sha256": sha})
    if expected_sha:
        e.update({"sha256_expected": expected_sha,
                  "verdict": "соответствует" if sha == expected_sha else "ИЗМЕНЁН"})
    return e


def section_a() -> dict:
    """Линия A: позиции КОМПЛЕКТ_ПОСТАВКИ.json с пересчётом хэшей."""
    src = os.path.join(AFINA, "releases", "КОМПЛЕКТ_ПОСТАВКИ.json")
    with open(src, encoding="utf-8") as f:
        kit = json.load(f)
    items = []
    for it in kit.get("items", []):
        paths = [file_entry(p["path"], AFINA, p.get("sha256")) for p in it.get("paths", [])]
        verdicts = [p.get("verdict") for p in paths if p.get("verdict")]
        status = it.get("status", "?")
        integrity = ("соответствует" if verdicts and all(v == "соответствует" for v in verdicts)
                     else "ИЗМЕНЁН/ОТСУТСТВУЕТ" if verdicts else "без хэш-контроля")
        items.append({"n": it.get("n"), "item": it.get("item"), "status": status,
                      "note": it.get("note"), "integrity": integrity, "paths": paths})
    ok = sum(1 for i in items if i["integrity"] == "соответствует")
    return {"title": "Линия A — СПО «Афина NextGen» (комплект §11.1)",
            "source": "afina/releases/КОМПЛЕКТ_ПОСТАВКИ.json",
            "items_total": len(items), "items_intact": ok, "items": items}


def section_b() -> dict:
    """Линия B: состав поставки Дополнения №2 (Приложение Б) + эталон вендора."""
    dirs = ["core", "api", "frontend", "integrations", "loadtest", "deploy", "tests", "scripts", "examples", "docs"]
    entries = [file_entry(d, ENG6) for d in dirs]
    entries += [file_entry(f, ENG6) for f in ("README.md", "package.json")]
    vendor = file_entry(os.path.join("core", "vendor", "eng6_core.js"), ENG6, VENDOR_SHA256_ETALON)
    entries.append(vendor)
    intact = all(e.get("present") for e in entries) and vendor.get("verdict") == "соответствует"
    return {"title": "Линия B — ENG6-PLAN (состав поставки, Приложение Б Дополнения №2)",
            "items_total": len(entries),
            "items_intact": sum(1 for e in entries if e.get("present")),
            "vendor_verdict": vendor.get("verdict"), "entries": entries,
            "intact": intact}


def section_c() -> dict:
    """Unified: артефакты единой программы (последние протоколы каждого типа)."""
    entries = []
    bridge = ["eng6_js_client.py", "eng6_probe.mjs", "backend_arc002.py", "xverify.py",
              "datamodel_map.py", "plan_service.py", "platform_db.py", "audit_bridge.py",
              "platform_client.py", "master_chain_sync.py", "scenario_export.py", "pg_export.py"]
    entries += [file_entry(os.path.join("unified", "bridge", f), REPO) for f in bridge]
    entries += [file_entry(os.path.join("unified", p), REPO) for p in
                ("ui/workspace.html", "ci/unified-ci.yml", "deploy/docker-compose.unified.yml",
                 "deploy/Dockerfile.platform", "deploy/prometheus.unified.yml",
                 "deploy/schema_unified_postgres.sql",
                 "scripts/demo_e2e.py", "scripts/validate_deploy.py", "scripts/verify_install.py",
                 "scripts/start_stack.sh", "scripts/stop_stack.sh",
                 "rtm/build_unified_rtm.py", "rtm/build_unified_manifest.py",
                 "rtm/seed_dopolnenie2.json",
                 "rtm/artifacts/RTM_UNIFIED.csv", "rtm/artifacts/RTM_UNIFIED.json",
                 "README.md", "WORKPLAN.md", "docs/00_UNIFIED_OVERVIEW.md", "docs/01_OBSERVATIONS.md",
                 "docs/02_REPO_ANALYSIS_2026-10-26.md", "docs/03_IP_AUDIT_LETTER_DRAFT.md",
                 "docs/04_LINE_A_CHANGES.md")]
    entries += [file_entry(os.path.join("unified", "bridge", "tests", f), REPO) for f in
                sorted(os.listdir(os.path.join(UNIFIED, "bridge", "tests")))
                if f.startswith("test_") and f.endswith(".py")]
    # последние протоколы каждого типа
    for pat in ("ПРОТОКОЛ_ARC002_XVER_*.json", "ПРОТОКОЛ_UNIF_E2E_*.json",
                "ПРОТОКОЛ_CROSS_ANCHOR_*.json", "ОТЧЁТ_VALIDATE_DEPLOY_*.json"):
        hits = sorted(glob.glob(os.path.join(UNIFIED, "protocols", pat)))
        if hits:
            entries.append(file_entry(hits[-1], REPO))
    missing = [e["path"] for e in entries if not e.get("present")]
    return {"title": "Единая программа — артефакты unified/",
            "items_total": len(entries), "items_intact": len(entries) - len(missing),
            "missing": missing, "entries": entries}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "artifacts", "MANIFEST_UNIFIED.json"))
    args = ap.parse_args()

    a, b, c = section_a(), section_b(), section_c()
    problems = []
    for it in a["items"]:
        if it["integrity"] == "ИЗМЕНЁН/ОТСУТСТВУЕТ":
            problems.append(f"A/{it['n']} {it['item']}: хэши не совпали")
    if b.get("vendor_verdict") != "соответствует":
        problems.append(f"B/вендор ENG6: {b.get('vendor_verdict')}")
    problems += [f"C/{m}: отсутствует" for m in c["missing"]]

    verdict = "COMPLETE" if not problems else ("DEGRADED" if not c["missing"] and b.get("vendor_verdict") == "соответствует" else "INCOMPLETE")
    manifest = {
        "document": "МАНИФЕСТ ЕДИНОЙ ПОСТАВКИ «Афина» (линия A + линия B + unified)",
        "manifest_version": VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hash_algorithm": "sha256 (пересчёт в момент генерации)",
        "verdict": verdict,
        "problems": problems,
        "sections": {"A_line_afina": a, "B_line_eng6": b, "C_unified": c},
        "summary": {
            "A": f"{a['items_intact']}/{a['items_total']} позиций без изменений хэшей",
            "B": f"{b['items_intact']}/{b['items_total']} позиций на месте; вендор — {b.get('vendor_verdict')}",
            "C": f"{c['items_intact']}/{c['items_total']} артефактов на месте",
        },
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"МАНИФЕСТ: вердикт {verdict}")
    for k, v in manifest["summary"].items():
        print(f"  {k}: {v}")
    if problems:
        print("  проблемы:")
        for p in problems[:10]:
            print("   -", p)
    print(f"  -> {args.out}")
    return 0 if verdict in ("COMPLETE", "DEGRADED") else 1


if __name__ == "__main__":
    sys.exit(main())
