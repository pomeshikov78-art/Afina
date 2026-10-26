"""Генератор протокола испытаний военного профиля (MIL)."""
import json, os, sys, time, unittest, platform
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.abspath(os.path.join(ROOT, "..", "protocols"))
sys.path.insert(0, ROOT)

def main():
    os.makedirs(OUT, exist_ok=True)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_mil")
    r = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
    doc = {"document": "Протокол испытаний военного профиля (MIL-01…12, программная часть)",
           "date": time.strftime("%Y-%m-%d %H:%M:%S"),
           "environment": {"python": platform.python_version(), "platform": platform.platform()},
           "ran": r.testsRun, "failures": len(r.failures), "errors": len(r.errors),
           "verdict": "PASS" if r.wasSuccessful() else "FAIL",
           "note": "MIL-01…12: программная часть автоматизирована 12/12 (G5.2); аппаратурная часть (реальные СИ, изделия, нагружение) выполняется на стенде заказчика — программа в ПМИ §2"}
    json.dump(doc, open(os.path.join(OUT, f"ПРОТОКОЛ_MIL_{time.strftime('%Y%m%d')}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: doc[k] for k in ("ran", "failures", "errors", "verdict")}))
    return 0 if r.wasSuccessful() else 1

if __name__ == "__main__":
    sys.exit(main())
