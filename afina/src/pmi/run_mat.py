"""Генератор протоколов испытаний (§13.1, ПМИ): прогоняет unittest-наборы и
фиксирует результат по каждому испытанию MAT-01…10 + ГИС + интеграция.
Выход: protocols/ПРОТОКОЛ_<дата>.md + .json. Основание приёмки — доказательства (§15).
"""
import json, os, sys, time, unittest, platform

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.abspath(os.path.join(ROOT, "..", "protocols"))
sys.path.insert(0, ROOT)

MAP = {  # испытание → (модуль, класс/тесты)
 "MAT-01 Аналитический эталон": ("tests.test_eng6", "MAT01_AnalyticReference"),
 "MAT-02 Размерности": ("tests.test_eng6", "MAT02_Dimensions"),
 "MAT-03 Сходимость": ("tests.test_eng6", "MAT03_Convergence"),
 "MAT-04 Границы применимости": ("tests.test_eng6", "MAT04_Applicability"),
 "MAT-05 Неопределённость": ("tests.test_eng6", "MAT05_Uncertainty"),
 "MAT-06 Сопряжение": ("tests.test_eng6", "MAT06_CoSimulation"),
 "MAT-07 Воспроизводимость": ("tests.test_eng6", "MAT07_Reproducibility"),
 "MAT-08 Суррогат": ("tests.test_eng6", "MAT08_Surrogate"),
 "MAT-09 Оптимизатор": ("tests.test_eng6", "MAT09_Optimizer"),
 "MAT-10 Регрессия": ("tests.test_eng6", "MAT10_Regression"),
 "GIS-001…004 (CRS/форматы/round-trip)": ("tests.test_gis", None),
 "ARC-003 Очередь": ("tests.test_eng6", "QueueTests"),
}

def main():
    os.makedirs(OUT, exist_ok=True)
    results = []
    loader = unittest.TestLoader()
    for name, (mod, cls) in MAP.items():
        suite = loader.loadTestsFromName(f"{mod}.{cls}" if cls else mod)
        r = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0).run(suite)
        results.append({"test": name, "module": mod, "class": cls or "все",
                        "ran": r.testsRun, "failures": len(r.failures), "errors": len(r.errors),
                        "verdict": "PASS" if r.wasSuccessful() else "FAIL",
                        "details": [str(f[0]) for f in r.failures + r.errors]})
    doc = {"document": "Протокол математических и геоинформационных испытаний",
           "date": time.strftime("%Y-%m-%d %H:%M:%S"),
           "environment": {"python": platform.python_version(), "platform": platform.platform(),
                            "backend": "CPU-reference (эталонная реализация G1; целевой C++/Vulkan — G2+)"},
           "note": "Протокол фиксирует фактические результаты; требование со статусом «реализовано» не считается принятым до проверки (§11.2).",
           "results": results,
           "summary": {"total": sum(x["ran"] for x in results),
                        "passed_suites": sum(1 for x in results if x["verdict"] == "PASS"),
                        "failed_suites": sum(1 for x in results if x["verdict"] == "FAIL")}}
    jpath = os.path.join(OUT, f"ПРОТОКОЛ_MAT_{time.strftime('%Y%m%d')}.json")
    json.dump(doc, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    md = ["# Протокол испытаний MAT/GIS/ARC", f"Дата: {doc['date']}", f"Среда: {doc['environment']['platform']} · Python {doc['environment']['python']}", "",
          "| Испытание | Прогнано | Вердикт |", "|---|---|---|"]
    for x in results: md.append(f"| {x['test']} | {x['ran']} | **{x['verdict']}** |")
    md += ["", f"Итого: {doc['summary']['total']} тестов, suites PASS {doc['summary']['passed_suites']}/{len(results)}"]
    open(os.path.join(OUT, f"ПРОТОКОЛ_MAT_{time.strftime('%Y%m%d')}.md"), "w", encoding="utf-8").write("\n".join(md))
    print(json.dumps(doc["summary"], ensure_ascii=False))
    return 0 if doc["summary"]["failed_suites"] == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
