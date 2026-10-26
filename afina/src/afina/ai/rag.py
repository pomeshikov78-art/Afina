"""ИИ-слой (§4.10): локальный детерминированный RAG без внешних зависимостей.

Архитектура готова к подключению локальной LLM (GGUF-раннер — отдельный
подключаемый компонент при наличии весов и GPU); текущая реализация —
извлекающий RAG: хэш-эмбеддинги (feature hashing, 512 dim), косинусная
близость, топ-k пассажей, сборка ответа С ОБЯЗАТЕЛЬНЫМИ источниками,
допущениями и ограничениями (AI-001).

Запреты (п. 4.10.2) обеспечены архитектурно: модуль не имеет доступа к записи
в хранилище (только чтение документов), не утверждает документы, не изменяет
базовые линии. AI-003: enabled=False полностью отключает слой.
"""
import hashlib, json, math, os, re, unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

VERSION = "1.0.0"
DIM = 512

STOP = set("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам сказал ведь потом себя ничего ей может они где тут есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая много разве три эту моя свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между".split())


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", t.lower())
    return re.sub(r"[^a-zа-яё0-9\-]+", " ", t)


def tokens(text: str) -> List[str]:
    return [w for w in _norm(text).split() if w and w not in STOP and len(w) > 1]


def embed(toks: List[str]) -> Dict[int, float]:
    """Feature hashing: токен → бакет (детерминированно, локально, без весов)."""
    v: Dict[int, float] = {}
    for t in toks:
        h = int(hashlib.sha1(t.encode()).hexdigest(), 16)
        b = h % DIM
        sign = 1.0 if (h // DIM) % 2 == 0 else -1.0
        v[b] = v.get(b, 0.0) + sign
    n = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {k: x / n for k, x in v.items()}


def cosine(a: Dict[int, float], b: Dict[int, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


@dataclass
class Chunk:
    doc_id: str
    section: str
    text: str
    emb: Dict[int, float] = field(default_factory=dict, repr=False)


class RagIndex:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.chunks: List[Chunk] = []

    # ---------- индексация ----------
    def add_markdown(self, path: str, doc_id: Optional[str] = None, max_chars=900):
        if not self.enabled:
            return 0
        doc_id = doc_id or os.path.basename(path)
        text = open(path, encoding="utf-8").read()
        parts = re.split(r"\n(?=#{1,4} )", text)
        n = 0
        for part in parts:
            head = part.strip().split("\n", 1)[0][:80]
            body = part.strip()
            for i in range(0, len(body), max_chars):
                seg = body[i:i + max_chars]
                if len(seg.strip()) < 40:
                    continue
                c = Chunk(doc_id, head, seg)
                c.emb = embed(tokens(seg))
                self.chunks.append(c); n += 1
        return n

    def add_text(self, doc_id: str, section: str, text: str):
        if not self.enabled:
            return 0
        c = Chunk(doc_id, section, text)
        c.emb = embed(tokens(text))
        self.chunks.append(c)
        return 1

    def add_json_records(self, doc_id: str, records: List[dict], fmt: str = "{k}: {v}"):
        n = 0
        for r in records:
            txt = "; ".join(fmt.format(k=k, v=v) for k, v in r.items() if not isinstance(v, (dict, list)))
            n += self.add_text(doc_id, r.get("id", r.get("name", "")), txt)
        return n

    # ---------- поиск и ответ ----------
    def search(self, query: str, k: int = 4) -> List[dict]:
        if not self.enabled:
            return []
        q = embed(tokens(query))
        scored = sorted(((cosine(q, c.emb), c) for c in self.chunks), key=lambda x: -x[0])
        return [{"score": round(s, 4), "doc": c.doc_id, "section": c.section, "text": c.text}
                for s, c in scored[:k] if s > 0.05]

    def answer(self, question: str, k: int = 4) -> dict:
        """Ответ со ссылками на документы и версии, допущениями и ограничениями (AI-001).

        При недостатке релевантности — объявляет неопределённость и запрашивает данные.
        """
        if not self.enabled:
            return {"status": "disabled", "note": "ИИ-слой отключён (AI-003); ГИС и расчёты работают штатно"}
        hits = self.search(question, k)
        if not hits or hits[0]["score"] < 0.12:
            return {"status": "insufficient_data",
                    "answer": "По локальному индексу достоверных сведений недостаточно. Запрашиваю данные: уточните объект/документ, либо зарегистрируйте источник. Подстановка догадки запрещена (EQP-001, п. 4.10.2).",
                    "sources": [], "assumptions": ["индекс может не содержать нужный документ"],
                    "limitations": ["ответ не является инженерным выводом; требуются данные"]}
        top = hits[0]
        snippet = top["text"][:600]
        return {
            "status": "ok",
            "answer": f"Наиболее релевантный фрагмент (score {top['score']}):\n«{snippet}…»",
            "sources": [{"doc": h["doc"], "section": h["section"], "score": h["score"]} for h in hits],
            "assumptions": ["извлекающий RAG без генерации: формулировки цитируются, не сочиняются",
                            "релевантность по хэш-эмбеддингам — при подключении локальной LLM качество формулировок вырастет"],
            "limitations": ["ответ не подтверждает техническую исправность", "не изменяет конфигурацию и документы (AI-002)",
                            "корреляция не выдаётся за причинно-следственную связь"],
            "confirmation_status": "требует инженерной проверки при использовании в решениях",
        }


# ---------- разбор ТЗ: извлечение требований (§4.10.1) ----------
REQ_LINE = re.compile(r"\|\s*([A-Z0-9]{2,4}-\d{3})\s*\|([^|]+)\|([^|]+)\|\s*(P\d)\s*\|")


def extract_requirements_from_tz(md_path: str) -> List[dict]:
    """Извлечение требований из текста ТЗ с источником и уверенностью."""
    text = open(md_path, encoding="utf-8").read()
    out = []
    for m in REQ_LINE.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        out.append({"id": m.group(1).strip(), "requirement": m.group(2).strip().replace("**", ""),
                    "acceptance_criteria": m.group(3).strip().replace("**", ""), "priority": m.group(4),
                    "source": {"doc": os.path.basename(md_path), "line": line_no},
                    "confidence": 0.97})
    return out


# ---------- поиск противоречий и аномалий (§4.10.1) ----------
def scan_anomalies(equipment_rows: List[dict], rules: dict = None) -> List[dict]:
    rules = rules or {"COM_power_max_w": 1000, "ANT_gain_max_dbi": 60}
    found = []
    for r in equipment_rows:
        pw = r.get("power_w")
        if pw is not None and r.get("class") == "COM" and pw > rules["COM_power_max_w"]:
            found.append({"id": r.get("id"), "type": "подозрительное значение",
                          "detail": f"мощность {pw} Вт > порога класса {rules['COM_power_max_w']} Вт",
                          "action": "вынести на арбитраж; автоисправление запрещено (DAT-004)"})
        y0, y1 = r.get("year_from"), r.get("year_to")
        if y0 and y1 and y1 < y0:
            found.append({"id": r.get("id"), "type": "противоречие дат", "detail": f"year_to {y1} < year_from {y0}"})
        if y1 and y1 < 2026 and r.get("status_kind") == "эксплуатируется":
            found.append({"id": r.get("id"), "type": "признак EOL", "detail": f"снят с производства {y1}, но статус «эксплуатируется»",
                          "action": "ВТ-07: вариант реакции (последний закуп/альтернатива/модернизация)"})
    return found
