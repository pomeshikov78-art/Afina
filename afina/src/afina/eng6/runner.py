"""Очередь расчётов (§4.5.1: ▶ синхронно / ⇶ асинхронно) — ARC-003.

Тяжёлые расчёты не блокируют интерфейс; очередь сохраняется при перезапуске
(perсистентность — JSON-файл). Восстановление/безопасный перезапуск при отказе (OPS-003).
"""
import json, os, threading, time, traceback, uuid
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, Optional

VERSION = "1.0.0"


@dataclass
class Job:
    job_id: str
    function: str
    payload: Dict
    status: str = "queued"      # queued|running|done|error
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    result_ref: Optional[str] = None
    passport_ref: Optional[str] = None


class CalcQueue:
    def __init__(self, persist_path: Optional[str] = None, workers: int = 1):
        self.jobs: Dict[str, Job] = {}
        self.order = []
        self.lock = threading.Lock()
        self.handlers: Dict[str, Callable] = {}
        self.persist_path = persist_path
        self.results_dir = os.path.join(os.path.dirname(persist_path or "."), "results") if persist_path else None
        self._stop = False
        self._thread = None
        if persist_path and os.path.exists(persist_path):
            self._load()
        if workers:
            self.start()

    def register(self, function: str, handler: Callable):
        self.handlers[function] = handler

    def submit(self, function: str, payload: Dict) -> str:
        jid = "JOB-" + uuid.uuid4().hex[:8].upper()
        job = Job(job_id=jid, function=function, payload=payload)
        with self.lock:
            self.jobs[jid] = job
            self.order.append(jid)
        self._save()
        return jid

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop:
            jid = None
            with self.lock:
                for j in self.order:
                    if self.jobs[j].status == "queued":
                        jid = j; break
            if not jid:
                time.sleep(0.05); continue
            job = self.jobs[jid]
            job.status = "running"; job.started_at = time.time()
            self._save()
            try:
                handler = self.handlers.get(job.function)
                if handler is None:
                    raise RuntimeError(f"обработчик «{job.function}» не зарегистрирован — запуск невозможен (тихая подмена запрещена)")
                res = handler(job.payload, progress_cb=lambda p: self._set_progress(jid, p))
                job.status = "done"; job.progress = 1.0
                job.result_ref = self._store_result(jid, res)
                if isinstance(res, dict) and "passport" in res:
                    job.passport_ref = res["passport"]
            except Exception as e:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                # задание сохранено: после перезапуска возможно безопасное повторное выполнение (OPS-003)
            finally:
                job.finished_at = time.time()
                self._save()

    def _set_progress(self, jid, p):
        self.jobs[jid].progress = p
        self._save()

    def _store_result(self, jid, res) -> str:
        if self.results_dir:
            os.makedirs(self.results_dir, exist_ok=True)
            path = os.path.join(self.results_dir, jid + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, default=str)
            return path
        return None

    def _save(self):
        if not self.persist_path:
            return
        os.makedirs(os.path.dirname(self.persist_path) or ".", exist_ok=True)
        tmp = self.persist_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"order": self.order, "jobs": {k: asdict(v) for k, v in self.jobs.items()}}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.persist_path)

    def _load(self):
        with open(self.persist_path, encoding="utf-8") as f:
            d = json.load(f)
        self.order = d.get("order", [])
        self.jobs = {k: Job(**v) for k, v in d.get("jobs", {}).items()}
        # OPS-003: задания, прерванные остановом, возвращаются в очередь для безопасного перезапуска
        for j in self.jobs.values():
            if j.status == "running":
                j.status = "queued"; j.progress = 0.0
                j.error = None

    def snapshot(self) -> Dict:
        with self.lock:
            return {"jobs": [asdict(self.jobs[j]) for j in self.order]}
