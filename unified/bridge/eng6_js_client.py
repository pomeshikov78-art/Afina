"""unified/bridge/eng6_js_client.py — клиент вычислительного контура ENG6-PLAN (линия B).

Назначение (шаг 5 плана совмещения): платформа «Афина» обращается к ядру ENG6-JS
через REST как к абстрагированному вычислительному бэкенду (ARC-002/ARC-003).

Принципы (наследуются от обеих линий):
  * stdlib-only (нулевые зависимости — режим Б / Air-Gap);
  * конформанс-гейт (К-04 Пути А): при conformance.ok != true расчёт помечается
    недостоверным, а не подменяется;
  * честная обработка 429/503: Retry-After соблюдается, ошибка не глотается;
  * X-Requestor обязателен (требование Военпреда: аудит каждого запуска).

Клиент НЕ хранит результаты (TTL на стороне сервиса) и НЕ логирует координаты.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

CLIENT_VERSION = "1.0.0"
DEFAULT_TIMEOUT_S = 120.0
MAX_RETRIES = 3


class Eng6ServiceError(RuntimeError):
    """Ошибка взаимодействия с сервисом ENG6-PLAN (код HTTP + машинный код тела)."""

    def __init__(self, message: str, http_status: Optional[int] = None,
                 code: Optional[str] = None, retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.retry_after_s = retry_after_s


class Eng6JsClient:
    """REST-клиент сервиса ENG6-PLAN (api/server.mjs, 11 маршрутов)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8080",
                 requestor: str = "afina-platform", timeout_s: float = DEFAULT_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.requestor = requestor
        self.timeout_s = timeout_s

    # ---------- низкоуровневый обмен ----------
    def _request(self, method: str, path: str, payload: Optional[Dict] = None,
                 headers: Optional[Dict[str, str]] = None,
                 timeout_s: Optional[float] = None) -> Dict[str, Any]:
        hdrs = {"User-Agent": f"afina-unified/{CLIENT_VERSION}",
                "X-Requestor": self.requestor,
                "Accept": "application/json"}
        hdrs.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=body,
                                     headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:2000]
            code, retry_after = None, None
            ra = e.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    retry_after = None
            try:
                parsed = json.loads(raw)
                code = (parsed.get("error") or {}).get("code") or parsed.get("code")
            except Exception:
                parsed = None
            raise Eng6ServiceError(
                f"HTTP {e.code} {method} {path}: {raw[:300]}",
                http_status=e.code, code=code, retry_after_s=retry_after) from None
        except urllib.error.URLError as e:
            raise Eng6ServiceError(f"сервис ENG6-PLAN недоступен ({self.base_url}): {e.reason}") from None

    def _request_retry(self, method: str, path: str, payload: Optional[Dict] = None,
                       headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Повторы с экспоненциальной паузой и учётом Retry-After (К-02 Пути А)."""
        last: Optional[Eng6ServiceError] = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._request(method, path, payload, headers)
            except Eng6ServiceError as e:
                last = e
                if e.http_status in (429, 503) and attempt < MAX_RETRIES - 1:
                    pause = e.retry_after_s if e.retry_after_s is not None else 0.5 * (2 ** attempt)
                    time.sleep(min(pause, 30.0))
                    continue
                raise
        raise last  # недостижимо

    # ---------- контракт и здоровье ----------
    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/healthz")

    def ready(self) -> bool:
        try:
            self._request("GET", "/readyz")
            return True
        except Eng6ServiceError:
            return False

    def meta(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/meta")

    def conformance(self, refresh: bool = False) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/conformance" + ("?refresh=1" if refresh else ""))

    def conformance_gate(self, refresh: bool = False) -> Dict[str, Any]:
        """К-04: проверка достоверности ядра перед расчётом.

        Возвращает протокол конформанса; бросает Eng6ServiceError, если ядро
        не прошло 33 эталонные проверки или 5 регрессионных сценариев —
        результат расчёта на таком ядре недостоверен и не выдаётся.
        """
        proto = self.conformance(refresh=refresh)
        ok = bool(proto.get("ok"))
        if not ok:
            raise Eng6ServiceError(
                "конформанс ENG6 не пройден — расчёт недостоверен (К-04): "
                + json.dumps({k: proto.get(k) for k in
                              ("selfTestFailed", "regressionFailed")}, ensure_ascii=False))
        return proto

    # ---------- расчёт ----------
    def plan_sync(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_retry("POST", "/api/v1/plan/sync", request)

    def plan_async(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_retry("POST", "/api/v1/plan/async", request)

    def task(self, task_id: str, summary: bool = False) -> Dict[str, Any]:
        hdrs = {"X-Result-Format": "summary"} if summary else None
        return self._request("GET", f"/api/v1/tasks/{task_id}", headers=hdrs)

    def wait_task(self, task_id: str, poll_s: float = 0.5,
                  timeout_s: float = 300.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            t = self.task(task_id)
            state = t.get("state") or t.get("status")
            if state in ("SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED"):
                return t
            if time.monotonic() > deadline:
                raise Eng6ServiceError(f"задача {task_id} не завершилась за {timeout_s} с (состояние {state})")
            time.sleep(poll_s)

    def plan_auto(self, request: Dict[str, Any], heavy_nodes: int = 500,
                  timeout_s: float = 300.0) -> Dict[str, Any]:
        """Режим auto (К-02): мелкие задачи — sync, крупные — async с ожиданием."""
        n_nodes = len((request.get("topology") or {}).get("nodes") or [])
        if n_nodes <= heavy_nodes:
            return self.plan_sync(request)
        accepted = self.plan_async(request)
        task_id = accepted.get("taskId")
        if not task_id:
            raise Eng6ServiceError("plan/async не вернул taskId: " + json.dumps(accepted, ensure_ascii=False)[:300])
        done = self.wait_task(task_id, timeout_s=timeout_s)
        if (done.get("state") or done.get("status")) != "SUCCEEDED":
            raise Eng6ServiceError(f"задача {task_id} завершилась в состоянии {done.get('state')}: "
                                   + json.dumps(done.get("error"), ensure_ascii=False)[:300])
        return done.get("result") or {}

    # ---------- аудит/метрики (наблюдаемость) ----------
    def audit(self, limit: int = 20) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/audit?limit={int(limit)}")
