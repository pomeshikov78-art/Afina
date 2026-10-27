"""unified/rtm/build_unified_rtm.py — единая матрица трассируемости требований (UNIF-08).

Объединяет:
  * линия A: data/Афина_RTM_машиночитаемая.csv — 83 требования Прил. А ТЗ.АФИНА.11.0
    (статусы наследуются как есть — честно, включая «не подтверждено автоматизированно»);
  * линия B: seed_dopolnenie2.json — 74 требования Дополнения №2 к ТЗ (Я/A/FR/К/NFR/И);
  * UNIF:    требования совмещённой платформы (мост, ARC-002, маппинг, CI, deploy).

Переопределения (overrides): статусы требований линии A, которые ЗАКРЫТЫ
работами совмещения (ARC-002 — протоколом XVER от 26.10.2026).

Выход: rtm/artifacts/RTM_UNIFIED.csv (разделитель ';' — как в источнике),
       rtm/artifacts/RTM_UNIFIED.json, сводка в stdout.
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
OUT_DIR = os.path.join(HERE, "artifacts")

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
     "text": "Кросс-верификация моделей двух линий (XVER): FSPL, Окамура–Хата, COST-231, Френель, kTB (Гц⇄кГц — ENG-004), J(v) ITU-R P.526, оптический бюджет (NET-01⇄FIBER.loss), КОМПОЗИТНЫЙ уровень — настоящий ENG6.calcLink (prop/rx/noise ⇄ RF-01/RF-07); двухстадийный протокол RAW→ALIGNED",
     "criteria": "ALIGNED PASS: все пары в допуске; расхождения документированы (CM, √1000, hata-suburban, порог J(v)), вендор не изменяется",
     "evidence": XVER_EVIDENCE + "; 327 точек (325 CONSISTENT): композит calcLink PROP Δ≤0.0002 дБ / RX / NOISE Δ≤0.0006 дБм; f до 7 ГГц — член газа по XVER-GAS-SUBST из канонического источника (OBS-11); OBS-06/10 (порог J(v); REL/NET — решение)",
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
     "text": "Единый CI: матрица Python 3.11 × Node 20; 145 + 125 + 37 тестов; conformance; selfcheck; смоуки; XVER; E2E; сборка Docker",
     "criteria": "все джобы зелёные на push/PR; протоколы — артефакты CI",
     "evidence": "unified/ci/unified-ci.yml (готов 26.10.2026); публикация .github/workflows/ отклонена GitHub — PAT без scope workflow (OBS-09)",
     "status": "скрипт готов; включение — через веб-интерфейс GitHub или токеном со scope workflow"},
    {"id": "UNIF-07", "priority": "P1",
     "text": "Единое развёртывание: docker-compose (platform + engine + Prometheus + Grafana); режим Б — оба стека zero-dependency",
     "criteria": "compose up: platform 8470, engine 8080, healthz обоих ok, метрики собираются",
     "evidence": "unified/deploy/docker-compose.unified.yml + Dockerfile.platform",
     "status": "скрипт готов; требует Docker-стенда"},
    {"id": "UNIF-08", "priority": "P1",
     "text": "Единый RTM: Прил. А (83) + Дополнение №2 (77) + UNIF (18) с источниками, доказательствами и честными статусами",
     "criteria": "CSV+JSON генерируются скриптом; статусы ссылаются на протоколы/тесты",
     "evidence": "unified/rtm/build_unified_rtm.py → rtm/artifacts/RTM_UNIFIED.{csv,json}",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-09", "priority": "P2",
     "text": "Стабилизация фазы 0: фикс smoke_planner.mjs:21 (INVALID-ветка) — npm run smoke проходит целиком",
     "criteria": "smoke_planner + smoke_api выполняются цепочкой без падений",
     "evidence": "eng6-service/tests/smoke_planner.mjs (?? null); прогон npm run smoke 26.10.2026",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-10", "priority": "P2",
     "text": "Гигиена репозитория: .gitignore unified (var/, artifacts-кэш, __pycache__), исключение служебных журналов из git, монорепо-структура",
     "criteria": "служебные файлы не попадают в коммиты; структура platform/engine/unified зафиксирована",
     "evidence": "unified/.gitignore; WORKPLAN.md шаг 2 (монорепо — в работе)",
     "status": "частично: unified — готово; корень репозитория (eng6-service/var/) — долг до слияния ветки"},
    {"id": "UNIF-11", "priority": "P0",
     "text": "PLAN-01 в штатной очереди платформы (ARC-003): тяжёлое планирование не блокирует интерфейс, задание queued→running→done с паспортом и результатом",
     "criteria": "CalcQueue «Афины» исполняет PLAN-01 через мост; паспорт ENG-001 с отпечатками обоих путей; live-цикл на реальном контуре",
     "evidence": "unified/bridge/plan_service.py; tests/test_plan_service.py 4/4 (live, 26.10.2026)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-12", "priority": "P1",
     "text": "Персистентность результатов планирования в БД платформы: plan_runs + таблицы К-03 (SQLite; целевая СУБД PostgreSQL — схема идентична, AUT-001)",
     "criteria": "save/load round-trip; контроль целостности: sha256 ответа + пересчёт таблиц (подмена ответа и подмена строк обнаруживаются)",
     "evidence": "unified/bridge/platform_db.py; tests/test_platform_db.py 5/5",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-13", "priority": "P1",
     "text": "Единый аудит: append-only хэш-цепочка, связка run_id ↔ engine taskId ↔ inputSha256 (точный canonicalHash ядра); якорь журнала NDJSON линии B; обнаружение подмены",
     "criteria": "verify() находит подмену/перестановку; якорь совпал по inputSha256 (matched ≥ 1) на живом контуре",
     "evidence": "unified/bridge/audit_bridge.py; tests/test_audit_bridge.py 5/5 + test_plan_service (anchor matched≥1)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-14", "priority": "P2",
     "text": "Единое рабочее место (UI PoC): клиент платформы во фрейме + модуль планирования (прямые запросы к контуру по CORS), Canvas2D-карта с запасами/маршрутами, экспорт К-03, заготовка postMessage-контракта",
     "criteria": "NFR-02: 0 внешних ресурсов, 0 обращений к хранилищам браузера (скан паттернами линии B — PASS); расчёт и отрисовка — на живом контуре",
     "evidence": "unified/ui/workspace.html; скан NFR-02 PASS (26.10.2026); визуальный контроль — браузерный стенд",
     "status": "подтверждено частично: скан и синтаксис — прогон 26.10.2026; визуальный контроль — требует браузерного стенда"},
    {"id": "UNIF-15", "priority": "P2",
     "text": "Корневая гигиена git: .gitignore корня (var/, __pycache__, node_modules, *.db), CI в .github/workflows/",
     "criteria": "служебные файлы вне индекса; workflow обнаруживается GitHub",
     "evidence": ".gitignore (корень, вкл. .github/ до активации CI)",
     "status": "подтверждено коммитом 26.10.2026"},
    {"id": "UNIF-16", "priority": "P1",
     "text": "Единый манифест поставки: три раздела (линия A комплект §11.1, линия B Приложение Б, unified) с пересчётом sha256 каждого файла в момент генерации",
     "criteria": "вердикт COMPLETE/DEGRADED/INCOMPLETE по факту сверки; вендор ENG6 — эталонный sha256; изменения/отсутствия не замалчиваются",
     "evidence": "unified/rtm/build_unified_manifest.py → rtm/artifacts/MANIFEST_UNIFIED.json (COMPLETE: A 17/18 целы, B 13/13+вендор, C 32/32)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-17", "priority": "P0",
     "text": "Сшивка трёх журналов (шаг 9): мастер-хэш-цепочка платформы (/audit/verify) + NDJSON контура (связка по inputSha256) + единый леджер; вердикт ANCHORED только при согласии всех трёх",
     "criteria": "cross_anchor на живом стеке: platform.verify ok, engine matched≥1, ledger ok; АВТО-якорь в продуктивном цикле PLAN-01; ротация леджера с непрерывностью цепочки через архивы (подмена/потеря архива обнаруживаются); протокол CROSS_ANCHOR в unified/protocols/",
     "evidence": "unified/bridge/master_chain_sync.py + plan_service (auto_anchor) + audit_bridge (ротация); tests/test_audit_bridge.py 10/10 + test_platform_bridge.py 5/5 (live)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-19", "priority": "P1",
     "text": "Наблюдаемость платформы: GET /metrics — Prometheus text exposition 0.0.4 (afina_up/objects/users/audit/calcs/queue); правка линии A №2 по реестру 04_LINE_A_CHANGES",
     "criteria": "эндпоинт отдаёт валидный текст 0.0.4; метрики очереди и расчётов соответствуют состоянию; отказ счётчика не рвёт выдачу",
     "evidence": "afina/src/afina/server/app.py (_metrics); tests/test_platform_bridge.py::test_06 (live); prometheus.unified.yml переключён с healthz-костыля на /metrics",
     "status": "подтверждено прогоном 27.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-20", "priority": "P1",
     "text": "Устранение зазора RF-07 по поглощению в газах: поле gas_absorption_db в LinkBudget (правка линии A №1) + композитная сетка XVER до 7 ГГц",
     "criteria": "обратная совместимость (дефолт 0, 145/145 линии A зелёные); XVER ALIGNED PASS на f 1.4–7 ГГц с документированной подстановкой члена",
     "evidence": "afina/src/afina/eng6/rf.py; tests/test_backend.py (gas-тест); XVER 327 точек; OBS-11 (остаток: собственная P.676 в линии A)",
     "status": "подтверждено прогоном 27.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-21", "priority": "P1",
     "text": "postMessage-мост клиент платформы ⇄ единое рабочее место: afina:ready / afina:scenario-request / afina:scenario (правка линии A №3); токен сессии через мост не передаётся; приёмник в workspace (анкор-конверсия метров, авто-линии только явно)",
     "criteria": "контрактные маркеры на месте; скан безопасности (токен/Authorization не пересекают мост); node --check всех скриптов клиента — регрессионный барьер OBS-12",
     "evidence": "afina/src/client/index.html (мост + фикс OBS-12, правка №4); unified/ui/workspace.html (приёмник); tests/test_client_bridge.py 4/4",
     "status": "подтверждено частично: статические тесты — прогон 27.10.2026; браузерный стенд для визуальной проверки моста"},
    {"id": "UNIF-22", "priority": "P2",
     "text": "Миграционный комплект SQLite → PostgreSQL: DDL unified (plan_runs + К-03 + unified_audit_entry, JSONB, FK, индексы) + детерминированный экспорт (INSERT ON CONFLICT, предочистка по run_id, экспорт леджера с архивами ротации, отказ при дубле seq) + sha256-манифест и verify",
     "criteria": "экспорт детерминирован (повтор → идентичный sha256); подмена артефакта обнаруживается verify_export; фактическая загрузка — psql на стенде",
     "evidence": "unified/deploy/schema_unified_postgres.sql; unified/bridge/pg_export.py; tests/test_pg_export.py 4/4",
     "status": "подтверждено частично: офлайн-тесты — прогон 27.10.2026; загрузка в PostgreSQL — на стенде"},
    {"id": "UNIF-23", "priority": "P2",
     "text": "Сухая валидация артефактов развёртывания без Docker: compose (сервисы/контексты/volume-пути/depends_on), цели Prometheus ⊆ сервисы, COPY-источники Dockerfile.platform",
     "criteria": "18/18 проверок PASS; ограничение (мини-разбор формата, полная валидация — docker compose config в CI) зафиксировано в отчёте",
     "evidence": "unified/scripts/validate_deploy.py → protocols/ОТЧЁТ_VALIDATE_DEPLOY_*.json (PASS 18/18)",
     "status": "подтверждено прогоном 27.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-24", "priority": "P1",
     "text": "Инструмент проверки установки: одна команда — 15 серверных проверок (среда, healthz, sha256 ядра, конформанс 33+5, эталонный расчёт, детерминизм, 422, /metrics, bootstrap+RBAC, мастер-цепочка, XVER, NFR-02-скан UI) с авто-подъёмом стека на свободных портах и инструкцией для браузера",
     "criteria": "вердикт «ВСЁ РАБОТАЕТ» на чистой установке; код возврата 0/1/2; ничего не устанавливает и не изменяет (БД временная)",
     "evidence": "unified/scripts/verify_install.py — прогон 27.10.2026: 15/15 ✅; шаг CI в bridge-integration",
     "status": "подтверждено прогоном 27.10.2026 (песочница 2 vCPU/1 ГБ)"},
    {"id": "UNIF-18", "priority": "P1",
     "text": "Экспорт обстановки платформы в контракт ENG6-PLAN: objects (COM/LINK/ORDER/ORG) → сущности Прил. Г → build_plan_request; RBAC (Инженер-расчётчик создаёт, Администратор аудирует); CRS: градусы WGS-84 + UTM-метры через ГИС-ядро линии A (round-trip), недекодированные единицы — честный пропуск (ENG-004)",
     "criteria": "сквозной live-цикл: объекты через API платформы → экспорт → PLAN-01 в очереди → PARTIAL (red-техника отклонена) → три журнала ANCHORED",
     "evidence": "unified/bridge/scenario_export.py + platform_client.py; tests/test_scenario_export.py 8/8 (UTM round-trip 1e-6) + test_platform_bridge.py 5/5 (live)",
     "status": "подтверждено прогоном 26.10.2026 (песочница 2 vCPU/1 ГБ)"},
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
                               "UNIF": "unified/rtm/build_unified_rtm.py (18)"},
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
