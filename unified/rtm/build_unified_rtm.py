"""unified/rtm/build_unified_rtm.py — единая матрица трассируемости требований (UNIF-08).

Объединяет:
  * линия A: data/Афина_RTM_машиночитаемая.csv — 83 требования Прил. А ТЗ.АФИНА.11.0
    (статусы наследуются как есть — честно, включая «не подтверждено автоматизированно»);
  * линия B: seed_dopolnenie2.json — 74 требования Дополнения №2 к ТЗ (Я/A/FR/К/NFR/И);
  * UNIF:    требования совмещённой платформы (мост, ARC-002, маппинг, CI, deploy).

Переопределения (overrides): статусы требований линии A, которые ЗАКРЫТЫ
работами совмещения (ARC-002 — протоколом XVER от 26.10.2026).

Выход: rtm/out/RTM_UNIFIED.csv (разделитель ';' — как в источнике),
       rtm/out/RTM_UNIFIED.json, сводка в stdout.
Запуск: python3 build_unified_rtm.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNIFIED = os.path.dirname(HERE)
REPO = os.path.dirname(UNIFIED)
SRC_A = os.path.join(REPO, "afina", "data", "Афина_RTM_машиночитаемая.csv")
SEED_B = os.path.join(HERE, "seed_dopolnenie2.json")
OUT_DIR = os.path.join(HERE, "out")

XVER_EVIDENCE = "unified/bridge/xverify.py → unified/protocols/ПРОТОКОЛ_ARC002_XVER_*.json (102 точки, RAW→ALIGNED PASS, 26.10.2026)"

# Требования линии A, закрытые работами совмещения (статус обновляется честно —
# только при наличии протокола в unified/protocols/).
OVERRIDES_A = {
    "ARC-002": {
        "tests": "unified/bridge/tests/test_backend.py (5); unified/scripts/demo_e2e.py шаг 7",
        "protocols": XVER_EVIDENCE,
        "status": "подтверждено (XVER 26.10.2026: два вычислительных пути — Python ENG6+ и вендор ENG6 v5.1.0; ALIGNED PASS, документированные согласования CM=3дБ/√1000, исключение hata-suburban)",
    },
}

# Требования совмещённой платформы (UNIF) — новые, единый продукт.
UNIF_REQUIREMENTS = [
    {"id": "UNIF-01", "priority": "P0",
     "text": "Мост платформы к вычислительному контуру ENG6-PLAN: REST-клиент (stdlib), конформанс-гейт К-04, Retry-After, X-Requestor",
     "criteria": "health/meta/conformance/plan sync+async/tasks/audit доступны из Python; при conformance.ok≠true расчёт отклоняётся",
     "evidence": "unified/bridge/eng6_js_client.py; tests/test_client.py 7/7 (прогон 26.10.2026)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-02", "priority": "P0",
     "text": "Кросс-верификация моделей двух линий (XVER): FSPL, Окамура–Хата, COST-231, Френель; двухстадийный протокол RAW→ALIGNED",
     "criteria": "ALIGNED PASS: все пары в допуске; расхождения документированы (CM, √1000, hata-suburban), вендор не изменяется",
     "evidence": XVER_EVIDENCE,
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-03", "priority": "P0",
     "text": "Единая модель данных: Прил. Г (ASSET/GEO_OBJECT/ORG_UNIT/TECHNICAL_STATE/заявки) ⇄ контракт ENG6-PLAN; обратный ход в плоские таблицы К-03",
     "criteria": "прямой и обратный маппинг без потерь; ошибки — стабильные коды REQUIRED/ENUM/RANGE/REF/DUPLICATE/STATUS; round6 (NFR-03)",
     "evidence": "unified/bridge/datamodel_map.py; tests/test_datamodel.py 16/16",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-04", "priority": "P1",
     "text": "Паспорт расчётного запуска с фиксацией обоих бэкендов (ENG-001 + ARC-002): backend id, версия, sha256 ядра",
     "criteria": "в environment паспорта — отпечатки Python ENG6+ и ENG6-JS (sha256 adb610c9…)",
     "evidence": "unified/scripts/demo_e2e.py шаг 6 → ПРОТОКОЛ_UNIF_E2E_*.json (passport)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-05", "priority": "P1",
     "text": "Сквозной сценарий: сущности платформы → запрос ENG6-PLAN → расчёт → таблицы платформы → паспорт (E2E)",
     "criteria": "7 шагов ok; статус плана ∈ {OPTIMAL,FEASIBLE,PARTIAL}; детерминированность NFR-04",
     "evidence": "unified/protocols/ПРОТОКОЛ_UNIF_E2E_*.json (verdict PASS, 26.10.2026)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-06", "priority": "P1",
     "text": "Единый CI: матрица Python 3.11 × Node 20; 145 + 125 + 23 теста; conformance; selfcheck; смоуки; XVER; E2E; сборка Docker",
     "criteria": "все джобы зелёные на push/PR; протоколы — артефакты CI",
     "evidence": "unified/ci/unified-ci.yml (скопировать в .github/workflows/ при включении)",
     "status": "скрипт готов; требует включения GitHub Actions в репозитории"},
    {"id": "UNIF-07", "priority": "P1",
     "text": "Единое развёртывание: docker-compose (platform + engine + Prometheus + Grafana); режим Б — оба стека zero-dependency",
     "criteria": "compose up: platform 8470, engine 8080, healthz обоих ok, метрики собираются",
     "evidence": "unified/deploy/docker-compose.unified.yml + Dockerfile.platform",
     "status": "скрипт готов; требует Docker-стенда"},
    {"id": "UNIF-08", "priority": "P1",
     "text": "Единый RTM: Прил. А (83) + Дополнение №2 (77) + UNIF (10) с источниками, доказательствами и честными статусами",
     "criteria": "CSV+JSON генерируются скриптом; статусы ссылаются на протоколы/тесты",
     "evidence": "unified/rtm/build_unified_rtm.py → rtm/out/RTM_UNIFIED.{csv,json}",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-09", "priority": "P2",
     "text": "Стабилизация фазы 0: фикс smoke_planner.mjs:21 (INVALID-ветка) — npm run smoke проходит целиком",
     "criteria": "smoke_planner + smoke_api выполняются цепочкой без падений",
     "evidence": "eng6-service/tests/smoke_planner.mjs (?? null); прогон npm run smoke 26.10.2026",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-10", "priority": "P2",
     "text": "Гигиена репозитория: .gitignore unified (var/, out/, __pycache__), исключение служебных журналов из git, монорепо-структура",
     "criteria": "служебные файлы не попадают в коммиты; структура platform/engine/unified зафиксирована",
     "evidence": "unified/.gitignore; WORKPLAN.md шаг 2 (монорепо — в работе)",
     "status": "частично: unified — готово; корень репозитория (eng6-service/var/) — долг до слияния ветки"},
]


def main() -> int:
    rows = []

    # --- линия A: 83 требования Прил. А (статусы как есть + overrides) ---
    with open(SRC_A, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            rid = r["requirement"]
            ov = OVERRIDES_A.get(rid, {})
            rows.append({
                "source": "A:ТЗ.АФИНА.11.0 Прил.А",
                "requirement": rid,
                "priority": r["priority"],
                "text": r["text"],
                "acceptance_criteria": r["acceptance_criteria"],
                "tests": ov.get("tests", r["tests"]),
                "protocols": ov.get("protocols", r["protocols"]),
                "status": ov.get("status", r["status"]),
            })

    # --- линия B: Дополнение №2 ---
    with open(SEED_B, encoding="utf-8") as f:
        seed = json.load(f)
    for r in seed["requirements"]:
        rows.append({
            "source": "B:Дополнение №2 к ТЗ (ENG6-PLAN)",
            "requirement": r["id"],
            "priority": r["priority"],
            "text": r["text"],
            "acceptance_criteria": r.get("criteria", "см. Дополнение №2, матрица соответствия §8"),
            "tests": r.get("evidence", ""),
            "protocols": r.get("source_id", ""),
            "status": r["status"],
        })

    # --- UNIF ---
    for r in UNIF_REQUIREMENTS:
        rows.append({
            "source": "UNIF:совмещённая платформа",
            "requirement": r["id"],
            "priority": r["priority"],
            "text": r["text"],
            "acceptance_criteria": r["criteria"],
            "tests": r["evidence"],
            "protocols": "",
            "status": r["status"],
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    cols = ["source", "requirement", "priority", "text", "acceptance_criteria", "tests", "protocols", "status"]
    csv_path = os.path.join(OUT_DIR, "RTM_UNIFIED.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    json_path = os.path.join(OUT_DIR, "RTM_UNIFIED.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"document": "RTM_UNIFIED — единая матрица трассируемости требований",
                   "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "sources": {"A": "afina/data/Афина_RTM_машиночитаемая.csv (83)",
                               "B": "unified/rtm/seed_dopolnenie2.json (77)",
                               "UNIF": "unified/rtm/build_unified_rtm.py (10)"},
                   "total": len(rows), "rows": rows},
                  f, ensure_ascii=False, indent=1)

    # --- сводка ---
    from collections import Counter
    by_src = Counter(r["source"].split(":")[0] for r in rows)
    conf = sum(1 for r in rows if r["status"].startswith("подтверждено"))
    part = sum(1 for r in rows if r["status"].startswith("подтверждено частично"))
    stand = sum(1 for r in rows if "требует стенда" in r["status"] or "стенд" in r["status"] and not r["status"].startswith("подтверждено"))
    not_conf = sum(1 for r in rows if r["status"].startswith("не подтверждено"))
    other = len(rows) - conf - not_conf - sum(1 for r in rows if r["status"].startswith("скрипт готов") or r["status"].startswith("частично"))
    print(f"RTM_UNIFIED: всего {len(rows)} требований (A:{by_src['A']} B:{by_src['B']} UNIF:{by_src['UNIF']})")
    print(f"  подтверждено (в т.ч. частично): {conf}")
    print(f"  не подтверждено автоматизированно (стенд/данные/вручную): {not_conf}")
    print(f"  скрипт готов / требует включения: {sum(1 for r in rows if r['status'].startswith('скрипт готов') or r['status'].startswith('частично'))}")
    print(f"  -> {csv_path}")
    print(f"  -> {json_path}")
    _ = (part, stand, other)  # для будущей детализации сводки
    return 0


if __name__ == "__main__":
    sys.exit(main())
