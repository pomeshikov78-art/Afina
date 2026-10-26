"""unified/bridge/audit_bridge.py — единый аудит совмещённой платформы (UNIF-13, шаг 9).

Мастер-журнал платформы — хэш-цепочка «Афины» (server/core.py). Журнал контура
ENG6-PLAN — NDJSON (sha256 входа БЕЗ координат, требование Военпреда). Настоящий
модуль связывает их append-only леджером с собственной хэш-цепочкой:

  запись = {seq, ts, event, run_id, engine_task_id, input_sha256, actor,
            status, prev_hash, hash}

* run_id          — паспорт запуска платформы (CALC-YYYY-XXXXXX, ENG-001);
* engine_task_id  — taskId/sync-идентификатор задачи контура;
* input_sha256    — sha256 канонического JSON запроса (совпадает с inputSha256
                    в NDJSON контура — сквозная связка двух журналов);
* verify()        — пересчёт цепочки; подмена/удаление строки обнаруживается
                    (индекс первого разрыва);
* anchor_engine_audit() — выбирает записи журнала контура по inputSha256 и
                    прикладывает их sha256-отпечаток к записи леджера (якорь).

Формат — NDJSON (append-only), как в линии B; ротация — по регламенту платформы.
stdlib-only, детерминированно (hash от канонического JSON без поля hash).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

VERSION = "1.0.0"
GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(entry: Dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()


class UnifiedAuditLedger:
    """Append-only NDJSON-леджер с хэш-цепочкой (связка run_id ↔ taskId ↔ sha256)."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            open(self.path, "a", encoding="utf-8").close()

    def _last(self) -> Dict[str, Any]:
        last = {"seq": 0, "hash": GENESIS}
        if os.path.getsize(self.path) == 0:
            return last
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = json.loads(line)
        return last

    def record(self, event: str, run_id: Optional[str] = None,
               engine_task_id: Optional[str] = None, input_sha256: Optional[str] = None,
               actor: str = "afina-platform", status: Optional[str] = None,
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        prev = self._last()
        entry: Dict[str, Any] = {
            "seq": prev["seq"] + 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event,
            "run_id": run_id,
            "engine_task_id": engine_task_id,
            "input_sha256": input_sha256,
            "actor": actor,
            "status": status,
            "prev_hash": prev["hash"],
        }
        if extra:
            entry["extra"] = extra
        entry["hash"] = _hash(entry)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_canon(entry) + "\n")
        return entry

    def entries(self) -> List[Dict[str, Any]]:
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify(self) -> Dict[str, Any]:
        """Пересчёт цепочки: ok=False + broken_at при подмене/удалении/перестановке."""
        prev_hash, prev_seq = GENESIS, 0
        for i, e in enumerate(self.entries()):
            if e.get("prev_hash") != prev_hash or e.get("seq") != prev_seq + 1 \
                    or _hash(e) != e.get("hash"):
                return {"ok": False, "broken_at": i, "entries": i + 1}
            prev_hash, prev_seq = e["hash"], e["seq"]
        return {"ok": True, "entries": prev_seq}

    def anchor_engine_audit(self, audit_items: List[Dict[str, Any]],
                            input_sha256: str, run_id: str,
                            engine_task_id: Optional[str] = None) -> Dict[str, Any]:
        """Якорь: прикладывает отпечаток записей журнала контура к нашему леджеру.

        audit_items — записи GET /api/v1/audit контура ENG6-PLAN; отбираются те,
        чей inputSha256 совпал с нашим (сквозная связка без раскрытия координат).
        """
        matched = [a for a in audit_items if a.get("inputSha256") == input_sha256]
        fp = hashlib.sha256(_canon(matched).encode("utf-8")).hexdigest()
        return self.record("engine_audit_anchored", run_id=run_id,
                           engine_task_id=engine_task_id, input_sha256=input_sha256,
                           status="anchored" if matched else "no_match",
                           extra={"matched_records": len(matched), "anchor_sha256": fp})
