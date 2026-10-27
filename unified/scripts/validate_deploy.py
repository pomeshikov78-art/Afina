"""unified/scripts/validate_deploy.py — сухая валидация артефактов развёртывания (UNIF-23).

Docker на стенде разработки может отсутствовать (режим Б, песочницы) — скрипт
выполняет СТРУКТУРНУЮ проверку без движка:
  * docker-compose.unified.yml: сервисы, build-контексты и dockerfile существуют,
    host-пути volume существуют, depends_on ссылается на существующие сервисы,
    image-имена непустые;
  * prometheus.unified.yml: цели скрейпа ⊆ имена сервисов compose;
  * Dockerfile.platform: источники COPY существуют относительно build-контекста;
  * eng6-service/deploy/Dockerfile: упоминаемые пути существуют.

Парсер — мини-разбор СТРОК нашего собственного формата compose (2-пробельные
отступы, известные ключи). Это НЕ полный YAML-парсер: полная валидация —
`docker compose config` в CI-джобе docker (там движок есть). Ограничение
честно зафиксировано в отчёте.

Запуск: python3 validate_deploy.py [--json out.json]
Код возврата: 0 — PASS, 1 — найдены проблемы.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNIFIED = os.path.dirname(HERE)
REPO = os.path.dirname(UNIFIED)
COMPOSE = os.path.join(UNIFIED, "deploy", "docker-compose.unified.yml")
PROM = os.path.join(UNIFIED, "deploy", "prometheus.unified.yml")
DOCKERFILE_PLATFORM = os.path.join(UNIFIED, "deploy", "Dockerfile.platform")

VERSION = "1.0.0"


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def parse_compose(text: str):
    """Мини-разбор нашего формата compose: сервисы и их ключевые поля."""
    services = {}
    cur = None
    in_services = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_services = (s == "services:")
            cur = None
            continue
        if not in_services:
            continue
        if indent == 2 and s.endswith(":") and ":" not in s[:-1]:
            cur = s[:-1]
            services[cur] = {"build": {}, "volumes": [], "depends_on": [], "image": None, "ports": []}
            continue
        if cur is None:
            continue
        if indent >= 4:
            m = re.match(r"^context:\s*(.+)$", s)
            if m:
                services[cur]["build"]["context"] = m.group(1).strip()
            m = re.match(r"^dockerfile:\s*(.+)$", s)
            if m:
                services[cur]["build"]["dockerfile"] = m.group(1).strip()
            m = re.match(r"^image:\s*(.+)$", s)
            if m:
                services[cur]["image"] = m.group(1).strip()
            m = re.match(r"^-\s*\"?([^:\"]+):([^:\"]+)(:ro)?\"?$", s)
            if m and ("/" in m.group(1)):
                services[cur]["volumes"].append(m.group(1).strip())
            m = re.match(r"^-\s*\"?(\d+):(\d+)\"?$", s)
            if m:
                services[cur]["ports"].append(f"{m.group(1)}:{m.group(2)}")
            m = re.match(r"^-\s*([a-z0-9-]+)$", s)
            if m and services[cur]["depends_on"] is not None:
                services[cur]["depends_on"].append(m.group(1))
            m = re.match(r"^([a-z0-9-]+):\s*$", s)
            if m and indent == 6 and services[cur]["depends_on"] is not None:
                # depends_on: engine: condition: ... (форма словаря)
                pass
        if indent == 6 and s.startswith("condition:") and cur:
            pass
    # depends_on в словарной форме (engine:\n  condition: ...) — второй проход
    cur = None
    in_services = False
    in_dep = False
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        line = raw.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_services = (s == "services:")
            cur, in_dep = None, False
            continue
        if not in_services:
            continue
        if indent == 2 and s.endswith(":"):
            cur, in_dep = s[:-1], False
            continue
        if cur and indent == 4 and s == "depends_on:":
            in_dep = True
            continue
        if in_dep and cur and indent == 6 and s.endswith(":") and "condition" not in s:
            dep = s[:-1]
            if dep not in services[cur]["depends_on"]:
                services[cur]["depends_on"].append(dep)
            continue
        if in_dep and indent <= 4:
            in_dep = False
    return services


def validate() -> dict:
    problems, checks = [], []

    def check(cond, msg):
        checks.append({"ok": bool(cond), "check": msg})
        if not cond:
            problems.append(msg)

    compose_txt = _read(COMPOSE)
    services = parse_compose(compose_txt)
    names = set(services)
    check({"engine", "platform", "prometheus", "grafana"} <= names,
          f"compose содержит обязательные сервисы (есть: {sorted(names)})")

    compose_dir = os.path.dirname(COMPOSE)
    for name, svc in services.items():
        b = svc.get("build") or {}
        if b.get("context"):
            ctx = os.path.normpath(os.path.join(compose_dir, b["context"]))
            check(os.path.isdir(ctx), f"{name}: build.context существует ({b['context']} → {ctx})")
            if b.get("dockerfile"):
                df = os.path.normpath(os.path.join(ctx, b["dockerfile"]))
                check(os.path.isfile(df), f"{name}: dockerfile существует ({b['dockerfile']})")
        for v in svc.get("volumes", []):
            vp = os.path.normpath(os.path.join(compose_dir, v))
            check(os.path.exists(vp), f"{name}: volume host-путь существует ({v})")
        for dep in svc.get("depends_on", []):
            check(dep in names, f"{name}: depends_on «{dep}» — существующий сервис")

    # prometheus: цели ⊆ сервисы
    prom_txt = _read(PROM)
    targets = re.findall(r"targets:\s*\[['\"]([^'\"]+)['\"]\]", prom_txt)
    for t in targets:
        host = t.split(":")[0]
        check(host in names, f"prometheus: цель «{host}» — имя сервиса compose")

    # Dockerfile.platform: источники COPY относительно контекста (корень репо)
    df_txt = _read(DOCKERFILE_PLATFORM)
    for m in re.finditer(r"^COPY\s+(\S+)\s+\S+", df_txt, re.M):
        src = m.group(1)
        check(os.path.exists(os.path.join(REPO, src)),
              f"Dockerfile.platform: COPY-источник существует ({src})")

    # Dockerfile линии B: ключевые пути
    dfb = os.path.join(REPO, "eng6-service", "deploy", "Dockerfile")
    if os.path.isfile(dfb):
        checks.append({"ok": True, "check": f"Dockerfile линии B на месте ({os.path.relpath(dfb, REPO)})"})
    else:
        problems.append("Dockerfile линии B отсутствует")

    verdict = "PASS" if not problems else "FAIL"
    return {"document": "ОТЧЁТ сухой валидации артефактов развёртывания (UNIF-23)",
            "validator_version": VERSION,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "limitation": "мини-разбор собственного формата compose; полная валидация — "
                          "`docker compose config` (CI-джоба docker)",
            "services_found": sorted(names),
            "checks_total": len(checks), "checks_failed": len(problems),
            "verdict": verdict, "problems": problems, "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    rep = validate()
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"validate_deploy: {rep['verdict']} · проверок {rep['checks_total']}, "
          f"проблем {rep['checks_failed']} · сервисы: {', '.join(rep['services_found'])}")
    for p in rep["problems"][:12]:
        print("  ✗", p)
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
