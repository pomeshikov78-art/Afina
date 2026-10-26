# Готовые сценарии для испытаний и интеграции

| Файл | Размер | Назначение |
|---|---:|---|
| `battalion.json` | 4,6 КБ | Эталон «батальон»: 7 узлов, 8 линий, 5 заявок, парк техники с 🟢🟡🔴. Быстрый прогон и демонстрация |
| `disconnected.json` | 4,6 КБ | Граничный случай: сеть из двух компонент → `PARTIAL` + `ADD_RELAY` с координатами и требуемой ЭИИМ |
| `oversubscribed.json` | 4,2 КБ | Граничный случай: заявок больше ёмкости → `PARTIAL` + `CAPACITY_SHORTAGE` + `ADD_CAPACITY` |
| `regional_500.json` | 418 КБ | Средний граф (500 узлов, ~1 800 рёбер, 20 заявок) — для измерения латентности API |

Крупные графы в репозиторий не кладутся (десятки МБ) — генерируются на месте:

```bash
# 25 000 узлов → examples/stress_25000.json
node -e "
import('./scripts/gen_topology.mjs').then(async ({randomTopology, fleetFor}) => {
  const {writeFileSync} = await import('node:fs');
  const r = randomTopology({n:25000, seed:77, areaKm:600, avgDegree:6, demandRatio:0.01, budget:null});
  r.fleet = fleetFor(r.topology.nodes, {seed:3});
  r.constraints.eng6Analysis = false;
  r.constraints.timeLimitMs = 120000;
  writeFileSync('examples/stress_25000.json', JSON.stringify(r));
});"

# расчёт любого сценария
curl -s -X POST localhost:8080/api/v1/plan/sync \
  -H 'Content-Type: application/json' -H 'X-Requestor: Иванов%20И.И.' \
  --data @examples/battalion.json | jq '{status, metrics, makespan: .plan.schedule.makespanMin}'
```
