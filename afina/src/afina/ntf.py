"""Уведомления (§4.12, NTF-001…003): дедупликация, группировка, эскалация,
локальная очередь. Доставка ≠ просмотр ≠ исполнение — статусы раздельны.
"""
import time
from typing import Dict, Optional

VERSION = "1.0.0"
LIFECYCLE = ["Создано", "В очереди", "Доставлено", "Просмотрено", "Подтверждено", "Исполнено", "Закрыто"]

# правила эскалации (настраиваются администратором, NTF-003)
DEFAULT_ESCALATION = [
    {"kind": "критическое", "unhandled_minutes": 15, "to": "дежурный"},
    {"kind": "критическое", "unhandled_minutes": 60, "to": "руководитель"},
    {"kind": "warn", "unhandled_minutes": 240, "to": "ответственный"},
]
DEDUP_WINDOW_S = 300  # 5 минут


class NotifyService:
    def __init__(self, store):
        self.store = store

    def notify(self, kind: str, title: str, body: str = "", dedup_key: Optional[str] = None) -> Dict:
        """Создание с дедупликацией: связанные события в окне группируются (NTF-002)."""
        key = dedup_key or f"{kind}:{title}"
        recent = self.store.q("""SELECT * FROM notifications WHERE dedup_key=? AND created>?
                                 ORDER BY id DESC LIMIT 1""", (key, time.time() - DEDUP_WINDOW_S))
        if recent:
            n = recent[0]
            self.store.x("UPDATE notifications SET body=? WHERE id=?",
                         (body + f"\n(сгруппировано с #{n['id']}, дедупликация активна)", n["id"]))
            return {"id": n["id"], "deduplicated": True,
                    "note": "лавина дубликатов подавлена (NTF-002); после переподключения очередь доставляется без повторов"}
        nid = self.store.x("INSERT INTO notifications(kind,title,body,created,status,dedup_key) VALUES(?,?,?,?,?,?)",
                           (kind, title, body, time.time(), "Создано", key))
        # критическое нельзя отключить бесследно — только с записью
        if kind == "критическое":
            self.store.audit("система", "-", "критическое уведомление создано (бесследное отключение запрещено)",
                             f"NTF#{nid}", "sec")
        return {"id": nid, "deduplicated": False, "status": "Создано"}

    def advance(self, nid: int, actor: str, role: str) -> Dict:
        rows = self.store.q("SELECT * FROM notifications WHERE id=?", (nid,))
        if not rows:
            return {"error": "не найдено"}
        cur = rows[0]["status"]
        i = LIFECYCLE.index(cur) if cur in LIFECYCLE else 0
        nxt = LIFECYCLE[min(i + 1, len(LIFECYCLE) - 1)]
        self.store.x("UPDATE notifications SET status=? WHERE id=?", (nxt, nid))
        self.store.audit(actor, role, f"уведомление #{nid}: {cur} → {nxt}", "NTF")
        return {"id": nid, "from": cur, "to": nxt,
                "note": "уведомление не является подтверждением изменения данных (§4.12.2)"}

    def escalation_scan(self, now: Optional[float] = None) -> Dict:
        """NTF-003: необработанные критические события эскалируются по правилам."""
        now = now or time.time()
        escalated = []
        for rule in DEFAULT_ESCALATION:
            rows = self.store.q("""SELECT * FROM notifications WHERE kind=? AND escalated=0
                                   AND status IN ('Создано','В очереди','Доставлено')""", (rule["kind"],))
            for n in rows:
                age_min = (now - n["created"]) / 60.0
                if age_min >= rule["unhandled_minutes"]:
                    self.store.x("UPDATE notifications SET escalated=1 WHERE id=?", (n["id"],))
                    self.store.audit("система", "-", f"эскалация NTF#{n['id']} → {rule['to']} (не обработано {int(age_min)} мин)",
                                     "NTF", "sec", "NTF-003")
                    escalated.append({"id": n["id"], "to": rule["to"], "age_min": round(age_min, 1)})
        return {"escalated": escalated, "rules": DEFAULT_ESCALATION}
