"""Геймификация, обучение и инженерный полигон (§4.11).

Принцип: игровой элемент выдаётся ЗА ПРОВЕРЯЕМЫЙ РЕЗУЛЬТАТ, а не за нажатие
кнопки. Начисление подтверждается сервером по первичным реестрам; запрещённые
основания отклоняются архитектурно; повторное начисление блокируется dedup-ключом;
ошибочные баллы отзываются с объяснением и журналированием. Визуальные награды
отключаются без потери инженерной функциональности (правила — данные).
"""
import hashlib, json, random, time
from typing import Dict, List, Optional

VERSION = "1.0.0"

# ---------- за что начисляются баллы (§4.11.3) ----------
POINTS = {
    "requirement_confirmed":   {"points": 10, "text": "подтверждённое выполнение требования (согласованное испытание)"},
    "conflict_resolved":       {"points": 5,  "text": "устранение конфликта совместимости/данных решением арбитража"},
    "data_quality_improved":   {"points": 3,  "text": "повышение качества данных объекта (статус «Введено»/«Проверено»)"},
    "topology_valid":          {"points": 4,  "text": "корректная топология: проверка правил без нарушений"},
    "redundancy_confirmed":    {"points": 6,  "text": "подтверждённое резервирование (расчёт REL/COSIM выполнен)"},
    "reproducible_run":        {"points": 5,  "text": "воспроизводимость: архив базовой линии с хэшем создан"},
    "assumption_documented":   {"points": 2,  "text": "документирование допущений в паспорте расчёта"},
    "zam_closed_verified":     {"points": 4,  "text": "закрытие замечания после повторной проверки (с основанием)"},
}

# ---------- за что начислять ЗАПРЕЩЕНО (§4.11.3) ----------
FORBIDDEN = [
    "количество объектов без качества",
    "запуск расчёта без анализа",
    "сокрытие дефектов",
    "обход согласования",
    "скорость ценой точности или безопасности",
    "неподтверждённые результаты",
]

# ---------- полигон: 8 этапов, переход по инженерным критериям (§4.11.4) ----------
POLYGON_STAGES = [
    ("Постановка задачи", "проект зарегистрирован и реестр требований загружен"),
    ("Разведка исходных данных", "есть объект с заполненными данными и источником"),
    ("Проектирование архитектуры", "не менее 3 связанных объектов (граф зависимостей непуст)"),
    ("Сборка цифрового двойника", "хотя бы один объект достиг DT-2 (расчёт подключён)"),
    ("Инженерное моделирование", "есть завершённый расчёт с паспортом"),
    ("Проверка сценариев", "выполнен хотя бы один сценарий двойника"),
    ("Сравнение вариантов", "OPT-01 выполнен с хотя бы одним допустимым вариантом"),
    ("Защита проекта", "утверждённая базовая линия есть; критические замечания закрыты"),
]

# ---------- уровни компетенции (§4.11.7) ----------
COMPETENCY = ["Исследователь", "Компоновщик", "Проектировщик", "Инженер-расчётчик",
              "Инженер-верификатор", "Руководитель проекта"]
COMPETENCY_THRESHOLDS = [0, 20, 60, 140, 260, 420]   # сумма подтверждённых баллов


class GameEngine:
    def __init__(self, store):
        self.store = store

    # ================= начисление: только проверяемый результат =================
    def verify_event(self, event: str, ref: str, body: Dict) -> Dict:
        """Серверная проверка основания начисления по первичным реестрам."""
        q = self.store.q
        if event == "requirement_confirmed":
            rows = q("SELECT COUNT(*) c FROM vt_tests WHERE approval LIKE 'Согласовано%' AND basis LIKE ?", (f"%{ref}%",))
            ok = bool(rows and rows[0]["c"] > 0)
            why = f"согласованное испытание со ссылкой на {ref}" if ok else "нет СОГЛАСОВАННОГО испытания, подтверждающего требование"
        elif event == "conflict_resolved":
            rows = q("SELECT * FROM conflicts WHERE id=?", (ref,))
            ok = bool(rows and rows[0]["status"] == "Разрешён" and rows[0]["basis"])
            why = "конфликт разрешён арбитражем с основанием" if ok else "конфликт не разрешён или нет основания (DAT-004)"
        elif event == "data_quality_improved":
            rows = q("SELECT dat_status, source FROM objects WHERE id=?", (ref,))
            ok = bool(rows and rows[0]["dat_status"] in ("Введено", "Проверено") and rows[0]["source"])
            why = f"объект в статусе «{rows[0]['dat_status']}» с источником" if ok else "статус данных не повышен или нет источника"
        elif event == "topology_valid":
            from .cfg import eval_rules
            res = eval_rules(body.get("ctx", {}))
            ok = res.get("overall") == "ok" and not res.get("violations")
            why = "проверка инженерных правил пройдена без нарушений" if ok else f"правила не пройдены: {res.get('violations')}"
        elif event == "redundancy_confirmed":
            rows = q("SELECT function, status FROM calcs WHERE run_id=?", (ref,))
            ok = bool(rows and rows[0]["status"] == "done" and rows[0]["function"] in ("REL-01", "COSIM-01"))
            why = "расчёт надёжности/сопряжения завершён" if ok else "нет завершённого расчёта REL-01/COSIM-01 с таким run_id"
        elif event == "reproducible_run":
            rows = q("SELECT COUNT(*) c FROM audit WHERE action LIKE 'архив воспроизводимости%' AND object=?", (ref,))
            ok = bool(rows and rows[0]["c"] > 0)
            why = "архив воспроизводимости зафиксирован в аудите" if ok else "архив воспроизводимости для базовой линии не создан"
        elif event == "assumption_documented":
            rows = q("SELECT passport FROM calcs WHERE run_id=?", (ref,))
            ok = False
            if rows and rows[0]["passport"]:
                try:
                    pj = json.loads(rows[0]["passport"])
                    ok = bool(pj.get("assumptions") or pj.get("applicability"))
                except Exception:
                    ok = False
            why = "паспорт содержит задокументированные допущения/применимость" if ok else "в паспорте нет допущений — балл не выдаётся"
        elif event == "zam_closed_verified":
            rows = q("SELECT status, basis FROM zam WHERE id=?", (ref,))
            ok = bool(rows and rows[0]["status"] == "Закрыто" and rows[0]["basis"])
            why = "замечание закрыто с основанием после проверки" if ok else "замечание не закрыто или закрыто без основания (§4.8.6)"
        else:
            return {"ok": False, "why": f"событие «{event}» не зарегистрировано в правилах; "
                                        f"запрещённые основания: {FORBIDDEN}"}
        return {"ok": ok, "why": why}

    def award(self, user_login: str, event: str, ref: str = "", body: Optional[Dict] = None) -> Dict:
        if event not in POINTS:
            return {"status": "rejected", "reason": f"«{event}» не является проверяемым основанием (§4.11.3)",
                    "forbidden_grounds": FORBIDDEN}
        v = self.verify_event(event, ref, body or {})
        if not v["ok"]:
            self.store.audit(user_login, "-", f"ОТКАЗ начисления баллов: {event} ({ref})", ref, "data", v["why"])
            return {"status": "rejected", "reason": v["why"], "policy": "баллы только за проверяемый результат"}
        key_ref = ref or (json.dumps(body, sort_keys=True, ensure_ascii=False)[:60] if body else user_login)
        dedup = f"{event}:{key_ref}"
        dup = self.store.q("SELECT id FROM game_awards WHERE dedup_key=? AND revoked=0", (dedup,))
        if dup:
            return {"status": "duplicate", "reason": f"повторное начисление заблокировано (award #{dup[0]['id']})",
                    "policy": "защита от повторного начисления (§4.11.3)"}
        self.store.x("INSERT INTO game_awards(user_login,event,points,dedup_key,ref,basis,created) VALUES(?,?,?,?,?,?,?)",
                     (user_login, event, POINTS[event]["points"], dedup, ref, v["why"], time.time()))
        return {"status": "awarded", "event": event, "points": POINTS[event]["points"],
                "basis": v["why"], "dedup_key": dedup}

    def revoke(self, award_id: int, by: str, reason: str) -> Dict:
        if not reason.strip():
            return {"status": "error", "error": "отзыв требует объяснения (§4.11.3)"}
        rows = self.store.q("SELECT * FROM game_awards WHERE id=?", (award_id,))
        if not rows:
            return {"status": "error", "error": "начисление не найдено"}
        if rows[0]["revoked"]:
            return {"status": "error", "error": "уже отозвано"}
        self.store.x("UPDATE game_awards SET revoked=1, revoke_reason=?, revoked_by=? WHERE id=?",
                     (reason, by, award_id))
        self.store.audit(by, "-", f"отзыв баллов award #{award_id} ({rows[0]['event']})", str(award_id), "data", reason)
        return {"status": "revoked", "id": award_id, "reason": reason,
                "note": "корректировка зажурналирована; итоговые суммы пересчитаны"}

    def scores(self) -> Dict:
        rows = self.store.q("""SELECT user_login, SUM(CASE WHEN revoked=0 THEN points ELSE 0 END) total,
                               COUNT(*) awards, SUM(revoked) revoked_n
                               FROM game_awards GROUP BY user_login ORDER BY total DESC""")
        for r in rows:
            r["competency"] = self.competency_of(r["total"] or 0)
        return {"leaderboard": rows, "points_table": POINTS, "forbidden_grounds": FORBIDDEN}

    @staticmethod
    def competency_of(total: int) -> str:
        lvl = 0
        for i, th in enumerate(COMPETENCY_THRESHOLDS):
            if total >= th:
                lvl = i
        return f"{lvl} {COMPETENCY[lvl]}"

    # ================= полигон: 8 этапов (§4.11.4) =================
    def polygon(self, project: str = "P-1") -> Dict:
        q = self.store.q
        def n(sql, args=()):
            r = q(sql, args)
            return r[0]["c"] if r else 0
        crit = [
            n("SELECT COUNT(*) c FROM projects WHERE id=?", (project,)) > 0 or n("SELECT COUNT(*) c FROM objects WHERE project=?", (project,)) > 0,
            n("SELECT COUNT(*) c FROM objects WHERE project=? AND dat_status!='Не заполнено' AND source IS NOT NULL AND source!=''", (project,)) > 0,
            n("SELECT COUNT(*) c FROM objects WHERE project=?", (project,)) >= 3,
            self._any_dt_level(project) >= 2,
            n("SELECT COUNT(*) c FROM calcs WHERE status='done' AND passport IS NOT NULL AND passport!='{}'") > 0,
            n("SELECT COUNT(*) c FROM scenarios") > 0,
            n("SELECT COUNT(*) c FROM calcs WHERE function='OPT-01' AND status='done'") > 0,
            n("SELECT COUNT(*) c FROM baselines WHERE project=? AND status='утверждена'", (project,)) > 0
            and n("SELECT COUNT(*) c FROM zam WHERE crit='Критический' AND status!='Закрыто'") == 0,
        ]
        stage = 0
        for ok in crit:
            if ok:
                stage += 1
            else:
                break
        return {"project": project, "stages": [{"n": i + 1, "name": POLYGON_STAGES[i][0],
                                                "criterion": POLYGON_STAGES[i][1], "met": crit[i]}
                                               for i in range(8)],
                "current_stage": stage + 1 if stage < 8 else "завершён",
                "rule": "переход определяется ИНЖЕНЕРНЫМИ критериями, а не количеством очков (§4.11.4)"}

    def _any_dt_level(self, project: str) -> int:
        from .twin import TwinService
        tw = TwinService(self.store)
        best = 0
        for o in self.store.q("SELECT id FROM objects WHERE project=?", (project,)):
            lev = tw.dt_level(o["id"]).get("level", "DT-0")
            try:
                best = max(best, int(str(lev).replace("DT-", "")))
            except ValueError:
                pass
        return best

    # ================= индексы готовности ЦД (§4.11.8) =================
    def readiness_indices(self, object_id: str) -> Dict:
        rows = self.store.q("SELECT * FROM objects WHERE id=?", (object_id,))
        if not rows:
            return {"status": "error", "error": "объект не найден"}
        o = rows[0]
        fields = {"x": o["x"], "y": o["y"], "class": o["class"], "name": o["name"],
                  "ts_code": o["ts_code"] if o["ts_code"] != "TS-0" else None,
                  "attrs": o["attrs"] if o["attrs"] not in (None, "{}") else None}
        filled = sum(1 for v in fields.values() if v is not None)
        completeness = round(100.0 * filled / len(fields), 1)
        reliability = 100.0 if o["source"] else None            # «не определён», а не 0 %
        from .twin import TwinService
        lev = TwinService(self.store).dt_level(object_id).get("level", "DT-0")
        maturity = round(100.0 * int(str(lev).replace("DT-", "")) / 5, 1)
        conf = self.store.q("SELECT COUNT(*) c FROM conflicts WHERE object_id=? AND status='Открыт'", (object_id,))
        compatibility = 100.0 if conf and conf[0]["c"] == 0 else (0.0 if conf else None)
        tele = self.store.q("SELECT COUNT(*) c FROM telemetry WHERE object_id=?", (object_id,))
        operational = None if not tele or not tele[0]["c"] else \
            round(100.0 * (1 if o["ts_code"] in ("TS-1", "TS-2") else 0.5), 1)
        return {"object_id": object_id,
                "полнота_данных": completeness, "достоверность": reliability,
                "зрелость_модели": maturity, "совместимость": compatibility,
                "эксплуатационная_готовность": operational,
                "rule": "не рассчитанный показатель — «не определён» (None), а не 0 % или 100 % (§4.11.8); "
                        "индексы разделены и не сворачиваются в один процент"}


# ================= учебные миссии (§4.11.6): изолированные синтетические данные =================
class Missions:
    """Каждая миссия: generate(seed) → задание; check(seed, answer) → верность +
    ПОЧЕМУ результат верен; hint(seed, level 1..3) — трёхуровневые подсказки.
    Данные синтетические, изолированные; производственные реестры не затрагиваются."""

    IDS = ["собери_узел", "найди_ошибку_в_паспорте", "проверь_модель", "безопасное_изменение",
           "восстанови_состояние", "восстанови_ошс", "проведи_инвентаризацию",
           "диагностируй_состояние", "подготовь_то"]

    @staticmethod
    def _rng(seed):
        return random.Random(f"afina-mission-{seed}")

    # ---------- 1. Собери узел ----------
    def _m1(self, seed):
        r = self._rng(seed)
        load_w = r.choice([300, 500, 800])
        need_h = r.choice([4, 6, 8])
        opts = []
        for i, cap in enumerate(sorted(r.sample(range(1000, 9000, 500), 3))):
            opts.append({"id": f"БП-{i+1}", "capacity_wh": cap, "cost_rub": cap * r.choice([8, 10, 12])})
        ok = [o for o in opts if o["capacity_wh"] >= load_w * need_h]
        best = min(ok, key=lambda o: o["cost_rub"]) if ok else None
        return {"task": f"Узел: нагрузка {load_w} Вт, требуемая автономность {need_h} ч. "
                        "Выберите источник питания минимальной стоимости, обеспечивающий автономность.",
                "options": opts, "answer": best["id"] if best else None,
                "why": (f"требуемая ёмкость = {load_w} Вт × {need_h} ч = {load_w*need_h} Вт·ч; "
                        f"«{best['id']}» ({best['capacity_wh']} Вт·ч) — минимальная стоимость среди подходящих"
                        if best else "ни один вариант не обеспечивает автономность — честный отказ")}, opts

    # ---------- 2. Найди ошибку в паспорте ----------
    def _m2(self, seed):
        r = self._rng(seed)
        errs = [
            ("frequency_mhz", -900, "частота не может быть отрицательной"),
            ("antenna_gain_dbi", 95, "усиление 95 дБи недостижимо для антенны данного класса (физический предел ~50 дБи)"),
            ("valid_from", "2030-01-01", "дата выдачи паспорта позже начала срока действия — противоречие"),
            ("power_w", "20 Вт", "единицы измерения не в СИ-реестре ENG-004: ожидается число в Вт"),
        ]
        field, bad, why = errs[r.randrange(len(errs))]
        passport = {"id": "ПС-СИНТ-1", "object": "Узел связи (синтетический)", "frequency_mhz": 900,
                    "antenna_gain_dbi": 17, "power_w": 40, "valid_from": "2024-01-01", "issued": "2024-01-01"}
        passport[field] = bad
        if field == "valid_from":
            passport["issued"] = "2024-06-01"
        return {"task": "В паспорте одна ошибка. Укажите поле.", "passport": passport,
                "answer": field, "why": why}, passport

    # ---------- 3. Проверь модель ----------
    def _m3(self, seed):
        r = self._rng(seed)
        domain = {"f_mhz": [150, 1500], "d_km": [1, 20], "h_bs_m": [30, 200], "h_ms_m": [1, 10]}
        cases = []
        for i in range(5):
            c = {"f_mhz": r.choice([200, 900, 1800, 450]), "d_km": r.choice([2, 8, 25, 15]),
                 "h_bs_m": r.choice([40, 90, 250, 120]), "h_ms_m": r.choice([1.5, 5, 12, 3])}
            c["id"] = i + 1
            cases.append(c)
        bad = [c["id"] for c in cases if any(not (lo <= c[k] <= hi) for k, (lo, hi) in domain.items())]
        return {"task": "Модель Окамура–Хата. Домен: " + json.dumps(domain, ensure_ascii=False) +
                        ". Перечислите ID наборов ВНЕ домена (через запятую).",
                "cases": cases, "domain": domain, "answer": sorted(bad),
                "why": "MOD-003: вне домена результат не выдаётся как достоверный; " +
                       ", ".join(f"набор {b}" for b in sorted(bad))}, cases

    # ---------- 4. Безопасное изменение ----------
    def _m4(self, seed):
        r = self._rng(seed)
        correct = ["фиксация базовой линии", "анализ влияния", "внесение изменения", "проверка (верификатор)", "утверждение"]
        wrong_variants = [
            ["внесение изменения", "фиксация базовой линии", "анализ влияния", "утверждение", "проверка (верификатор)"],
            ["анализ влияния", "внесение изменения", "фиксация базовой линии", "утверждение", "проверка (верификатор)"],
            ["фиксация базовой линии", "внесение изменения", "анализ влияния", "утверждение", "проверка (верификатор)"],
        ]
        opts = [correct] + wrong_variants[:2]
        r.shuffle(opts)
        labels = "ABC"
        return {"task": "Выберите правильный порядок безопасного изменения (буква варианта).",
                "options": {labels[i]: o for i, o in enumerate(opts)},
                "answer": labels[opts.index(correct)],
                "why": "§4.8.2–4.8.3: базовая линия фиксируется ДО изменения, анализ влияния — до внесения, "
                       "утверждает не автор (Maker–Checker)"}, opts

    # ---------- 5. Восстанови состояние ----------
    def _m5(self, seed):
        r = self._rng(seed)
        bls = [{"id": f"BL-{i}", "created": 1700000000 + i * 86400,
                "sha256": hashlib.sha256(f"bl-{seed}-{i}".encode()).hexdigest()[:16]} for i in range(1, 4)]
        incident_at = bls[1]["created"] + 3600   # инцидент после BL-2, до BL-3
        return {"task": f"Инцидент в {incident_at}. Какое состояние восстанавливать (последняя линия ДО инцидента)?",
                "baselines": bls, "incident_at": incident_at, "answer": "BL-2",
                "why": "восстанавливается последняя базовая линия, зафиксированная ДО момента инцидента; "
                       "более поздняя линия содержит состояние уже после повреждения"}, bls

    # ---------- 6. Восстанови ОШС ----------
    def _m6(self, seed):
        r = self._rng(seed)
        tree = {"У-1": None, "У-2": "У-1", "У-3": "У-2", "У-4": "У-2", "У-5": "У-3"}
        broken = dict(tree)
        victim = r.choice(["У-3", "У-4", "У-5"])
        broken[victim] = r.choice([None, "У-5" if victim != "У-5" else "У-1"])
        return {"task": "ОШС повреждена: найдите узел с неверным подчинением и укажите правильный parent.",
                "tree": broken, "answer": {"node": victim, "parent": tree[victim]},
                "why": f"штатная структура: «{victim}» подчиняется «{tree[victim]}»; "
                       "цикл/потеря родителя нарушает управляемость (три уровня возможностей, §4.4.3)"}, broken

    # ---------- 7. Проведи инвентаризацию ----------
    def _m7(self, seed):
        r = self._rng(seed)
        names = ["Радиостанция Р-168", "Антенна АШ-2", "Блок питания БП-30", "Кабель К-25", "Шифратор З-1"]
        items = []
        for i in range(9):
            items.append({"inv": f"ИНВ-{100+i}", "name": r.choice(names)})
        uniq = len({i["name"] for i in items})
        dups = len(items) - uniq
        return {"task": "В списке позиции с одинаковыми наименованиями — дубли номенклатуры. "
                        "Сколько УНИКАЛЬНЫХ наименований?", "items": items, "answer": uniq,
                "why": f"дубли: {dups} записей повторяют наименования; уникальных {uniq} (ВТ-04: дубли выносятся на арбитраж, не удаляются молча)"}, items

    # ---------- 8. Диагностируй состояние ----------
    def _m8(self, seed):
        r = self._rng(seed)
        classifier = {"TS-1": "исправно", "TS-2": "работоспособно с замечаниями", "TS-3": "неисправно", "TS-0": "данные не заполнены"}
        scenario = r.choice([
            ("напряжение в норме, температура в норме, BER в норме", "TS-1"),
            ("напряжение в норме, температура выше нормы на 8 °C, BER в норме", "TS-2"),
            ("напряжение ниже минимума, передатчик не выходит на режим", "TS-3"),
        ])
        obs, ans = scenario
        return {"task": f"Наблюдения: {obs}. Классификатор: {json.dumps(classifier, ensure_ascii=False)}. Код состояния?",
                "answer": ans, "why": f"по классификатору §4.4.2 наблюдения соответствуют «{ans} — {classifier[ans]}»; "
                                      "статус качества данных не подменяет техническое состояние"}, obs

    # ---------- 9. Подготовь ТО ----------
    def _m9(self, seed):
        r = self._rng(seed)
        now = 1760000000 + r.randrange(0, 86400)
        units = []
        for i in range(3):
            interval = r.choice([30, 90, 180]) * 86400
            last = now - r.randrange(int(interval * 0.5), int(interval * 1.5))
            units.append({"id": f"УЗ-{i+1}", "last_service": last, "interval_days": interval // 86400})
        for u in units:
            u["next_due"] = u["last_service"] + u["interval_days"] * 86400
        due = sorted(units, key=lambda u: u["next_due"])
        return {"task": f"Текущая дата (epoch): {now}. Какое устройство требует ТО ПЕРВЫМ (ID)?",
                "units": units, "now": now, "answer": due[0]["id"],
                "why": f"ближайший срок: {due[0]['id']} — next_due {due[0]['next_due']} "
                       f"(last_service + интервал); просрочка: {[u['id'] for u in units if u['next_due'] < now]}"}, units

    _DISPATCH = None

    def _fn(self, mid: str):
        if Missions._DISPATCH is None:
            Missions._DISPATCH = {self.IDS[i]: getattr(self, f"_m{i+1}") for i in range(9)}
        return Missions._DISPATCH.get(mid)

    def start(self, mid: str, seed: int) -> Dict:
        fn = self._fn(mid)
        if not fn:
            return {"status": "error", "error": f"миссия «{mid}» не найдена; доступные: {self.IDS}"}
        task, _ = fn(seed)
        task.pop("answer", None); task.pop("why", None)
        return {"status": "ok", "mission": mid, "seed": seed, **task,
                "isolation": "синтетические данные; производственные реестры не изменяются (§4.11.6)"}

    def check(self, mid: str, seed: int, answer) -> Dict:
        fn = self._fn(mid)
        if not fn:
            return {"status": "error", "error": "миссия не найдена"}
        task, _ = fn(seed)
        correct = task["answer"]
        if isinstance(correct, list):
            try:
                got = sorted(int(x) for x in (re_split(answer) if not isinstance(answer, list) else answer))
            except (ValueError, TypeError):
                got = None
            ok = got == sorted(correct)
        elif isinstance(correct, dict):
            ok = isinstance(answer, dict) and answer.get("node") == correct["node"] and answer.get("parent") == correct["parent"]
        else:
            ok = str(answer).strip() == str(correct).strip()
        return {"status": "checked", "correct": ok, "why": task["why"],
                "policy": "объяснение ПОЧЕМУ результат верен — обязательная часть учебного режима (§4.11.6)",
                "isolation": "результат не влияет на производственные реестры"}

    def hint(self, mid: str, seed: int, level: int) -> Dict:
        fn = self._fn(mid)
        if not fn:
            return {"status": "error", "error": "миссия не найдена"}
        task, aux = fn(seed)
        level = max(1, min(3, int(level)))
        generic = {
            1: "Уровень 1: перечитайте условие — ответ вычисляется из приведённых данных, догадка не нужна.",
            2: "Уровень 2: примените инженерное правило из ТЗ (формула/порядок/классификатор), а не интуицию.",
            3: "Уровень 3: решите аналогичный мини-пример другими числами тем же методом, затем перенесите метод.",
        }
        specific = {
            ("собери_узел", 2): "Ёмкость = нагрузка × часы автономности; затем минимальная стоимость среди подходящих.",
            ("найди_ошибку_в_паспорте", 2): "Проверьте: физические пределы, знаки, единицы СИ, противоречия дат.",
            ("проверь_модель", 2): "Для каждого набора сверьте ВСЕ четыре параметра с границами домена.",
            ("безопасное_изменение", 2): "Ключевое: базовая линия — ДО изменения; утверждает не автор.",
            ("восстанови_состояние", 2): "Ищите максимальную created, которая МЕНЬШЕ момента инцидента.",
            ("восстанови_ошс", 2): "Признаки поломки: parent=None у некорневого узла или цикл (родитель — собственный потомок).",
            ("проведи_инвентаризацию", 2): "Уникальность — по наименованию, не по инвентарному номеру.",
            ("диагностируй_состояние", 2): "Сопоставьте наблюдения с формулировками классификатора TS-кодов.",
            ("подготовь_то", 2): "next_due = last_service + interval; первый — с минимальным next_due.",
        }
        text = specific.get((mid, level))
        return {"status": "ok", "mission": mid, "level": level, "hint": generic[level] + (" " + text if text and level >= 2 else ""),
                "note": "подсказка не выдаёт ответ (§4.11.6)"}


def re_split(answer):
    import re
    return [x for x in re.split(r"[,\s;]+", str(answer)) if x]
