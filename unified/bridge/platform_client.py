"""unified/bridge/platform_client.py — клиент сервера платформы «Афина» (линия A).

stdlib-only (urllib); Bearer-токен сессии; маршруты /api/v1/*.
Назначение в единой программе:
  * bootstrap/вход администратора (первый пользователь — bootstrap, r_register);
  * чтение журнала аудита и ПРОВЕРКА мастер-хэш-цепочки (/audit/verify);
  * создание/чтение объектов обстановки (GEO_OBJECT-слой, таблица objects);
  * используется master_chain_sync (сшивка трёх журналов) и scenario_export
    (экспорт обстановки платформы в контракт ENG6-PLAN).

Коды ошибок не глотаются: PlatformError с http_status и телом.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

VERSION = "1.0.0"
API = "/api/v1"


class PlatformError(RuntimeError):
    def __init__(self, message: str, http_status: Optional[int] = None):
        super().__init__(message)
        self.http_status = http_status


class PlatformClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8470", timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.token: Optional[str] = None
        self.identity: Dict[str, Any] = {}

    # ---------- низкоуровневый обмен ----------
    def _req(self, method: str, path: str, payload: Optional[Dict] = None,
             auth: bool = True) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": f"afina-unified/{VERSION}"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base_url + API + path, data=body,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:500]
            raise PlatformError(f"HTTP {e.code} {method} {path}: {raw}", http_status=e.code) from None
        except urllib.error.URLError as e:
            raise PlatformError(f"платформа недоступна ({self.base_url}): {e.reason}") from None

    # ---------- доступ ----------
    def health(self) -> Dict[str, Any]:
        req = urllib.request.Request(self.base_url + "/healthz",
                                     headers={"User-Agent": f"afina-unified/{VERSION}"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def register(self, login: str, password: str, role: str = "Администратор системы",
                 display: Optional[str] = None) -> Dict[str, Any]:
        """Регистрация пользователя. Первый пользователь — bootstrap-администратор
        (без токена); последующие — только с токеном «Администратора системы»."""
        return self._req("POST", "/register",
                         {"login": login, "password": password, "role": role,
                          "display": display or login})

    def login(self, login: str, password: str) -> Dict[str, Any]:
        r = self._req("POST", "/login", {"login": login, "password": password}, auth=False)
        self.token = r.get("token")
        self.identity = {k: r.get(k) for k in ("login", "role", "display")}
        if not self.token:
            raise PlatformError("login не вернул токен: " + json.dumps(r, ensure_ascii=False)[:200])
        return r

    def bootstrap_admin(self, login: str = "unified-admin", password: str = "",
                        role: str = "Администратор системы") -> Dict[str, Any]:
        """Bootstrap первого администратора (или вход, если пользователь существует)."""
        import secrets
        password = password or secrets.token_hex(16)
        self.password = password
        try:
            self.register(login, password, role)
        except PlatformError as e:
            if e.http_status not in (400, 403, 409):   # уже зарегистрирован — не ошибка
                raise
        return self.login(login, password)

    # ---------- аудит (мастер-цепочка линии A) ----------
    def audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        return (self._req("GET", f"/audit?limit={int(limit)}") or {}).get("audit", [])

    def audit_verify(self) -> Dict[str, Any]:
        return self._req("GET", "/audit/verify")

    # ---------- обстановка (объекты) ----------
    def create_object(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        return self._req("POST", "/objects", obj)

    def list_objects(self) -> List[Dict[str, Any]]:
        r = self._req("GET", "/objects")
        return r.get("objects", r if isinstance(r, list) else [])

    # ---------- очередь платформы ----------
    def queue(self) -> Dict[str, Any]:
        return self._req("GET", "/queue")
