"""Генератор протокола интеграционных испытаний и безопасности (server E2E).

Один прогон класса ServerE2E (тесты используют общее состояние сессии — порядок важен),
вердикты раскладываются по требованиям ТЗ.
"""
import json, os, sys, time, unittest, platform
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.abspath(os.path.join(ROOT, "..", "protocols"))
sys.path.insert(0, ROOT)

MAP = [  # (требование/критерий, метод теста)
 ("Bootstrap-регистрация и контроль ролей при регистрации", "test_01_bootstrap_and_roles"),
 ("SEC-001 RBAC: аутентификация, отказы по матрице ролей", "test_02_auth_and_rbac"),
 ("INT-002 Идемпотентность операций (client_id)", "test_03_objects_and_idempotency"),
 ("SEC-004 Экспорт без закрытых полей и персональных данных", "test_04_export_sec004"),
 ("ENG-001/MOD-005 Паспорт расчёта через API (RF-03, 4 компонента неопределённости)", "test_05_calc_rf03_with_passport"),
 ("INT-001 Отрицательный тест: незарегистрированная функция отклонена (без тихой подмены)", "test_06_unknown_function_rejected"),
 ("SEC-005 Maker–Checker: цепочка замечания, само-согласование отклонено", "test_07_zam_maker_checker"),
 ("DGT-002 Телеметрия не изменяет базовую линию", "test_08_telemetry_does_not_touch_baseline"),
 ("SEC-003 Аудит: хэш-цепочка верифицируема, отказы залогированы", "test_09_audit_chain_and_denials_logged"),
 ("Многопользовательская сессия: блокировки объектов (409 конфликт)", "test_10_locks_multiuser"),
 ("Каталог 1940→2036: поиск, честная маркировка прогнозов", "test_11_catalog_db"),
 ("AI-001 ИИ-API: источники и ограничения в ответе; MIL-12", "test_12_ai_ask"),
 ("DAT-004 Сканер аномалий: политика арбитража", "test_13_ai_anomalies"),
 ("AI-003 Отключаемость ИИ без влияния на расчёты", "test_14_ai_disable_ai003"),
]

def main():
    os.makedirs(OUT, exist_ok=True)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_server.ServerE2E")
    res = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
    bad = {}
    for t, _ in res.failures + res.errors:
        bad[t._testMethodName] = True
    results = [{"requirement": r, "test": m, "verdict": "FAIL" if m in bad else "PASS"} for r, m in MAP]
    doc = {"document": "Протокол интеграционных испытаний и безопасности (сервер API)",
           "date": time.strftime("%Y-%m-%d %H:%M:%S"), "environment": platform.platform(),
           "python": platform.python_version(), "ran": res.testsRun,
           "results": results,
           "summary": {"pass": sum(1 for x in results if x["verdict"] == "PASS"), "total": len(results)}}
    json.dump(doc, open(os.path.join(OUT, f"ПРОТОКОЛ_SERVER_{time.strftime('%Y%m%d')}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    md = ["# Протокол интеграционных испытаний и безопасности", f"Дата: {doc['date']} · Среда: {doc['environment']}", "",
          "| Требование / критерий | Тест | Вердикт |", "|---|---|---|"]
    for x in results: md.append(f"| {x['requirement']} | {x['test']} | **{x['verdict']}** |")
    md += ["", f"Итого: {doc['summary']['pass']}/{doc['summary']['total']} PASS"]
    open(os.path.join(OUT, f"ПРОТОКОЛ_SERVER_{time.strftime('%Y%m%d')}.md"), "w", encoding="utf-8").write("\n".join(md))
    print(json.dumps(doc["summary"]))
    return 0 if doc["summary"]["pass"] == doc["summary"]["total"] else 1

if __name__ == "__main__": sys.exit(main())
