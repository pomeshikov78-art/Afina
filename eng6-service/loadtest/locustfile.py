"""
loadtest/locustfile.py — нагрузочное тестирование ENG6-PLAN средствами Locust.

Запуск:
    pip install locust
    locust -f loadtest/locustfile.py --host http://localhost:8080 \
           --users 50 --spawn-rate 5 --run-time 5m

Профили (переменная окружения PROFILE):
    smoke  — 1 пользователь, мелкие графы, проверка доступности;
    load   — штатная нагрузка: смесь мелких (UI/ERP) и средних графов;
    burst  — шквал тяжёлых задач (требование Критика): ожидаем 202/429, без 5xx.

Размер графа задаётся NODES_MIN/NODES_MAX, доля асинхронных запросов — ASYNC_RATIO.

Метрики Locust дополнены собственными счётчиками: статусы плана, время расчёта
в ядре (из заголовка X-Compute-Ms), время в очереди, доля 429/5xx.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from typing import Any

from locust import HttpUser, between, events, task, tag

BASE_NODES_MIN = int(os.getenv("NODES_MIN", "200"))
BASE_NODES_MAX = int(os.getenv("NODES_MAX", "5000"))
ASYNC_RATIO = float(os.getenv("ASYNC_RATIO", "0.35"))
PROFILE = os.getenv("PROFILE", "load").lower()
BURST_NODES = int(os.getenv("BURST_NODES", "20000"))
BURST_CONCURRENCY = int(os.getenv("BURST_CONCURRENCY", "100"))

# ── собственные метрики (выводятся в конце прогона) ───────────────────────
PLAN_STATUS: dict[str, int] = {}
COMPUTE_MS: list[float] = []
QUEUE_MS: list[float] = []
RATE_429 = 0
RATE_5XX = 0
TOTAL = 0


@events.quitting.add_listener
def _report(environment, **_kwargs: Any) -> None:
    stats = environment.stats.total
    pct = stats.get_response_time_percentile(0.95)
    print("\n═══ ENG6-PLAN · сводка Locust ═══")
    print(f"профиль: {PROFILE} · запросов: {stats.num_requests} · ошибок: {stats.num_failures}")
    print(f"RPS: {stats.total_rps:.2f} · латентность p50 {stats.get_response_time_percentile(0.5):.0f} мс · p95 {pct:.0f} мс")
    print(f"статусы плана: {json.dumps(PLAN_STATUS, ensure_ascii=False)}")
    print(f"429: {RATE_429} ({RATE_429 / max(1, TOTAL) * 100:.1f}%) · 5xx: {RATE_5XX} ({RATE_5XX / max(1, TOTAL) * 100:.2f}%)")
    if COMPUTE_MS:
        COMPUTE_MS.sort()
        print(f"расчёт в ядре, мс: p50 {COMPUTE_MS[len(COMPUTE_MS) // 2]:.0f} · p95 {COMPUTE_MS[int(len(COMPUTE_MS) * 0.95)]:.0f} · max {COMPUTE_MS[-1]:.0f}")
    if QUEUE_MS:
        QUEUE_MS.sort()
        print(f"ожидание в очереди, мс: p50 {QUEUE_MS[len(QUEUE_MS) // 2]:.0f} · max {QUEUE_MS[-1]:.0f}")
    if RATE_5XX:
        print("⚠ ЕСТЬ 5xx — нарушение НТЗ (сервис не должен падать под нагрузкой)")
        environment.process_exit_code = 1


# ── генератор топологий ──────────────────────────────────────────────────
def _type_for_distance(dkm: float) -> str:
    if dkm <= 25:
        return "VHF"
    if dkm <= 45:
        return "UHF"
    if dkm <= 90:
        return "RRL"
    return "SAT"


def make_request(n: int, seed: int | None = None) -> dict[str, Any]:
    """Случайный граф: n узлов в квадрате, рёбра — k ближайших соседей (сеточная корзина)."""
    rnd = random.Random(seed if seed is not None else random.randint(0, 2**31))
    area = max(60.0, math.sqrt(n) * 6.0)
    lat0, lng0 = 55.75, 37.62
    pts = [( (rnd() - 0.5) * area, (rnd() - 0.5) * area ) for _ in range(n)]
    nodes = []
    for i, (x, y) in enumerate(pts):
        lat = lat0 + y / 111.32
        lng = lng0 + x / (111.32 * math.cos(math.radians(lat)))
        r = rnd()
        ntype = "cp" if i == 0 or r < 0.02 else "relay" if r < 0.14 else "observer" if r < 0.2 else "sat" if r < 0.26 else "radio"
        nodes.append({
            "id": f"n{i}", "type": ntype, "name": "КП" if ntype == "cp" else f"Узел {i}",
            "lat": round(lat, 6), "lng": round(lng, 6), "status": "active",
            "parentId": None if i == 0 else "n0",
        })
    cell = max(1e-6, area / max(8.0, math.sqrt(n)))
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(pts):
        buckets.setdefault((int((x + area) // cell), int((y + area) // cell)), []).append(i)
    links: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    k = max(1, min(n - 1, 6))
    for i, (x, y) in enumerate(pts):
        cx, cy = int((x + area) // cell), int((y + area) // cell)
        cand: list[int] = []
        for ring in range(4):
            if len(cand) >= k * 3:
                break
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if ring and max(abs(dx), abs(dy)) != ring:
                        continue
                    cand.extend(j for j in buckets.get((cx + dx, cy + dy), []) if j != i)
        cand.sort(key=lambda j: ((x - pts[j][0]) ** 2 + (y - pts[j][1]) ** 2, j))
        for j in cand[:k]:
            key = (i, j) if i < j else (j, i)
            if key in seen:
                continue
            seen.add(key)
            dkm = math.dist(pts[i], pts[j])
            links.append({"id": f"e{len(links)}", "from": f"n{i}", "to": f"n{j}", "type": _type_for_distance(dkm), "priority": 2})
    rates = [2.4, 4.8, 9.6, 16, 16, 32, 64, 128]
    demands = []
    for q in range(max(2, int(n * 0.02))):
        a = rnd.randrange(n)
        b = rnd.randrange(n)
        if a == b:
            b = (b + 1) % n
        demands.append({
            "id": f"q{q}", "from": f"n{a}", "to": f"n{b}",
            "rateKbps": rnd.choice(rates), "maxDelayMs": rnd.choice([150, 250, 400]),
            "priority": rnd.randint(1, 5), "protected": rnd.random() < 0.1,
        })
    return {
        "meta": {"name": f"locust {PROFILE} n={n}", "author": "locust-loadtest", "seed": seed or 0},
        "environment": {
            "clutterType": "open", "terrain": {"source": "flat", "amplitudeM": 0},
            "weather": {"rainRateMmHr": 0, "fogDensity": 0, "temperatureC": 15, "humidityPercent": 60},
            "ionosphere": {"timeOfDay": 12, "season": "equinox", "solarActivity": "medium"},
        },
        "topology": {"nodes": nodes, "links": links},
        "demands": demands,
        "constraints": {
            "budget": None, "minMarginDb": 3, "maxDelayMs": 400, "maxHops": 24,
            "physics": "fast", "timeLimitMs": 240000, "reserveFactor": 1.3, "eng6Analysis": False,
        },
        "weights": {"cost": 0.4, "delay": 0.3, "survivability": 0.2, "margin": 0.1},
    }


_CACHE: dict[int, dict[str, Any]] = {}


def cached_request(n: int) -> dict[str, Any]:
    """Генерация графа дорога — кэшируем по размеру (Locust меряет API, а не генератор)."""
    if n not in _CACHE:
        _CACHE[n] = make_request(n, seed=1000 + n)
    return _CACHE[n]


HEADERS = {"Content-Type": "application/json", "X-Requestor": "locust-loadtest/Инженер", "Accept": "application/json"}


class Eng6User(HttpUser):
    """Пользователь API ENG6-PLAN: смесь sync/async запросов + опрос задач."""

    wait_time = between(0.4, 1.6)

    def on_start(self) -> None:
        self.client.headers.update(HEADERS)

    @task(1)
    @tag("health")
    def health(self) -> None:
        with self.client.get("/healthz", name="/healthz", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"healthz {r.status_code}")

    @task(6)
    @tag("plan")
    def plan(self) -> None:
        global RATE_429, RATE_5XX, TOTAL
        n = BURST_NODES if PROFILE == "burst" else random.randint(BASE_NODES_MIN, BASE_NODES_MAX)
        payload = cached_request(n)
        use_async = PROFILE == "burst" or random.random() < ASYNC_RATIO or n > 5000
        TOTAL += 1
        if use_async:
            self._plan_async(payload, n)
        else:
            self._plan_sync(payload, n)

    def _plan_sync(self, payload: dict[str, Any], n: int) -> None:
        global RATE_429, RATE_5XX
        name = f"POST /api/v1/plan/sync [n={n}]"
        with self.client.post("/api/v1/plan/sync", data=json.dumps(payload), name=name, catch_response=True, timeout=300) as r:
            if r.status_code == 429:
                RATE_429 += 1
                r.success()  # честный отказ под нагрузкой — не ошибка
                return
            if r.status_code >= 500:
                RATE_5XX += 1
                r.failure(f"5xx: {r.status_code} {r.text[:200]}")
                return
            if r.status_code not in (200, 422):
                r.failure(f"неожиданный код {r.status_code}")
                return
            try:
                body = r.json()
            except Exception as exc:  # noqa: BLE001
                r.failure(f"ответ не JSON: {exc}")
                return
            PLAN_STATUS[body.get("status", "?")] = PLAN_STATUS.get(body.get("status", "?"), 0) + 1
            compute = r.headers.get("X-Compute-Ms")
            if compute:
                COMPUTE_MS.append(float(compute))
            if body.get("status") == "ERROR":
                r.failure(f"ядро вернуло ERROR: {json.dumps(body.get('statusReasons'))[:200]}")
            elif not body.get("metrics"):
                r.failure("в ответе нет блока metrics")

    def _plan_async(self, payload: dict[str, Any], n: int) -> None:
        global RATE_429, RATE_5XX
        name = f"POST /api/v1/plan/async [n={n}]"
        with self.client.post("/api/v1/plan/async", data=json.dumps(payload), name=name, catch_response=True, timeout=120) as r:
            if r.status_code == 429:
                RATE_429 += 1
                r.success()
                return
            if r.status_code >= 500:
                RATE_5XX += 1
                r.failure(f"5xx: {r.status_code}")
                return
            if r.status_code != 202:
                r.failure(f"ожидался 202, получен {r.status_code}")
                return
            if not r.headers.get("Location"):
                r.failure("202 без заголовка Location")
                return
            acc = r.json()
        self._poll_task(acc["taskId"])

    def _poll_task(self, task_id: str) -> None:
        global RATE_5XX
        url = f"/api/v1/tasks/{task_id}"
        t0 = time.time()
        for _ in range(600):
            time.sleep(0.5)
            with self.client.get(url, name="GET /api/v1/tasks/{id}", headers={"X-Result-Format": "summary"}, catch_response=True, timeout=30) as r:
                if r.status_code == 404:
                    r.success()
                    return
                if r.status_code >= 500:
                    RATE_5XX += 1
                    r.failure(f"5xx при опросе: {r.status_code}")
                    return
                if r.status_code != 200:
                    r.failure(f"опрос: {r.status_code}")
                    return
                body = r.json()
                state = body.get("state")
                if state in ("QUEUED", "RUNNING"):
                    r.success()
                    continue
                elapsed = (time.time() - t0) * 1000
                if body.get("queueWaitMs") is not None:
                    QUEUE_MS.append(float(body["queueWaitMs"]))
                if body.get("computeMs") is not None:
                    COMPUTE_MS.append(float(body["computeMs"]))
                status = body.get("status") or state
                PLAN_STATUS[status] = PLAN_STATUS.get(status, 0) + 1
                events.request.fire(
                    request_type="TASK", name=f"task:{state}", response_time=elapsed,
                    response_length=len(r.content or b""), exception=None, context={},
                )
                if state != "SUCCEEDED":
                    r.failure(f"задача {state}: {json.dumps(body.get('error'))[:200]}")
                elif not (body.get("result") or {}).get("metrics"):
                    r.failure("SUCCEEDED без metrics")
                return

    @task(1)
    @tag("invalid")
    def invalid_input(self) -> None:
        """Некорректный вход под нагрузкой: ожидаем 4xx и живой сервис."""
        global RATE_5XX, TOTAL
        TOTAL += 1
        payload = {"topology": {"nodes": [{"id": "a", "type": "cp", "lat": 999, "lng": 0}], "links": []}}
        with self.client.post("/api/v1/plan/sync", data=json.dumps(payload), name="POST /api/v1/plan/sync [invalid]", catch_response=True, timeout=30) as r:
            if r.status_code >= 500:
                RATE_5XX += 1
                r.failure(f"мусорный вход вызвал 5xx: {r.status_code}")
            elif r.status_code in (400, 422, 429):
                r.success()
            else:
                r.failure(f"ожидался 400/422/429, получен {r.status_code}")

    @task(1)
    @tag("metrics")
    def metrics_endpoint(self) -> None:
        with self.client.get("/metrics", name="/metrics", catch_response=True, timeout=15) as r:
            if r.status_code != 200:
                r.failure(f"metrics {r.status_code}")
            elif "eng6_plan_duration_ms" not in r.text:
                r.failure("в /metrics нет метрик расчёта")
