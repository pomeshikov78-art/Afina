"""unified/bridge/plan_service.py — PLAN-01 в штатной очереди платформы (UNIF-11, шаг 5).

Регистрирует обработчик «PLAN-01» в CalcQueue «Афины» (afina/eng6/runner.py,
ARC-003): тяжёлое планирование не блокирует интерфейс платформы, выполняется
вычислительным контуром ENG6-PLAN через мост, сопровождается паспортом ENG-001
с отпечатками бэкендов, сохраняется в БД платформы (platform_db, UNIF-12) и
регистрируется в едином аудит-леджере (audit_bridge, UNIF-13).

Payload задания (оба варианта):
  {"request": <готовый контракт ENG6-PLAN>}                 — прямой запрос;
  {"assets": […], "geo_objects": […], "relations": […],     — сущности Прил. Г
   "orders": […], "org_units": {…}, "constraints": {…},       (маппинг на месте)
   "environment": {…}, "meta": {…}}
  + необязательные: "mode": "auto"|"sync"|"async", "actor": "Иванов И.И."

Результат задания: {run_id, status, grade, makespan_min, tables:{…счётчики},
passport, engine_task_id, audit_seq, anchors}.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)
AFINA_SRC = os.path.join(os.path.dirname(os.path.dirname(BRIDGE_DIR)), "afina", "src")
if AFINA_SRC not in sys.path:
    sys.path.insert(0, AFINA_SRC)

from afina.eng6.runner import CalcQueue  # noqa: E402  (очередь платформы, ARC-003)

from audit_bridge import UnifiedAuditLedger  # noqa: E402
from backend_arc002 import (Eng6PlanBackend, Eng6ProbeBackend, make_passport,  # noqa: E402
                            stable_hash)
from datamodel_map import build_plan_request, plan_to_platform_tables  # noqa: E402
from eng6_js_client import Eng6JsClient  # noqa: E402
from master_chain_sync import cross_anchor  # noqa: E402
import platform_db  # noqa: E402

VERSION = "1.1.0"


class UnifiedPlanService:
    """Связка: очередь платформы → мост → ENG6-PLAN → БД + аудит + паспорт.

    platform_client (опционально) — клиент сервера «Афины» с ролью, имеющей
    audit_read: при auto_anchor=True после каждого прогона выполняется сшивка
    трёх журналов (UNIF-17) и её вердикт возвращается в результате задания.
    """

    def __init__(self, db_path: str, engine_url: str = "http://127.0.0.1:8080",
                 ledger_path: Optional[str] = None, requestor: str = "afina-platform",
                 requestor_header: str = "afina-platform",
                 platform_client=None, auto_anchor: bool = True):
        self.db_path = db_path
        self.client = Eng6JsClient(engine_url, requestor=requestor_header)
        self.backend = Eng6PlanBackend(self.client)
        self.ledger = UnifiedAuditLedger(ledger_path or os.path.join(
            os.path.dirname(os.path.abspath(db_path)) or ".", "unified_audit.ndjson"))
        self.requestor = requestor
        self.platform_client = platform_client
        self.auto_anchor = auto_anchor
        self._probe_fp: Optional[Dict[str, Any]] = None

    def _probe_fingerprint(self) -> Optional[Any]:
        """Отпечаток второго пути (зонд вендора) — best effort: недоступность node
        не блокирует расчёт, но честно фиксируется предупреждением в паспорте."""
        if self._probe_fp is None:
            try:
                self._probe_fp = Eng6ProbeBackend().fingerprint()
            except Exception:
                self._probe_fp = False  # кэш отрицательного результата
        return self._probe_fp or None

    def register(self, queue: CalcQueue, function: str = "PLAN-01") -> None:
        queue.register(function, self.handle)

    # ---------- обработчик очереди ----------
    def handle(self, payload: Dict[str, Any], progress_cb=None) -> Dict[str, Any]:
        cb = progress_cb or (lambda p: None)
        actor = payload.get("actor") or self.requestor
        pre_warnings = []

        # 1. запрос: готовый контракт или сборка из сущностей Прил. Г
        if "request" in payload:
            request = payload["request"]
        else:
            request = build_plan_request(
                assets=payload.get("assets") or [],
                geo_objects=payload.get("geo_objects") or [],
                relations=payload.get("relations") or [],
                orders=payload.get("orders") or [],
                org_units=payload.get("org_units"),
                constraints=payload.get("constraints"),
                environment=payload.get("environment"),
                meta=payload.get("meta"))
        request_sha = stable_hash(request)   # платформенный канонический хэш (паспорт/БД)
        cb(0.1)

        # 1б. точный canonicalHash ядра (тот же, что пишет в NDJSON аудит линии B) —
        # ключ сквозной связки журналов. Best effort: недоступность node honestly
        # фиксируется, связка выполняется платформенным хэшем.
        probe_backend: Optional[Eng6ProbeBackend] = None
        engine_sha: Optional[str] = None
        try:
            probe_backend = Eng6ProbeBackend()
            engine_sha = probe_backend.canonical_hash(request)
        except Exception as e:
            pre_warnings.append(f"канонический хэш ядра недоступен ({type(e).__name__}) — "
                                "сквозная связка аудитов по inputSha256 не выполнена")

        # 2. расчёт через контур (конформанс-гейт К-04 — внутри backend.plan)
        mode = payload.get("mode", "auto")
        response = self.backend.plan(request, mode=mode)
        engine_task_id = response.get("taskId")
        cb(0.6)

        # 3. паспорт ENG-001 с отпечатками вычислительных путей
        fps = [self.backend.fingerprint()]
        if probe_backend is not None:
            try:
                fps.append(probe_backend.fingerprint())
            except Exception:
                probe_backend = None
        if probe_backend is None:
            pre_warnings.append("второй вычислительный путь (зонд вендора) недоступен — "
                                "ARC-002 сверка этим запуском не подтверждается")
        passport = make_passport("PLAN-01", "ENG6-PLAN", {
            "request_sha256": request_sha,
            "engine_input_sha256": engine_sha,
            "nodes": len(request.get("topology", {}).get("nodes", [])),
            "links": len(request.get("topology", {}).get("links", [])),
            "demands": len(request.get("demands", [])),
            "mode": mode,
        }, fps, mode="Engineering")

        status = response.get("status")
        ok_status = status in ("OPTIMAL", "FEASIBLE", "PARTIAL")
        metrics = response.get("metrics") or {}
        issues = ((response.get("diagnostics") or {}).get("issues") or [])
        passport.finish(status="done" if ok_status else "error",
                        results_ref=stable_hash(response),
                        warnings=pre_warnings + [f"{i.get('sev')}/{i.get('code')}" for i in issues[:6]],
                        applicability={"terrain": ((request.get("environment") or {}).get("terrain") or {}).get("source", "default")},
                        uncertainty_db={"data": 1.2, "numeric": 0.3, "model_params": 3.8, "physical_model": 2.1})
        run_id = passport.run_id
        cb(0.75)

        # 4. обратный маппинг + персистентность (UNIF-12)
        tables = plan_to_platform_tables(response, run_id=run_id)
        conn = platform_db.connect(self.db_path)   # отдельное соединение: очередь — другой поток
        try:
            platform_db.save_run(conn, run_id, request, response, passport.to_dict(),
                                 tables, engine_task_id=engine_task_id,
                                 engine_input_sha256=engine_sha)
        finally:
            conn.close()
        cb(0.85)

        # 5. единый аудит (UNIF-13): запись + якорь журнала контура по inputSha256
        entry = self.ledger.record("plan_completed", run_id=run_id,
                                   engine_task_id=engine_task_id,
                                   input_sha256=engine_sha or request_sha,
                                   actor=actor, status=status,
                                   extra={"grade": metrics.get("grade"), "mode": mode,
                                          "request_sha256_platform": request_sha})
        anchors: Dict[str, Any] = {"matched": None, "note": None}
        if engine_sha:
            try:
                audit = self.client.audit(limit=50)
                items = audit.get("items") or audit.get("entries") or []
                anchor = self.ledger.anchor_engine_audit(items, engine_sha, run_id, engine_task_id)
                anchors = {"matched": (anchor.get("extra") or {}).get("matched_records"),
                           "anchor_sha256": (anchor.get("extra") or {}).get("anchor_sha256")}
            except Exception as e:  # якорь — best effort: журнал контура может быть недоступен
                anchors["note"] = f"якорь не приложен: {type(e).__name__}: {e}"
        else:
            anchors["note"] = "якорь пропущен: нет канонического хэша ядра"

        # 6. авто-сшивка трёх журналов (UNIF-17): платформа + контур + леджер
        cross: Dict[str, Any] = {"performed": False}
        if self.platform_client is not None and self.auto_anchor and engine_sha:
            try:
                proto = cross_anchor(self.ledger, self.platform_client, self.client,
                                     run_id=run_id, engine_task_id=engine_task_id,
                                     input_sha256=engine_sha, actor=actor)
                cross = {"performed": True, "verdict": proto["verdict"], "reasons": proto["reasons"]}
            except Exception as e:
                cross = {"performed": True, "verdict": "ERROR",
                         "reasons": [f"{type(e).__name__}: {e}"]}
        cb(1.0)

        return {
            "run_id": run_id,
            "status": status,
            "grade": metrics.get("grade"),
            "makespan_min": ((response.get("plan") or {}).get("schedule") or {}).get("makespanMin"),
            "engine_task_id": engine_task_id,
            "tables": {k: len(v) for k, v in tables.items() if v},
            "passport": passport.to_dict(),
            "audit_seq": entry["seq"],
            "anchors": anchors,
            "cross_anchor": cross,
            "request_sha256": request_sha,
            "engine_input_sha256": engine_sha,
        }


def submit_plan(queue: CalcQueue, payload: Dict[str, Any], function: str = "PLAN-01") -> str:
    return queue.submit(function, payload)


def wait_job(queue: CalcQueue, job_id: str, timeout_s: float = 120.0,
             poll_s: float = 0.1) -> Dict[str, Any]:
    """Ожидание завершения задания очереди; возвращает снимок задания."""
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = queue.snapshot()
        for j in snap["jobs"]:
            if j["job_id"] == job_id:
                if j["status"] in ("done", "error"):
                    return j
                break
        time.sleep(poll_s)
    raise TimeoutError(f"задание {job_id} не завершилось за {timeout_s} с")
