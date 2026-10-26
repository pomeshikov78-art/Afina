#!/usr/bin/env python3
"""Симулятор телеметрии (демонстрация адаптера): POST /api/v1/telemetry каждую секунду.

Целевые адаптеры (G3): MQTT-брокер локальный, OSS/EAM CSV-экспорт (см. import_oss_csv.py),
SNMP-опрос (по решению заказчика). Телеметрия обновляет НАБЛЮДАЕМОЕ состояние и не
изменяет базовую линию (DGT-002).
"""
import json, math, random, sys, time, urllib.request

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8470/api/v1"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else None
OBJ = sys.argv[3] if len(sys.argv) > 3 else "SMOKE-1"

def post(metric, value):
    req = urllib.request.Request(API + "/telemetry", data=json.dumps({"object_id": OBJ, "metric": metric, "value": value}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    if TOKEN: req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())

t = 0
print(f"симулятор телеметрии → {API} (объект {OBJ}); остановка Ctrl+C")
while True:
    post("power_w", round(4200 + 180 * math.sin(t / 9) + random.uniform(-40, 40), 1))
    post("t_cabinet_c", round(29 + 3 * math.sin(t / 14) + random.uniform(-0.4, 0.4), 2))
    if t % 30 == 0:
        post("battery_pct", round(max(5, 98 - t / 12), 1))
    t += 1
    time.sleep(1)
