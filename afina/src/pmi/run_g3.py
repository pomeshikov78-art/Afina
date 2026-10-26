#!/usr/bin/env python3
"""Протокол G3 «Интеграция и жизненный цикл»: прогон tests/test_g3.py с
постатейной привязкой к требованиям ТЗ (DGT/CFG/NTF/DAT/GIS-006).

Запуск: python3 pmi/run_g3.py  (из каталога src)
"""
import json, os, subprocess, sys, time, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REQ_MAP = {
    ("TestTwin", "test_01_dt_level_by_criteria"): ("DGT-004", "уровень DT по проверяемым критериям, не «через голову»"),
    ("TestTwin", "test_02_observed_does_not_change_baseline"): ("DGT-002", "телеметрия не изменяет базовую конфигурацию"),
    ("TestTwin", "test_03_divergence_protocol"): ("DGT-003", "протокол расхождений проект↔факт, отсутствие наблюдений ≠ ноль"),
    ("TestTwin", "test_04_calibration_separation_and_refusal"): ("DGT-005", "идентифицированные параметры отдельно от паспорта; честный отказ при неинформативности"),
    ("TestTwin", "test_05_predict_marked_as_estimate"): ("DGT §4.6.3", "прогноз — оценка с горизонтом и неопределённостью"),
    ("TestCfg", "test_01_impact_graph_bfs"): ("CFG-002", "анализ влияния до утверждения (граф зависимостей)"),
    ("TestCfg", "test_02_rules_declarative"): ("CFG-004", "правила — данные с версией/обоснованием; пропуск поля ≠ соответствие"),
    ("TestCfg", "test_03_change_states"): ("CFG-003", "статусы изменения: Предложено/Внесено/Проверено/Утверждено"),
    ("TestNtf", "test_01_dedup_window"): ("NTF-002", "дедупликация/группировка лавины событий"),
    ("TestNtf", "test_02_lifecycle_separate_statuses"): ("NTF §4.12", "создание ≠ доставка ≠ просмотр ≠ исполнение"),
    ("TestNtf", "test_03_escalation"): ("NTF-003", "эскалация необработанных критических событий"),
    ("TestDat", "test_01_validation_blocks_publish"): ("DAT-002/EQP-001", "валидация блокирует публикацию; состояния значений"),
    ("TestDat", "test_02_duplicates_to_arbitration"): ("DAT §6.1", "слияние дубликатов не автоматическое"),
    ("TestDat", "test_03_conflict_registry_and_arbitration"): ("DAT-004", "конфликт хранит все значения; арбитраж уполномоченным с основанием"),
    ("TestDat", "test_04_reproducibility_archive"): ("DAT-006", "пакет воспроизводимости с sha256"),
    ("TestDat", "test_05_migration_hash"): ("DAT-005", "контроль целостности архива при миграции"),
    ("TestModels3D", "test_01_obj_roundtrip"): ("GIS-004", "OBJ round-trip"),
    ("TestModels3D", "test_02_gltf_embedded"): ("GIS-006", "glTF 2.0 POSITION (embedded)"),
    ("TestModels3D", "test_03_gltf_external_buffer_error"): ("GIS-006", "внешний буфер без резолвера — ошибка (запрет тихой деградации)"),
    ("TestModels3D", "test_04_citygml_lod1"): ("GIS-006", "CityGML LOD1: здания, контуры, имена"),
    ("TestModels3D", "test_05_ifc_structure"): ("GIS-006", "IFC: пространственная структура; геометрия — честная заглушка (G4)"),
    ("TestG3E2E", "test_01_twin_endpoint"): ("DGT-001…004", "E2E: слои, наблюдаемое состояние, уровень DT, отклонения"),
    ("TestG3E2E", "test_02_calibrate_predict"): ("DGT-005", "E2E: калибровка и прогноз через API"),
    ("TestG3E2E", "test_03_change_flow_maker_checker"): ("CFG-002/003 + SEC-005", "E2E: CR-жизненный цикл, линия до изменения, автор не утверждает"),
    ("TestG3E2E", "test_04_rules_and_validate"): ("CFG-004 + DAT-002", "E2E: реестр правил, проверка контекста, валидация"),
    ("TestG3E2E", "test_05_conflict_flow"): ("DAT-004", "E2E: конфликт → арбитраж (роли, основание, сохранение значений)"),
    ("TestG3E2E", "test_06_archive_and_ntf"): ("DAT-006 + NTF-002/003", "E2E: архив воспроизводимости, дедупликация, эскалация"),
}


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_g3")
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
        "protocol": "ПРОТОКОЛ_G3", "title": "Рубеж G3 «Интеграция и жизненный цикл»",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"), "tz_doc": "ТЗ.АФИНА.11.0",
        "scope": ["DGT-001…005 цифровые двойники", "CFG-002…004 конфигурация и правила",
                  "NTF-002/003 уведомления", "DAT-001…006 качество данных", "GIS-004/006 3D-загрузчики"],
        "tests_total": len(items), "tests_passed": n_pass,
        "verdict": "PASS" if n_pass == len(items) else "FAIL",
        "items": items,
        "honest_notes": [
            "IFC-геометрия — структурная заглушка; полный парсер IfcProduct — рубеж G4",
            "glTF: прочитан POSITION; NORMAL/TEXCOORD/анимации/дрейк — G4",
            "эскалация NTF-003 проверена на модельном времени (created сдвинут), таймеры реального дежурства — стенд G5",
            "прогноз DT-5 в E2E не включается: нет подключённого сценарного модуля на сервере (критерий честно не выполнен)",
        ],
    }
    out = os.path.join(os.path.dirname(__file__), "..", "..", "protocols",
                       f"ПРОТОКОЛ_G3_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(proto, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{proto['verdict']}: {n_pass}/{len(items)} → {os.path.normpath(out)}")
    return 0 if proto["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
