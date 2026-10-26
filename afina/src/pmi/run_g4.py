#!/usr/bin/env python3
"""Протокол G4 «Расширение»: прогон tests/test_g4.py с постатейной привязкой к
требованиям ТЗ (DGT DT-5, AI-001…003, ENG-006/OPT-01/LCC-01, ENG-005, MOD-006,
GIS-006, ВТ-01…08, §4.11).

Запуск: python3 pmi/run_g4.py  (из каталога src)
"""
import json, os, sys, time, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REQ_MAP = {
    # G4.1 сценарный модуль DT-5
    ("TestScenarioUnit", "test_01_scenario_marked_as_estimate"): ("DGT §4.6.1 DT-5", "сценарий маркирован «оценка»; базовая линия и телеметрия не изменяются (DGT-002)"),
    ("TestScenarioUnit", "test_02_insufficient_data_honest_refusal"): ("§4.6.4/§4.10.2", "недостаточно данных — честный отказ с запросом данных, сценарий не записывается"),
    ("TestScenarioUnit", "test_03_unknown_kind_rejected"): ("ENG §4.5.6", "незарегистрированный вид сценария отклоняется (тихая подмена запрещена)"),
    ("TestScenarioUnit", "test_04_dt5_requires_validated_scenario"): ("DGT-004 + §4.10.2", "DT-5 только после независимой проверки сценария; уровни не пропускаются «через голову»"),
    ("TestScenarioUnit", "test_05_degradation_uses_rel_kernel"): ("REL-01 §4.5.4", "доступность в сценарии считается ядром REL-01, а не «на глаз»"),
    # G4.1 объяснения ИИ
    ("TestExplainUnit", "test_01_explain_calc_structure"): ("AI-001 §4.10", "ответ содержит источники, версии, допущения, ограничения, статус подтверждения"),
    ("TestExplainUnit", "test_02_explain_without_passport_requests_data"): ("§4.10.2", "при недостатке данных ИИ запрашивает данные, а не подставляет догадку"),
    ("TestExplainUnit", "test_03_explain_issue_and_unknown"): ("§4.10.1", "объяснение ошибок топологии/совместимости; неизвестный тип — честный отказ"),
    ("TestExplainUnit", "test_04_suggest_scenarios_data_driven"): ("§4.10.1", "предложение допустимых сценариев по фактической наполненности данных"),
    ("TestExplainUnit", "test_05_policy_prohibitions_present"): ("AI-002 §4.10.2", "семь запретов ИИ присутствуют в политике каждого ответа"),
    # G4.2 оптимизация
    ("TestOptUnit", "test_01_hard_constraint_blocks_best"): ("ENG-006 §4.5.4", "нарушитель жёсткого ограничения не может быть «лучшим»; причины отклонения возвращаются"),
    ("TestOptUnit", "test_02_pareto_and_weights_separated"): ("OPT-01 §4.5.4", "Парето-фронт и взвешенное ранжирование — раздельные результаты"),
    ("TestOptUnit", "test_03_unknown_constraint_type_rejected"): ("ENG-006", "неподдержанная спецификация ограничения отклоняется, а не игнорируется"),
    ("TestOptUnit", "test_04_lcc_math"): ("LCC-01 §4.5.4", "TCO = CAPEX + PV(OPEX) − PV(утилизация); чувствительность ±20 %"),
    # G4.3 суррогаты и 3D
    ("TestSurrogateUnit", "test_01_train_predict_domain"): ("MOD-006 §4.5.8", "обучение на решателе, RMSE, область применения, детекция выхода за диапазон, маркировка"),
    ("TestSurrogateUnit", "test_02_degenerate_set_refused"): ("MOD-006", "вырожденный обучающий набор — честный отказ (постоянный признак не идентифицируется)"),
    ("TestSurrogateUnit", "test_03_compare_and_disable"): ("MOD-006 §4.5.8", "сравнение с основным решателем на независимом наборе; возможность отключения"),
    ("TestSurrogateUnit", "test_04_gltf_normals_uvs_indices"): ("GIS-006", "glTF: NORMAL, TEXCOORD_0 и индексы (5121/5123/5125) прочитаны"),
    ("TestSurrogateUnit", "test_05_ifc_extrusion_geometry"): ("GIS-006", "IFC: IfcExtrudedAreaSolid → призма в абсолютных координатах через цепочку IfcLocalPlacement"),
    # G4.4 ВТ-профиль
    ("TestVTUnit", "test_01_stage_commission_rule"): ("ВТ-02 §4.9", "файлы ≠ этап; закрытие только решением комиссии при готовых материалах, с основанием"),
    ("TestVTUnit", "test_02_metrology_blocks_approval"): ("ВТ-03 §4.8.4/4.8.5", "просроченная поверка СИ → результаты недопустимы; паспорт испытания полного состава"),
    ("TestVTUnit", "test_03_readiness_no_skipping"): ("ВТ-05 §4.9", "4 уровня формальной готовности без пропуска; комиссия не подменяется"),
    ("TestVTUnit", "test_04_conditions_and_factors"): ("ВТ-06 §4.9", "10 факторов условий эксплуатации; сопоставление с проектом, оба значения сохраняются"),
    ("TestVTUnit", "test_05_aging_and_unification"): ("ВТ-07/ВТ-04 §4.9", "риски старения с вариантами реакции; дубли номенклатуры, заимствование, EOL-последствия"),
    ("TestVTUnit", "test_06_panel_sections"): ("ВТ-08 §4.9", "панель руководителя: 8 разделов из первичных реестров"),
    # G4.5 геймификация
    ("TestGameUnit", "test_01_award_only_for_verified_result"): ("§4.11.3", "баллы за проверяемый результат; защита от повторного начисления"),
    ("TestGameUnit", "test_02_forbidden_grounds"): ("§4.11.3", "6 запрещённых оснований отклоняются архитектурно"),
    ("TestGameUnit", "test_03_revoke_with_explanation"): ("§4.11.3", "отзыв ошибочных баллов с объяснением и журналированием корректировок"),
    ("TestGameUnit", "test_04_competency_levels"): ("§4.11.7", "уровни компетенции 0…5; уровень пользователя ≠ зрелость проекта"),
    ("TestGameUnit", "test_05_polygon_criteria_not_points"): ("§4.11.4", "полигон: 8 этапов, переход по инженерным критериям, не по очкам"),
    ("TestGameUnit", "test_06_readiness_indices_undefined"): ("§4.11.8", "индексы готовности ЦД разделены; не рассчитанный — «не определён»"),
    ("TestMissionsUnit", "test_01_all_missions_deterministic_and_checked"): ("§4.11.6", "9 миссий на изолированных синтетических данных; объяснение «почему верно»"),
    ("TestMissionsUnit", "test_02_hints_three_levels_no_answer_leak"): ("§4.11.6", "трёхуровневые подсказки не выдают ответ"),
    ("TestMissionsUnit", "test_03_unknown_mission"): ("§4.11.6", "неизвестная миссия — честный отказ"),
    # E2E
    ("TestG4E2E", "test_01_scenario_e2e_and_dt5"): ("DGT DT-5 + SEC-001", "E2E: сценарий через API, валидация только ролью verify (403 для прочих)"),
    ("TestG4E2E", "test_02_ai_explain_suggest_and_disable"): ("AI-001/003", "E2E: объяснение и предложение сценариев; отключение ИИ не ломает двойник/ГИС"),
    ("TestG4E2E", "test_03_opt01_lcc01_as_service"): ("OPT-01/LCC-01 + ARC-003", "E2E: оптимизация и TCO через очередь расчётов с паспортом"),
    ("TestG4E2E", "test_04_cosim01_e2e"): ("ENG-005 §4.5.6", "E2E: Co-Simulation — ID сценария, версии, единицы; ошибка решателя не подменяется"),
    ("TestG4E2E", "test_05_surrogate_service_e2e"): ("MOD-006 + SEC-001", "E2E: обучение/домен/сравнение/валидация (verify)/отключение (dict_edit)"),
    ("TestG4E2E", "test_06_vt_profile_e2e"): ("ВТ-01…08", "E2E: кооперация, этапы и комиссия, метрология, унификация, старение, готовность, условия, панель"),
    ("TestG4E2E", "test_07_game_and_missions_e2e"): ("§4.11", "E2E: начисление/дубль/отзыв по ролям, миссии с подсказками, полигон, индексы"),
    ("TestG4E2E", "test_08_analytics_and_demo_train"): ("ВТ-08 G4.6", "E2E: тренды из первичных реестров; demo-обучение суррогата на решателе RF-01"),
}


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_g4")
    results = []

    class Rec(unittest.TextTestResult):
        def addSuccess(self, t):
            super().addSuccess(t); results.append((t, "PASS", None))
        def addFailure(self, t, err):
            super().addFailure(t, err); results.append((t, "FAIL", self._exc_info_to_string(err, t)))
        def addError(self, t, err):
            super().addError(t, err); results.append((t, "ERROR", self._exc_info_to_string(err, t)))

    runner = unittest.TextTestRunner(resultclass=Rec, verbosity=0, stream=open(os.devnull, "w"))
    runner.run(suite)

    items = []
    for t, verdict, tb in results:
        cls = type(t).__name__; m = t._testMethodName
        req, desc = REQ_MAP.get((cls, m), ("-", m))
        items.append({"requirement": req, "description": desc, "test": f"{cls}.{m}", "verdict": verdict})
    n_pass = sum(1 for i in items if i["verdict"] == "PASS")
    proto = {
        "protocol": "ПРОТОКОЛ_G4", "title": "Рубеж G4 «Расширение»",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"), "tz_doc": "ТЗ.АФИНА.11.0",
        "scope": ["G4.1 сценарный модуль DT-5 + объяснения ИИ (AI-001…003)",
                  "G4.2 OPT-01/LCC-01 как сервис (ENG-006)",
                  "G4.3 Co-Simulation (ENG-005) + суррогаты (MOD-006) + glTF NORMAL/TEXCOORD, IFC-геометрия (GIS-006)",
                  "G4.4 военно-технический профиль ВТ-01…08 (§4.9, §4.8.4–4.8.5)",
                  "G4.5 геймификация, полигон, миссии (§4.11)",
                  "G4.6 аналитика и панель руководителя (ВТ-08)"],
        "tests_total": len(items), "tests_passed": n_pass,
        "verdict": "PASS" if n_pass == len(items) else "FAIL",
        "items": items,
        "honest_notes": [
            "ИИ-слой — детерминированный извлекающий RAG + шаблонные объяснения; локальная LLM (GGUF) остаётся ОПЦИЕЙ подключения при поставке весов Заказчиком (веса в комплект не входят, внешние API запрещены AUT-002)",
            "Суррогат — полиномиальная модель степени ≤2 (МНК); гауссовские процессы/RBF/нейросети — расширяемый реестр на той же обвязке (обучение/домен/сравнение/отключение реализованы)",
            "Полный 3D Ray-Tracing/SBR и UTD — целевой порт ядра C++/GPU (G5+); в G4 геометрическая трассировка LOS/Френель/дифракция из G1 сохранена и используется",
            "ВТ-01: передача комплектов данных фиксируется реестровой записью с sha256; физический перенос — регламент режима Б (AUT-005), сетевого моста нет",
            "Критерий 3-го этапа полигона («3+ объекта») упрощён относительно полного графа связности; графовый критерий применяется в CFG-анализе влияния",
            "Пороги уровней компетенции и правила баллов — данные (настраиваются независимо от ядра, §4.11.2)",
        ],
    }
    out = os.path.join(os.path.dirname(__file__), "..", "..", "protocols",
                       f"ПРОТОКОЛ_G4_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(proto, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{proto['verdict']}: {n_pass}/{len(items)} → {os.path.normpath(out)}")
    return 0 if proto["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
