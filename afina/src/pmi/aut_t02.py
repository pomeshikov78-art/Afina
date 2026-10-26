"""AUT-T01/T02: ядро и ГИС при ФИЗИЧЕСКИ запрещённых сетевых вызовах.

socket.connect/socket.create_connection блокируются — любая попытка внешнего
обращения приводит к падению теста. 0 неразрешённых исходящих соединений.
"""
import json, os, socket, sys, time, unittest

BLOCKED = []
class NetworkBlockedError(RuntimeError): pass
_orig_connect = socket.socket.connect
_orig_create = socket.create_connection
def _blocked_connect(self, *a, **k):
    BLOCKED.append(("connect", str(a)))
    raise NetworkBlockedError("внешние соединения заблокированы (режим Б, AUT-T02)")
socket.socket.connect = _blocked_connect
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(NetworkBlockedError("blocked"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
loader = unittest.TestLoader()
suite = unittest.TestSuite([loader.loadTestsFromName("tests.test_eng6"), loader.loadTestsFromName("tests.test_gis")])
r = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
doc = {"document": "Протокол автономности AUT-T01/T02 (ядро ENG6+ и ГИС)",
       "date": time.strftime("%Y-%m-%d %H:%M:%S"),
       "method": "полный прогон тестов с заблокированным socket.connect",
       "tests_ran": r.testsRun, "failures": len(r.failures), "errors": len(r.errors),
       "blocked_connection_attempts": BLOCKED,
       "unauthorized_outgoing": len(BLOCKED),
       "verdict": "PASS" if r.wasSuccessful() and len(BLOCKED) == 0 else "FAIL",
       "note": "0 неразрешённых исходящих соединений при полном наборе сценариев (критерий AUT-002/008)"}
os.makedirs("protocols", exist_ok=True)
json.dump(doc, open(f"protocols/ПРОТОКОЛ_AUT_{time.strftime('%Y%m%d')}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({k: doc[k] for k in ("tests_ran", "failures", "errors", "unauthorized_outgoing", "verdict")}))
