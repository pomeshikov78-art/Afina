"""unified/bridge/xverify.py — стенд кросс-верификации моделей (шаг 6 плана совмещения).

Запуск:  python3 xverify.py [--out ../protocols]
Результат: ПРОТОКОЛ_ARC002_XVER_<ts>.json + .md — двухстадийная сверка
(RAW → ALIGNED) Python ENG6+ (линия A) ⇄ вендор ENG6 v5.1.0 (линия B):
сетка точек, допуски, согласования, максимальные расхождения, вердикт.

Политика честности (наследуется от обеих линий):
  * расхождение выше допуска — не «ошибка теста», а измеренная величина
    (стадия RAW сохраняет факт; ALIGNED фиксирует согласование);
  * несовместимые семантики не подгоняются, а документируются и исключаются
    из ALIGNED с обоснованием (KNOWN_INCOMPATIBLE);
  * сетка детерминирована и сохраняется в протоколе (воспроизводимость);
  * отпечатки обоих путей (sha256) — в шапке протокола.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend_arc002 import (Eng6ProbeBackend, PythonRefBackend, default_grid,  # noqa: E402
                            verify_arc002)


def _stage_table(stage: dict, tolerances: dict) -> list:
    s = stage["summary"]
    lines = [f"### Стадия {stage['stage']}: **{stage['verdict']}** — CONSISTENT {s['consistent']}, "
             f"DEGRADED {s['degraded']}, FAIL {s['failed']}, EXCLUDED {s['excluded']}",
             "",
             "| Модель | max Δ | Допуск | Ед. |",
             "|---|---:|---:|---|"]
    for m, d in s["max_delta_by_model"].items():
        t = tolerances.get(m, {})
        lines.append(f"| {m} | {d} | {t.get('tol', '?')} | {t.get('unit', '?')} |")
    bad = [r for r in stage["rows"] if r["verdict"] not in ("CONSISTENT", "EXCLUDED")]
    if bad:
        lines += ["", "Точки вне допуска (первые 15):", "",
                  "| Модель | Параметры | A | B | Δ | Вердикт |",
                  "|---|---|---:|---:|---:|---|"]
        for r in bad[:15]:
            p = r["point"]
            prm = ", ".join(f"{k}={v}" for k, v in p.items() if k != "model")
            lines.append(f"| {p['model']} | {prm} | {r.get('a', r.get('a_error', '—'))} "
                         f"| {r.get('b', r.get('b_error', '—'))} | {r.get('delta', '—')} | {r['verdict']} |")
    return lines


def render_md(proto: dict) -> str:
    lines = [
        "# ПРОТОКОЛ ARC-002 / XVER — сверка двух вычислительных путей",
        "",
        "| Реквизит | Значение |",
        "|---|---|",
        f"| Дата | {proto['generated_at']} |",
        f"| Требование | {proto['requirement']['id']} ({proto['requirement']['priority']}): {proto['requirement']['criteria']} |",
        f"| Путь A | `{proto['path_a']['backend']}` v{proto['path_a']['version']} (sha256 {str(proto['path_a'].get('sha256'))[:16]}…) |",
        f"| Путь B | `{proto['path_b']['backend']}` v{proto['path_b']['version']} (sha256 {str(proto['path_b'].get('sha256'))[:16]}…) |",
        f"| Точек сетки | {proto['grid_points']} |",
        f"| ИТОГОВЫЙ ВЕРДИКТ (по ALIGNED) | **{proto['verdict']}** |",
        "",
        "## Документированные согласования (стадия ALIGNED)",
        "",
        "| ID | Модель | Согласование |",
        "|---|---|---|",
    ]
    for m, al in proto["alignments"].items():
        lines.append(f"| {al['id']} | {m} | {al['note']} |")
    lines += ["", "## Документированные несовместимости (исключены из ALIGNED)", "",
              "| ID | Модель/env | Обоснование |", "|---|---|---|"]
    for inc in proto["known_incompatible"]:
        lines.append(f"| {inc['id']} | {inc['model']}/{inc['env']} | {inc['reason']} |")
    if proto.get("model_notes"):
        lines += ["", "## Примечания к парам моделей", "", "| Модель | Примечание |", "|---|---|"]
        for m, note in proto["model_notes"].items():
            lines.append(f"| {m} | {note} |")
    lines += ["", "## Стадии сверки", ""]
    lines += _stage_table(proto["stages"]["raw"], proto["tolerances"])
    lines += [""]
    lines += _stage_table(proto["stages"]["aligned"], proto["tolerances"])
    lines += ["", "---",
              "Полная построчная сетка обеих стадий — в JSON-версии протокола. Стенд: unified/bridge/xverify.py. "
              "Повторный запуск обязан дать идентичные значения сетки (детерминированность обеих линий, NFR-04). "
              "Вендор ENG6 не изменялся (§9.3.1 Дополнения №2); все согласования выполнены на стороне платформы."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Кросс-верификация Python ENG6+ ⇄ вендор ENG6 v5.1.0")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "protocols"))
    args = ap.parse_args()

    a, b = PythonRefBackend(), Eng6ProbeBackend()
    proto = verify_arc002(a, b, default_grid())

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.out, exist_ok=True)
    jp = os.path.join(args.out, f"ПРОТОКОЛ_ARC002_XVER_{ts}.json")
    mp = os.path.join(args.out, f"ПРОТОКОЛ_ARC002_XVER_{ts}.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(proto, f, ensure_ascii=False, indent=1)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_md(proto))

    raw_s, al_s = proto["stages"]["raw"]["summary"], proto["stages"]["aligned"]["summary"]
    print(f"XVER: {proto['grid_points']} точек · RAW {proto['stages']['raw']['verdict']} "
          f"(max Δ {json.dumps(raw_s['max_delta_by_model'], ensure_ascii=False)}) · "
          f"ALIGNED {proto['stages']['aligned']['verdict']} "
          f"(CONSISTENT {al_s['consistent']}, DEGRADED {al_s['degraded']}, FAIL {al_s['failed']}, EXCLUDED {al_s['excluded']})")
    print(f"  протокол: {jp}")
    return 0 if proto["verdict"] in ("PASS", "DEGRADED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
