"""Объяснения ИИ-слоя (§4.10.1, AI-001/002) — детерминированные, локальные.

Каждый ответ ОБЯЗАН содержать: источники, версии, допущения, ограничения,
статус подтверждения. При недостатке данных — запрос данных или объявление
неопределённости (подстановка догадки запрещена). Запреты §4.10.2 зашиты
в политику каждого ответа: объяснение не изменяет конфигурацию, не подтверждает
исправность, не валидирует модели, не закрывает замечания, не подменяет ядро.
"""
from typing import Dict, List, Optional

VERSION = "1.0.0"

# нормативные привязки расчётных функций (ТЗ §4.5.4) — источник для объяснений
TZ_REFS = {
    "RF-01": "ТЗ §4.5.4 RF-01 (потери распространения)",
    "RF-03": "ТЗ §4.5.4 RF-03 (радиопокрытие: карта, доля площади)",
    "RF-04": "ТЗ §4.5.4 RF-04 (SINR/C·I)",
    "RF-07": "ТЗ §4.5.4 RF-07 (бюджет радиолинии)",
    "PWR-01": "ТЗ §4.5.4 PWR-01 (баланс электропитания, автономность)",
    "THM-01": "ТЗ §4.5.4 THM-01 (тепловой режим)",
    "REL-01": "ТЗ §4.5.4 REL-01 (доступность, SPOF, FMEA)",
    "OPT-01": "ТЗ §4.5.4 OPT-01 (альтернативы, KPI, ограничения, Парето)",
    "LCC-01": "ТЗ §4.5.4 LCC-01 (TCO/LCC, чувствительность)",
    "COSIM-01": "ТЗ §4.5.6 (многофизическое сопряжение)",
}

AI_POLICY = [
    "объяснение не изменяет утверждённую конфигурацию (AI-002)",
    "не подтверждает техническую исправность",
    "не объявляет модель валидированной",
    "не закрывает замечания и не утверждает документы",
    "не подменяет расчётное ядро: числа берутся из паспорта запуска, не генерируются",
    "корреляция не выдаётся за причинно-следственную связь",
    "прогноз без независимой проверки не выдаётся за достоверный",
]


def _uncertainty_text(u: Optional[Dict]) -> List[str]:
    if not u:
        return ["неопределённость в паспорте не заполнена — объяснение выдаёт запрос данных"]
    names = {"data": "исходные данные", "numeric": "численная погрешность",
             "model_params": "параметры модели", "physical_model": "физическая модель"}
    return [f"{names.get(k, k)}: ±{v} дБ" for k, v in u.items() if isinstance(v, (int, float))]


def explain_calc(passport: Dict, rag=None, extra_sources: List[Dict] = None) -> Dict:
    """Объяснение расчётного запуска по его паспорту (§4.5.10).

    passport — dict паспорта (Passport.to_dict()); rag — опциональный RagIndex
    для цитирования ТЗ. Числа не пересчитываются — только цитируются (запрет
    подмены ядра)."""
    if not passport or not isinstance(passport, dict):
        return {"status": "insufficient_data",
                "answer": "Паспорт запуска не передан. Запрашиваю данные: run_id или содержимое паспорта.",
                "sources": [], "versions": {}, "assumptions": [], "limitations": AI_POLICY[:1],
                "confirmation_status": "не подтверждено"}
    fn = passport.get("function", "?")
    src = [{"doc": "ТЗ.АФИНА.11.0", "section": TZ_REFS.get(fn, f"ТЗ §4.5.4 ({fn})")}]
    if rag and getattr(rag, "enabled", False):
        for h in rag.search(f"{fn} {passport.get('model_id', '')}", k=2):
            src.append({"doc": h["doc"], "section": h["section"], "score": h["score"]})
    if extra_sources:
        src.extend(extra_sources)
    inputs = passport.get("inputs", {})
    unc = passport.get("uncertainty_db") or passport.get("uncertainty")
    lines = [
        f"Что посчитано: функция {fn}, модель «{passport.get('model_id', '?')}» версии {passport.get('model_version', '?')}, "
        f"режим {passport.get('mode', '?')}, уровень детализации {passport.get('level', '?')}.",
        f"Входы (хэш {str(passport.get('inputs_hash', '?'))[:12]}…): " +
        ", ".join(f"{k}={v}" for k, v in list(inputs.items())[:8]) if inputs else "Входы: см. паспорт.",
        "Неопределённость (4 компонента, §4.5.7): " + "; ".join(_uncertainty_text(unc)),
        f"Статус запуска: {passport.get('status', '?')}; CRS/единицы: {passport.get('crs', 'WGS84/СИ')}.",
    ]
    applic = passport.get("applicability") or {}
    if applic:
        lines.append("Оценка применимости: " + ", ".join(f"{k}={v}" for k, v in applic.items()))
    return {
        "status": "ok",
        "answer": "\n".join(lines),
        "sources": src,
        "versions": {"explain": VERSION, "model": passport.get("model_version", "?"),
                     "engine": passport.get("engine_version", "ENG6+ 1.0.0")},
        "assumptions": ["объяснение цитирует паспорт запуска и не пересчитывает числа",
                        "домен применимости модели взят из реестра (§4.5.9), а не подобран под ответ"],
        "limitations": AI_POLICY,
        "confirmation_status": "требует инженерной проверки при использовании в решениях",
    }


def explain_issue(kind: str, ctx: Optional[Dict] = None) -> Dict:
    """Объяснение ошибок топологии и совместимости (§4.10.1)."""
    ctx = ctx or {}
    catalog = {
        "no_power": ("Объект без электропитания",
                     "Узел не подключён к источнику питания — расчёт PWR-01 для него не выполняется.",
                     "Добавьте связь «источник→потребитель» или укажите автономность; затем повторите PWR-01.",
                     "ТЗ §4.5.4 PWR-01; правило топологии R-POWER (CFG-004)"),
        "loop": ("Цикл в топологии",
                 "Обнаружена циклическая зависимость: {detail}. Прямой прогон невозможен.",
                 "Разорвите цикл или задайте итерационную схему с контролем сходимости (§4.5.6).",
                 "ТЗ §4.5.6; cosim.detect_cycles"),
        "unit_mismatch": ("Несогласование единиц",
                          "Порт источника и приёмника имеют разные размерности: {detail}.",
                          "Приведите единицы через реестр ENG-004 (units.Q.to); молчаливая подстановка запрещена.",
                          "ТЗ §4.5.6; ENG-004"),
        "incompatible": ("Несовместимость средств",
                         "Средства несовместимы по declared-интерфейсам: {detail}.",
                         "Проверьте классификатор интерфейсов (ВТ-04) и карточки средств; при необходимости — CR-цикл изменения.",
                         "ТЗ §4.3.5 (Compatibility Engine)"),
        "domain_violation": ("Выход за область применимости модели",
                             "Параметры {detail} вне домена модели — результат не выдаётся как достоверный (MOD-003).",
                             "Выберите модель из домена или уточните входные данные; блокировка снята быть не может без изменения модели.",
                             "ТЗ §4.5.5, MOD-003"),
    }
    if kind not in catalog:
        return {"status": "insufficient_data",
                "answer": f"Тип проблемы «{kind}» не зарегистрирован в каталоге объяснений. Запрашиваю данные: уточните код ошибки.",
                "sources": [], "versions": {"explain": VERSION}, "assumptions": [],
                "limitations": AI_POLICY, "confirmation_status": "не подтверждено"}
    title, what, fix, ref = catalog[kind]
    detail = ctx.get("detail", "")
    return {
        "status": "ok",
        "answer": f"{title}. {what.format(detail=detail)} Рекомендация: {fix}",
        "sources": [{"doc": "ТЗ.АФИНА.11.0", "section": ref}],
        "versions": {"explain": VERSION},
        "assumptions": [f"контекст: {detail}" if detail else "контекст не передан — объяснение по типу ошибки"],
        "limitations": AI_POLICY,
        "confirmation_status": "рекомендация требует проверки инженером",
    }


def suggest_scenarios(object_row: Optional[Dict], observed: Dict, has_calc: bool = False) -> Dict:
    """Предложение допустимых сценариев и параметров (§4.10.1) — по фактической
    наполненности данных, а не «всем подряд». Честно сообщает, чего не хватает."""
    if not object_row:
        return {"status": "insufficient_data", "answer": "Объект не найден — сценарии не предлагаются.",
                "suggestions": [], "sources": [], "versions": {"explain": VERSION},
                "assumptions": [], "limitations": AI_POLICY, "confirmation_status": "не подтверждено"}
    obs = observed or {}
    sugg: List[Dict] = []
    metrics = sorted(obs.keys())
    if metrics:
        sugg.append({"kind": "param", "params": {"metric": metrics[0], "delta_pct": -10},
                     "why": f"what-if по наблюдаемой метрике «{metrics[0]}»: деградация на 10 %",
                     "data_needed": None})
        sugg.append({"kind": "trend", "params": {"metric": metrics[0], "horizon": 6},
                     "why": "прогноз тренда (требуется ≥3 наблюдений в истории)",
                     "data_needed": "история телеметрии ≥3 точек"})
    else:
        sugg.append({"kind": "trend", "params": None, "why": "прогноз невозможен: нет телеметрии",
                     "data_needed": "подключите телеметрию (DGT-002: только наблюдаемое состояние)"})
    sugg.append({"kind": "degradation", "params": {"mtbf_h": 8000, "mttr_h": 4, "factor": 0.5},
                 "why": "доступность при двукратной деградации MTBF (REL-01)",
                 "data_needed": None if has_calc else "паспорт изделия для фактических MTBF/MTTR"})
    return {
        "status": "ok",
        "answer": f"Для объекта {object_row.get('id')} предложено сценариев: {len(sugg)}. Все результаты будут маркированы «оценка».",
        "suggestions": sugg,
        "sources": [{"doc": "ТЗ.АФИНА.11.0", "section": "ТЗ §4.6.1 DT-5, §4.7 (сценарное моделирование)"}],
        "versions": {"explain": VERSION},
        "assumptions": ["предложения основаны на наличии данных, а не на догадках о назначении объекта"],
        "limitations": AI_POLICY,
        "confirmation_status": "сценарий выполняется движком двойника; ИИ лишь предлагает допустимые варианты",
    }
