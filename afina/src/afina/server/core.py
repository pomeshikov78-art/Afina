"""Серверное ядро: БД, аутентификация, RBAC, аудит с хэш-цепочкой (§3.3, §8.1).

Хранилище: SQLite (одноместовая поставка/режим Б) с той же логической схемой,
что и целевая PostgreSQL+PostGIS (src/db/schema_postgres.sql). Внешних
зависимостей нет (stdlib). Аутентификация: scrypt + HMAC-токены (интерфейс
СКЗИ-провайдера — crypto_provider: точка подключения сертифицированного
компонента определяется Заказчиком, §8.1).
"""
import hashlib, hmac, json, os, secrets, sqlite3, threading, time
from typing import Optional, Dict, List

SCHEMA_VERSION = "1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT UNIQUE,
  pass_hash TEXT, salt TEXT, role TEXT, display TEXT, created REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INT, issued REAL, expires REAL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, crs TEXT, created REAL,
  created_by TEXT, baseline TEXT);
CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, project TEXT, class TEXT, name TEXT,
  x REAL, y REAL, z REAL, attrs TEXT, ts_code TEXT DEFAULT 'TS-0', dat_status TEXT DEFAULT 'Не заполнено',
  source TEXT, recorded_at REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS tech_states(id INTEGER PRIMARY KEY AUTOINCREMENT, object_id TEXT,
  ts_code TEXT, basis TEXT, responsible TEXT, recorded_at REAL);
CREATE TABLE IF NOT EXISTS calcs(run_id TEXT PRIMARY KEY, function TEXT, model TEXT, mode TEXT,
  level TEXT, inputs_hash TEXT, payload TEXT, status TEXT, result_ref TEXT, passport TEXT,
  created REAL, finished REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT,
  body TEXT, created REAL, status TEXT DEFAULT 'Создано', dedup_key TEXT, escalated INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS zam(id TEXT PRIMARY KEY, title TEXT, crit TEXT, org TEXT, req TEXT,
  status TEXT, basis TEXT, own_user TEXT, created REAL);
CREATE TABLE IF NOT EXISTS locks(object_id TEXT PRIMARY KEY, user_login TEXT, session TEXT, since REAL);
CREATE TABLE IF NOT EXISTS telemetry(id INTEGER PRIMARY KEY AUTOINCREMENT, object_id TEXT, ts REAL,
  metric TEXT, value REAL, quality TEXT DEFAULT 'наблюдение');
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_login TEXT,
  role TEXT, action TEXT, object TEXT, class TEXT, result TEXT, prev_hash TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS equipment(id TEXT PRIMARY KEY, class TEXT, name TEXT, maker TEXT, country TEXT,
  year_from INT, year_to INT, bands TEXT, power_w REAL, sensitivity_dbm REAL, sens_state TEXT,
  status_kind TEXT, data_quality TEXT, source TEXT, attrs TEXT);
CREATE TABLE IF NOT EXISTS org_units(id TEXT PRIMARY KEY, parent TEXT, name TEXT, kind TEXT, oshc_version TEXT,
  staff INT, fact INT, flags TEXT, subordination TEXT);
CREATE TABLE IF NOT EXISTS baselines(id TEXT PRIMARY KEY, project TEXT, status TEXT, created REAL,
  created_by TEXT, content_hash TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY, object_id TEXT, field TEXT, vals TEXT,
  status TEXT, created REAL, resolution TEXT, resolved_by TEXT, basis TEXT);
CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY, title TEXT, reason TEXT, status TEXT, author TEXT,
  created REAL, affected TEXT, approver TEXT, version_new TEXT);
CREATE TABLE IF NOT EXISTS scenarios(id TEXT PRIMARY KEY, object_id TEXT, kind TEXT, params TEXT,
  base_state TEXT, result TEXT, marked TEXT DEFAULT 'оценка (не факт)', validated INTEGER DEFAULT 0,
  validation_basis TEXT, created REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS surrogates(id TEXT PRIMARY KEY, model_ref TEXT, kind TEXT, payload TEXT,
  status TEXT DEFAULT 'Разрабатывается', disabled INTEGER DEFAULT 0, compare_result TEXT,
  validated_by TEXT, validation_basis TEXT, created REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS vt_participants(id TEXT PRIMARY KEY, org TEXT, role TEXT, zone TEXT,
  contact TEXT, created REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS vt_transfers(id TEXT PRIMARY KEY, package TEXT, from_org TEXT, to_org TEXT,
  category TEXT, sha256 TEXT, items INT, created REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS vt_stages(id TEXT PRIMARY KEY, contract TEXT, title TEXT, due REAL,
  product INTEGER DEFAULT 0, docs INTEGER DEFAULT 0, tests INTEGER DEFAULT 0, kit INTEGER DEFAULT 0,
  evidence TEXT, commission TEXT, commission_by TEXT, commission_basis TEXT, created REAL);
CREATE TABLE IF NOT EXISTS vt_benches(id TEXT PRIMARY KEY, name TEXT, kind TEXT, passport TEXT,
  calib_due REAL, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS vt_tests(id TEXT PRIMARY KEY, object_id TEXT, bench_id TEXT, basis TEXT,
  config_ref TEXT, method_ref TEXT, software TEXT, conditions TEXT, raw TEXT, results TEXT,
  deviations TEXT, conclusion TEXT, approval TEXT DEFAULT 'Не согласован', created REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS vt_conditions(id TEXT PRIMARY KEY, object_id TEXT, factor TEXT,
  project_value REAL, actual_value REAL, unit TEXT, source TEXT, created REAL);
CREATE TABLE IF NOT EXISTS game_awards(id INTEGER PRIMARY KEY AUTOINCREMENT, user_login TEXT,
  event TEXT, points INT, dedup_key TEXT UNIQUE, ref TEXT, basis TEXT, created REAL,
  revoked INTEGER DEFAULT 0, revoke_reason TEXT, revoked_by TEXT);
"""

# RBAC-матрица (Приложение Н, G0.4)
ROLE_OPS = {
 "Проектировщик": {"read", "edit_models", "run_calc", "import", "export_limited", "tasks"},
 "Инженер-расчётчик": {"read", "run_calc", "edit_models_cond", "export_limited", "import", "tasks"},
 "Инженер-верификатор": {"read", "run_calc_cond", "verify", "audit_read", "export_limited"},
 "ГИС-аналитик": {"read", "import", "gis_edit", "export_limited"},
 "Администратор данных": {"read", "dict_edit", "migrate", "import", "audit_read"},
 "Руководитель программы": {"read", "assign", "audit_read", "export_limited"},
 "Уполномоченный утверждающий": {"read", "approve", "baseline_fix", "zam_approve", "audit_read", "export_limited"},
 "Аудитор": {"read", "audit_read"},
 "Администратор системы": {"read", "deploy", "audit_read", "audit_rotate"},
}


class CryptoProvider:
    """Интерфейс криптопровайдера (§8.1). По умолчанию — stdlib (scrypt/HMAC-SHA256).

    Точка подключения сертифицированного СКЗИ (КриптоПро/ViPNet по классу системы):
    заменить методы hash_password/verify_password/sign/verify на вызовы СКЗИ.
    Наличие сертифицированного компонента НЕ означает сертификации всей Системы.
    """
    name = "stdlib-dev"

    @staticmethod
    def hash_password(pw: str, salt: bytes) -> str:
        return hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()

    @staticmethod
    def sign(data: bytes, key: bytes) -> str:
        return hmac.new(key, data, hashlib.sha256).hexdigest()

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema',?)", (SCHEMA_VERSION,))
        if not self._meta("secret"):
            self._set_meta("secret", secrets.token_hex(32))
        self.db.commit()

    def _meta(self, k):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def _set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))

    def q(self, sql, args=()) -> List[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # ---------- пользователи / сессии ----------
    def register_user(self, login, password, role, display=None):
        if role not in ROLE_OPS:
            raise ValueError(f"неизвестная роль «{role}» (матрица Приложения Н)")
        salt = secrets.token_bytes(16)
        ph = CryptoProvider.hash_password(password, salt)
        self.x("INSERT INTO users(login,pass_hash,salt,role,display,created) VALUES(?,?,?,?,?,?)",
               (login, ph, salt.hex(), role, display or login, time.time()))
        return {"login": login, "role": role}

    def login(self, login, password, ttl_s=8 * 3600):
        rows = self.q("SELECT * FROM users WHERE login=? AND active=1", (login,))
        if not rows:
            return None
        u = rows[0]
        if CryptoProvider.hash_password(password, bytes.fromhex(u["salt"])) != u["pass_hash"]:
            return None
        token = secrets.token_hex(24)
        sig = CryptoProvider.sign(token.encode(), bytes.fromhex(self._meta("secret")))
        full = f"{token}:{sig}"
        self.x("INSERT INTO sessions(token,user_id,issued,expires) VALUES(?,?,?,?)",
               (full, u["id"], time.time(), time.time() + ttl_s))
        return {"token": full, "login": u["login"], "role": u["role"], "display": u["display"]}

    def session(self, token: Optional[str]):
        if not token:
            return None
        rows = self.q("SELECT s.*, u.login, u.role, u.display FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?",
                      (token, time.time()))
        return dict(rows[0]) if rows else None

    # ---------- RBAC ----------
    @staticmethod
    def can(role: str, op: str) -> bool:
        ops = ROLE_OPS.get(role, set())
        if op in ops:
            return True
        if op.endswith("_cond"):
            return op[:-5] in ops or op in ops
        return False

    # ---------- аудит: защищённая хэш-цепочка (SEC-003) ----------
    def audit(self, user_login, role, action, obj="", cls="data", result="успешно"):
        with self._lock:
            last = self.db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else "GENESIS"
            ts = time.time()
            body = json.dumps([ts, user_login, role, action, obj, cls, result, prev], ensure_ascii=False)
            h = CryptoProvider.sha256(body.encode())
            self.db.execute("INSERT INTO audit(ts,user_login,role,action,object,class,result,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?)",
                            (ts, user_login, role, action, obj, cls, result, prev, h))
            self.db.commit()
            return h

    def audit_verify(self):
        rows = self.q("SELECT * FROM audit ORDER BY id")
        prev = "GENESIS"
        for r in rows:
            body = json.dumps([r["ts"], r["user_login"], r["role"], r["action"], r["object"],
                               r["class"], r["result"], r["prev_hash"]], ensure_ascii=False)
            if CryptoProvider.sha256(body.encode()) != r["hash"] or r["prev_hash"] != prev:
                return {"ok": False, "broken_at": r["id"]}
            prev = r["hash"]
        return {"ok": True, "entries": len(rows)}

    # ---------- экспорт: SEC-004 фильтр ----------
    EXPORT_DROP_FIELDS = {"pass_hash", "salt", "token", "secret"}
    EXPORT_RESTRICTED_ATTRS = {"категория_доступа_закрытая", "персональные_данные", "гриф"}

    def sanitize_for_export(self, row: dict, category: str = "ВНУТРЕННЯЯ") -> dict:
        out = {}
        for k, v in row.items():
            if k.lower() in self.EXPORT_DROP_FIELDS:
                continue
            if k == "attrs" and isinstance(v, str):
                try:
                    a = json.loads(v)
                    a = {ak: av for ak, av in a.items() if ak.lower() not in self.EXPORT_RESTRICTED_ATTRS}
                    if category == "ОТКРЫТАЯ":
                        a = {ak: av for ak, av in a.items() if not str(ak).startswith("_")}
                    v = a
                except Exception:
                    pass
            out[k] = v
        return out
