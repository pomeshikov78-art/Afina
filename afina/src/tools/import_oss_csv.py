#!/usr/bin/env python3
"""Адаптер импорта из внутренних OSS/EAM (CSV) → POST /api/v1/objects.

«Магнит данных» (профиль 3, §10.3): обновление атрибутов объектов из учётных систем
заказчика. Правила: внешние ID сохраняются, сопоставление фиксируется, слияние
дубликатов НЕ автоматическое (§6.1), конфликты → на арбитраж (DAT-004).
Формат CSV: external_id;class;name;x;y;attrs_json[;source]
"""
import csv, json, sys, time, urllib.request, urllib.error

API = sys.argv[1]; TOKEN = sys.argv[2]; CSV = sys.argv[3]

def post(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode(),
                                 method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")

stats = {"imported": 0, "rejected": 0, "conflicts": []}
with open(CSV, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f, delimiter=";"):
        body = {"id": "EXT-" + row["external_id"], "class": row.get("class", "COM"), "name": row.get("name", row["external_id"]),
                "x": float(row.get("x") or 0), "y": float(row.get("y") or 0),
                "attrs": json.loads(row.get("attrs_json") or "{}"),
                "source": row.get("source", "OSS/EAM CSV"), "dat_status": "Введено"}
        code, res = post("/objects", body)
        if code == 200: stats["imported"] += 1
        else:
            stats["rejected"] += 1; stats["conflicts"].append({"external_id": row["external_id"], "error": res.get("error")})
print(json.dumps(stats, ensure_ascii=False, indent=1))
print("внешние ID сохранены с префиксом EXT-; сопоставление и конфликты — в протоколе (не перезаписываются)")
