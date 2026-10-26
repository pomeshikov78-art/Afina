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
    """Append-only NDJSON-леджер с хэш-цепочкой (связка run_id ↔ taskId ↔ sha256).

    Ротация (max_bytes > 0): при превышении размера файл переименовывается в
    архив `<path>.<seq>.ndjson`, а новый файл НАЧИНАЕТСЯ с якорной записи
    `chain_rotation`, чей prev_hash = хэш последней записи архива — цепочка
    непрерывна через ротацию (регламент линии B: ротация по достижении 1 ГБ,
    хранение ≥ года; append-only语义 сохранена — архивы не перезаписываются).
    """

    def __init__(self, path: str, max_bytes: int = 0):
        self.path = path
        self.max_bytes = int(max_bytes or 0)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            open(self.path, "a", encoding="utf-8").close()

    def _entries_of(self, path: str) -> List[Dict[str, Any]]:
        out = []
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def _last(self) -> Dict[str, Any]:
        last = {"seq": 0, "hash": GENESIS}
        entries = self._entries_of(self.path)
        if entries:
            last = entries[-1]
        return last

    def _rotate_if_needed(self) -> Optional[str]:
        """Ротация по размеру; возвращает имя архива (или None)."""
        if not self.max_bytes or not os.path.exists(self.path):
            return None
        if os.path.getsize(self.path) <= self.max_bytes:
            return None
        last = self._last()
        archive = f"{self.path}.{last['seq']:08d}.ndjson"
        os.replace(self.path, archive)
        open(self.path, "a", encoding="utf-8").close()
        # якорная запись: цепочка продолжается через архив
        anchor: Dict[str, Any] = {
            "seq": last["seq"] + 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "chain_rotation",
            "run_id": None, "engine_task_id": None, "input_sha256": None,
            "actor": "audit-bridge", "status": "rotated",
            "prev_hash": last["hash"],
            "extra": {"archive": os.path.basename(archive), "entries_in_archive": last["seq"]},
        }
        anchor["hash"] = _hash(anchor)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_canon(anchor) + "\n")
        return archive

    def record(self, event: str, run_id: Optional[str] = None,
               engine_task_id: Optional[str] = None, input_sha256: Optional[str] = None,
               actor: str = "afina-platform", status: Optional[str] = None,
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._rotate_if_needed()
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
        return self._entries_of(self.path)

    def _verify_file(self, path: str, depth: int = 0) -> Dict[str, Any]:
        entries = self._entries_of(path)
        rotations = 0
        prev_hash, prev_seq = GENESIS, 0
        for i, e in enumerate(entries):
            if _hash(e) != e.get("hash"):
                return {"ok": False, "broken_at": i, "file": os.path.basename(path), "reason": "HASH_MISMATCH"}
            if i == 0:
                if e.get("event") == "chain_rotation":
                    if depth >= 64:
                        return {"ok": False, "broken_at": 0, "file": os.path.basename(path), "reason": "ROTATION_DEPTH"}
                    arch_name = (e.get("extra") or {}).get("archive") or ""
                    arch = os.path.join(os.path.dirname(os.path.abspath(path)), arch_name)
                    if not arch_name or not os.path.exists(arch):
                        return {"ok": False, "broken_at": 0, "file": os.path.basename(path), "reason": "ARCHIVE_MISSING"}
                    sub = self._verify_file(arch, depth + 1)
                    if not sub.get("ok"):
                        return sub
                    if sub["last_hash"] != e.get("prev_hash") or sub["last_seq"] != e.get("seq") - 1:
                        return {"ok": False, "broken_at": 0, "file": os.path.basename(path), "reason": "ANCHOR_MISMATCH"}
                    rotations = 1 + sub.get("rotations", 0)
                elif e.get("prev_hash") != GENESIS or e.get("seq") != 1:
                    return {"ok": False, "broken_at": 0, "file": os.path.basename(path), "reason": "GENESIS_MISMATCH"}
            else:
                if e.get("prev_hash") != prev_hash or e.get("seq") != prev_seq + 1:
                    return {"ok": False, "broken_at": i, "file": os.path.basename(path), "reason": "CHAIN_MISMATCH"}
            prev_hash, prev_seq = e["hash"], e["seq"]
        return {"ok": True, "entries": len(entries), "rotations": rotations,
                "last_hash": prev_hash, "last_seq": prev_seq}

    def verify(self) -> Dict[str, Any]:
        """Пересчёт цепочки (включая архивы ротаций): ok=False + причина/индекс при
        подмене, удалении, перестановке или отсутствующем архиве."""
        r = self._verify_file(self.path)
        if r.get("ok"):
            return {"ok": True, "entries": r["entries"], "rotations": r.get("rotations", 0),
                    "total_seq": r["last_seq"]}
        return r

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
