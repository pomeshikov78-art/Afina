"""G1.3 Замеры производительности эталонной CPU-реализации (профили S1–S3).

ЧЕСТНО: это замеры Python-эталона в среде разработки, не целевого C++/Vulkan-ядра.
Нормативы §7.2 утверждаются актом по результатам замеров на целевом оборудовании;
данный протокол фиксирует базовую точку и узкие места для порта.
"""
import json, os, sys, time, platform
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from afina.eng6 import rf
from afina.eng6.pwr_thm import PowerSystem, Load, thermal_steady, ThermalNode

out = {"document": "Протокол замеров производительности (базовая точка G1.3)",
       "date": time.strftime("%Y-%m-%d %H:%M:%S"),
       "environment": {"python": platform.python_version(), "platform": platform.platform(),
                       "backend": "CPU-reference (чистый Python, без NumPy) — worst case для порта"},
       "measurements": []}

def measure(name, fn, repeat=3):
    ts = []
    for _ in range(repeat):
        t0 = time.perf_counter(); r = fn(); ts.append(time.perf_counter() - t0)
    out["measurements"].append({"metric": name, "min_s": round(min(ts), 4), "med_s": round(sorted(ts)[len(ts)//2], 4), "result": r if not isinstance(r, dict) else {k: v for k, v in list(r.items())[:3]}})
    return min(ts)

# S1: интерактивная предварительная оценка (Preview): покрытие 1 км, сетка 25 м, 1 БС
st1 = [rf.BaseStation("BS1", 500, 500, 30)]
t_s1 = measure("S1 Preview RF-03 1×1 км, 25 м, 1 БС (норма ≤ 0.1 с)",
               lambda: rf.coverage_grid(st1, 1000, 1000, 25, model_key="FSPL", threshold_dbm=-95).coverage_pct)

# S2-аналог: сектор 5G r=3 км (без 3D-застройки в эталоне), сетка 25 м
st2 = [rf.BaseStation("BS5G", 3000, 3000, 30, f_mhz=3500, azimuth_deg=0)]
t_s2 = measure("S2 Engineering RF-03 6×6 км, 25 м, сектор 5G SPM (норма ≤ 120 с)",
               lambda: rf.coverage_grid(st2, 6000, 6000, 25, model_key="SPM", threshold_dbm=-100).coverage_pct, repeat=1)

# S3-аналог: 10×10 км, сетка 50 м, 3 БС (норма ≤ 300 с)
st3 = [rf.BaseStation(f"BS{i}", 2000+i*3000, 3000, 30, azimuth_deg=i*120) for i in range(3)]
t_s3 = measure("S3 RF-03 10×10 км, 50 м, 3 БС SPM+SINR (норма ≤ 300 с)",
               lambda: rf.coverage_grid(st3, 10000, 10000, 50, model_key="SPM", calc_sinr=True, threshold_dbm=-100).coverage_pct, repeat=1)

# PWR/THM быстрые операции
measure("PWR-01 баланс (<1 мс)", lambda: PowerSystem(loads=[Load("x", 4200, 5100)]).balance())
measure("THM-01 стационар (<1 мс)", lambda: thermal_steady(ThermalNode("шкаф", 3000), 28))

out["conclusions"] = [
 "S1 Preview норма ≤100 мс: " + ("ВЫПОЛНЕНА" if t_s1 <= 0.1 else "НЕ выполнена на чистом Python — порт ядра на C++ обязателен (план G2); для Preview допустим суррогат L0"),
 "S2/S3: зафиксированы базовые точки эталонной реализации; целевые нормативы утверждаются актом на целевом оборудовании после порта (Приложение З)",
 "Векторизация (NumPy-free C++/OpenMP или GPU-бэкенд ARC-002) — основной путь ускорения сеточных расчётов"]
os.makedirs("protocols", exist_ok=True)
json.dump(out, open(f"protocols/ПРОТОКОЛ_S1S3_{time.strftime('%Y%m%d')}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({m["metric"]: m["med_s"] for m in out["measurements"]}, ensure_ascii=False, indent=1))
