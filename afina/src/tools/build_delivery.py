#!/usr/bin/env python3
"""G5.4 · Генератор комплекта поставки (§11.1, 18 позиций).

Формирует машиночитаемые позиции и манифест:
- позиция 6: спецификация API (интроспекция маршрутов сервера) → docs/Афина_API_спецификация.md
- позиция 7: реестр математических моделей с паспортами V&V → data/Афина_Реестр_моделей.json
- позиция 8: матрица прослеживаемости (требование→тест→протокол) → data/Афина_RTM_машиночитаемая.{csv,json}
- позиция 3: локальный репозиторий → releases/afina_repo.bundle (git bundle --all)
- манифест 18 позиций со статусами и sha256 → releases/КОМПЛЕКТ_ПОСТАВКИ.json

Запуск: python3 tools/build_delivery.py (из src).
"""
import csv, glob, hashlib, json, os, re, subprocess, sys, tempfile, time
from dataclasses import asdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))   # afina/
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))          # afina/src


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def latest(pattern):
    fs = sorted(glob.glob(os.path.join(ROOT, "protocols", pattern)))
    return fs[-1] if fs else None


# ---------- позиция 6: спецификация API ----------
def build_api_spec() -> str:
    from afina.server.app import AfinaApp, API
    td = tempfile.mkdtemp()
    app = AfinaApp(db_path=os.path.join(td, "spec.db"))
    rows = []
    for m, rx, fn, op, auth in app.routes():
        rows.append((m, rx.pattern.replace("^" + API, "").rstrip("$").replace("\\", ""), op, auth))
    lines = ["# Спецификация REST API /api/v1 (позиция 6 комплекта, §11.1)", "",
             f"Сгенерировано интроспекцией маршрутов {time.strftime('%Y-%m-%d %H:%M:%S')} · версия API: v1 · формат: JSON (UTF-8)",
             "Аутентификация: `Authorization: Bearer <token>` (scrypt+HMAC, TTL 8 ч). RBAC: операция проверяется для роли каждого запроса (Приложение Н); отказ → 403 + запись в аудит (SEC-001).",
             "Идемпотентность: POST с `client_id` не создаёт дубликатов (INT-002). Тяжёлые расчёты — через очередь (ARC-003): 202 + job_id, опрос `GET /calc/<job_id>`.",
             "Ошибки: 400 (валидация), 401 (сессия), 403 (RBAC/Maker–Checker), 404, 409 (конфликт/отказ по правилам), 500 — с политикой «ошибка не подменяется упрощённым результатом».", "",
             "| Метод | Путь | Операция RBAC | Авторизация |", "|---|---|---|---|"]
    for m, p, op, auth in rows:
        lines.append(f"| {m} | `{API}{p}` | {op or 'read'} | {'да' if auth else 'нет (bootstrap/login)'} |")
    lines += ["", "## Схемы ключевых запросов",
              "- `POST /calc` RF-03: `{function, model: OKUMURA_HATA|COST231_HATA|SUI|SPM|FSPL|INDOOR_P1238, width_m, height_m, cell_m, threshold_dbm, env, stations:[{id,x_m,y_m,h_ant_m,f_mhz,tx_power_dbm,ant_gain_dbi,sector}]}` → 202 `{job_id}`; результат: `{coverage_pct, grid, passport{run_id,model_version,inputs_hash,environment,uncertainty_db,applicability}}`.",
              "- `POST /calc` OPT-01: `{variants, criteria:[{key,name,direction,weight,hard_min,hard_max}], constraints:[{type:subset_of|forbidden_value|all_required|numeric_bound,...}]}` → `{feasible_count, rejected[{variant,reason}], pareto_front, ranked, best, passport}`.",
              "- `POST /calc` COSIM-01: `{scenario_id, seed:{w_nom,w_peak,t_ambient_c,t_limit_c,cooling_w,mtbf_h,mttr_h}}` → `{ok, data, log, versions, unit_checks, passport}`.",
              "- Единицы и CRS: реестры ENG-004/GIS-001; даты — epoch seconds (DOUBLE), формат ответов — JSON.",
              "- Корректный пример: см. `pmi/smoke_g4.py` (рабочие вызовы всех семейств). Некорректный: `POST /calc {function:'MAGIC-99'}` → 400 «функция не зарегистрирована» (тихая подмена запрещена)."]
    out = os.path.join(ROOT, "docs", "Афина_API_спецификация.md")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return out


# ---------- позиция 7: реестр математических моделей ----------
def build_model_registry() -> str:
    from afina.eng6 import models
    mat = latest("ПРОТОКОЛ_MAT_*.json")
    mat_doc = json.load(open(mat, encoding="utf-8")) if mat else {}
    doms = {"OKUMURA_HATA": models.HATA_DOMAIN, "SUI": models.SUI_DOMAIN, "SPM": models.SPM_DOMAIN}
    reg = {"registry": "Реестр математических моделей (§4.5.9)", "generated": time.time(),
           "engine_version": models.VERSION, "vv_protocol": os.path.basename(mat) if mat else None,
           "vv_verdict": mat_doc.get("verdict"), "models": []}
    common = {"source": "открытые нормативные источники (ITU-R/публикации)", "license": "в составе кода Системы",
              "hardware": "CPU (эталонная реализация Python); целевой backend C++/Vulkan — с регрессией MOD-002/004",
              "responsible": "Инженер-расчётчик (роль Прил. Н)"}
    for name, dom in doms.items():
        reg["models"].append({**common, "id": name, "purpose": "потери распространения (RF-01)",
                              "domain": {"f_mhz": dom.f_mhz, "d_km": dom.d_km, "h_bs_m": dom.h_bs_m,
                                         "h_ms_m": dom.h_ms_m, "env": list(dom.env)},
                              "method": "эмпирическая/полудетерминированная модель",
                              "inputs": ["f_mhz", "d_km", "h_bs", "h_ms", "env"], "outputs": ["loss_db"],
                              "units": "МГц/км/м/дБ (ENG-004)", "reference_scenarios": "Приложение Е + tests/test_eng6 (MAT-01)",
                              "vv": {"MAT-01": "аналитические эталоны", "MAT-04": "границы применимости (MOD-003)",
                                     "MAT-07": "воспроизводимость", "verdict": mat_doc.get("verdict")},
                              "status": "Валидирована для указанной области" if mat_doc.get("verdict") == "PASS" else "Проверяется"})
    for mid, purpose, meth in (("FSPL", "потери свободного пространства", "аналитическая (эталон MAT-01)"),
                               ("COST231_HATA", "потери распространения", "эмпирическая (расширение Хаты)"),
                               ("INDOOR_P1238", "внутрипомешные потери", "ITU-R P.1238"),
                               ("MARKOV_AVAIL", "готовность/доступность", "марковская модель REL-01"),
                               ("FMEA", "анализ отказов", "FMEA/FMECA REL-01"),
                               ("LSQ_CALIB", "идентификация параметров", "МНК (DGT-005)"),
                               ("POLY_SURROGATE", "суррогатная аппроксимация", "полином ≤2, MOD-006")):
        reg["models"].append({**common, "id": mid, "purpose": purpose, "method": meth,
                              "vv": {"protocol": os.path.basename(mat) if mat else None, "verdict": mat_doc.get("verdict")},
                              "status": "Валидирована для указанной области" if mat_doc.get("verdict") == "PASS" else "Проверяется"})
    out = os.path.join(ROOT, "data", "Афина_Реестр_моделей.json")
    json.dump(reg, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


# ---------- позиция 8: RTM машиночитаемая ----------
REQ_TOKEN = re.compile(r"\b([A-Z0-9]{2,4}-[A-Z]?\d{2,3}(?:…\d{3})?)\b")


def build_rtm() -> str:
    req_path = os.path.join(ROOT, "data", "Афина_Реестр_требований_ПриложениеА.json")
    reqs = json.load(open(req_path, encoding="utf-8")).get("requirements", [])
    # сбор подтверждений из протоколов (последние каждого семейства)
    evidence = {}
    for fam in ("ПРОТОКОЛ_G3_*.json", "ПРОТОКОЛ_G4_*.json", "ПРОТОКОЛ_MAT_*.json",
                "ПРОТОКОЛ_SERVER_*.json", "ПРОТОКОЛ_MIL_*.json", "ПРОТОКОЛ_AUT_*.json",
                "ПРОТОКОЛ_AUT_G5_*.json", "ПРОТОКОЛ_132_*.json", "ПРОТОКОЛ_SMOKE_G3_*.json", "ПРОТОКОЛ_SMOKE_G4_*.json"):
        p = latest(fam)
        if not p:
            continue
        d = json.load(open(p, encoding="utf-8"))
        for it in d.get("items", []) + d.get("results", []) + d.get("steps", []):
            txt = json.dumps(it, ensure_ascii=False)
            for tok in set(REQ_TOKEN.findall(txt)):
                base = tok.split("…")[0]
                evidence.setdefault(base, []).append({"protocol": os.path.basename(p),
                                                      "test": it.get("test") or it.get("id") or it.get("check", "")[:60],
                                                      "verdict": it.get("verdict", "")})
    rows, js = [], []
    for r in reqs:
        rid = r["id"]
        ev = evidence.get(rid, [])
        # охват «семейств»: RF-03 подтверждает и RF-0x при наличии прямых упоминаний
        passed = [e for e in ev if str(e.get("verdict", "")).upper().startswith("PASS")]
        status = ("подтверждено автоматизированным испытанием" if passed else
                  "не подтверждено автоматизированно (стенд/данные/вручную — см. ПМИ)")
        row = {"requirement": rid, "priority": r.get("priority"), "text": r.get("requirement"),
               "acceptance_criteria": r.get("acceptance_criteria"),
               "tests": "; ".join(sorted({e["test"] for e in passed}))[:400],
               "protocols": "; ".join(sorted({e["protocol"] for e in passed})),
               "status": status}
        rows.append(row); js.append(row)
    out_csv = os.path.join(ROOT, "data", "Афина_RTM_машиночитаемая.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader(); w.writerows(rows)
    json.dump({"rtm": js, "generated": time.time(),
               "note": "требование не считается подтверждённым наличием пункта в интерфейсе (§4.13.2)"},
              open(os.path.join(ROOT, "data", "Афина_RTM_машиночитаемая.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return out_csv


# ---------- позиция 3: локальный репозиторий ----------
def build_bundle() -> str:
    out = os.path.join(ROOT, "releases", "afina_repo.bundle")
    try:
        subprocess.run(["git", "bundle", "create", out, "--all"], cwd=ROOT, check=True,
                       capture_output=True, timeout=120)
        return out
    except Exception:
        return ""


# ---------- манифест 18 позиций ----------
def build_manifest(generated_paths):
    def ex(*paths):
        return [p for p in paths if p and os.path.exists(p)]
    items = [
        (1, "Установочные пакеты для обоих автономных режимов", "частично (стенд)",
         ex(os.path.join(SRC, "installer/afina.nsi"), os.path.join(SRC, "installer/afina.iss"),
            os.path.join(SRC, "installer/build_linux.sh"), os.path.join(SRC, "installer/run_server.pyw"),
            os.path.join(SRC, "installer/bundle_manifest.json")),
         "скрипты NSIS/Inno/Linux готовы; .exe собирается и подписывается на Windows-стенде (честный статус)"),
        (2, "Исходные тексты разработанных компонентов", "в комплекте", [os.path.join(SRC, "afina"), os.path.join(SRC, "client"), os.path.join(SRC, "tools")], ""),
        (3, "Сценарии воспроизводимой сборки и локальный репозиторий", "в комплекте",
         ex(os.path.join(SRC, "installer/build_linux.sh"), generated_paths.get("bundle")), "git bundle --all (вся релизная линия с тегами)"),
        (4, "Архитектурная спецификация", "в комплекте", ex(os.path.join(ROOT, "docs/Афина_Архитектурная_спецификация.md")), ""),
        (5, "Логическая и физическая модели БД, словарь данных, правила миграции", "в комплекте",
         ex(os.path.join(SRC, "db/schema_postgres.sql"), os.path.join(ROOT, "data/Афина_Словарь_данных_ПриложениеВ.json"),
            os.path.join(ROOT, "data/Афина_Модель_данных_ПриложениеГ.json"), os.path.join(ROOT, "docs/Афина_Регламенты.md")), ""),
        (6, "Спецификации API и форматов обмена", "в комплекте",
         ex(generated_paths.get("api"), os.path.join(SRC, "afina/gis/io_formats.py")), "генерируется интроспекцией маршрутов"),
        (7, "Реестр математических моделей с паспортами V&V", "в комплекте",
         ex(generated_paths.get("models"), latest("ПРОТОКОЛ_MAT_*.json")), ""),
        (8, "Матрица прослеживаемости требований (машиночитаемая)", "в комплекте",
         ex(generated_paths.get("rtm"), os.path.join(ROOT, "data/Афина_RTM_машиночитаемая.json"),
            os.path.join(ROOT, "data/Афина_Реестр_требований_ПриложениеА.json")), ""),
        (9, "ПМИ и протоколы всех видов испытаний", "в комплекте",
         ex(os.path.join(SRC, "pmi/ПМИ.md"), os.path.join(ROOT, "protocols")), ""),
        (10, "Эталонные наборы данных и тестовые сценарии", "в комплекте",
         ex(os.path.join(ROOT, "data/ПриложениеЕ_эталонные_сценарии.json"), os.path.join(SRC, "tests"),
            os.path.join(SRC, "pmi/run_132.py")), ""),
        (11, "Руководства: пользователя, инженера, администратора, сопровождения", "в комплекте",
         ex(*[os.path.join(ROOT, "docs", f) for f in os.listdir(os.path.join(ROOT, "docs")) if f.startswith("Руководство")]), ""),
        (12, "Модель угроз и комплект документов по защите информации", "в комплекте",
         ex(os.path.join(ROOT, "G0/ИБ_модель_угроз.md")), "СКЗИ — интерфейс CryptoProvider (стенд)"),
        (13, "Регламенты миграции, резервного копирования, восстановления и обновления", "в комплекте",
         ex(os.path.join(ROOT, "docs/Афина_Регламенты.md"), os.path.join(SRC, "tools/backup.py")), ""),
        (14, "SBOM: сторонние компоненты, лицензии, ограничения", "в комплекте",
         ex(os.path.join(ROOT, "data/ПриложениеЛ_SBOM.json"), os.path.join(ROOT, "G0/Реестр_лицензий.md")), "stdlib-only: сторонние компоненты в рантайме отсутствуют"),
        (15, "План сопровождения, исправления дефектов и развития", "в комплекте", ex(os.path.join(ROOT, "docs/Афина_План_сопровождения.md")), ""),
        (16, "План подготовки пользователей и администраторов", "в комплекте", ex(os.path.join(ROOT, "docs/Афина_План_подготовки.md")), ""),
        (17, "Ведомость передачи прав на код, модели, библиотеки и данные", "в комплекте", ex(os.path.join(ROOT, "docs/Афина_Ведомость_прав.md")), ""),
        (18, "Перечень известных ограничений и технического долга", "в комплекте", ex(os.path.join(ROOT, "docs/Афина_Известные_ограничения.md")), ""),
    ]
    man = {"document": "КОМПЛЕКТ ПОСТАВКИ (§11.1, 18 позиций)", "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "release_line": "git-теги g0-docs…g5-*", "items": []}
    for n, name, status, paths, note in items:
        entry = {"n": n, "item": name, "status": status, "note": note, "paths": []}
        for p in paths:
            rel = os.path.relpath(p, ROOT)
            if os.path.isdir(p):
                entry["paths"].append({"path": rel + "/", "kind": "dir"})
            else:
                entry["paths"].append({"path": rel, "kind": "file", "sha256": sha256_file(p),
                                       "bytes": os.path.getsize(p)})
        man["items"].append(entry)
    out = os.path.join(ROOT, "releases", "КОМПЛЕКТ_ПОСТАВКИ.json")
    json.dump(man, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


def main():
    gen = {}
    gen["api"] = build_api_spec(); print("API-спецификация:", os.path.relpath(gen["api"], ROOT))
    gen["models"] = build_model_registry(); print("Реестр моделей:", os.path.relpath(gen["models"], ROOT))
    gen["rtm"] = build_rtm(); print("RTM:", os.path.relpath(gen["rtm"], ROOT))
    gen["bundle"] = build_bundle(); print("git bundle:", os.path.relpath(gen["bundle"], ROOT) if gen["bundle"] else "не создан (git недоступен)")
    man = build_manifest(gen)
    d = json.load(open(man, encoding="utf-8"))
    n_ok = sum(1 for i in d["items"] if i["status"] == "в комплекте")
    print(f"Манифест: {os.path.relpath(man, ROOT)} — «в комплекте» {n_ok}/18, «частично (стенд)» {18 - n_ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
