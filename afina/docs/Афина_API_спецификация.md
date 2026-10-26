# Спецификация REST API /api/v1 (позиция 6 комплекта, §11.1)

Сгенерировано интроспекцией маршрутов 2026-10-18 14:40:47 · версия API: v1 · формат: JSON (UTF-8)
Аутентификация: `Authorization: Bearer <token>` (scrypt+HMAC, TTL 8 ч). RBAC: операция проверяется для роли каждого запроса (Приложение Н); отказ → 403 + запись в аудит (SEC-001).
Идемпотентность: POST с `client_id` не создаёт дубликатов (INT-002). Тяжёлые расчёты — через очередь (ARC-003): 202 + job_id, опрос `GET /calc/<job_id>`.
Ошибки: 400 (валидация), 401 (сессия), 403 (RBAC/Maker–Checker), 404, 409 (конфликт/отказ по правилам), 500 — с политикой «ошибка не подменяется упрощённым результатом».

| Метод | Путь | Операция RBAC | Авторизация |
|---|---|---|---|
| POST | `/api/v1/register` | read | нет (bootstrap/login) |
| POST | `/api/v1/login` | read | нет (bootstrap/login) |
| GET | `/api/v1/session` | read | да |
| GET | `/api/v1/objects` | read | да |
| POST | `/api/v1/objects` | edit_models_cond | да |
| POST | `/api/v1/locks` | edit_models_cond | да |
| DELETE | `/api/v1/locks` | edit_models_cond | да |
| POST | `/api/v1/calc` | run_calc | да |
| GET | `/api/v1/calc/(.*)` | read | да |
| GET | `/api/v1/queue` | read | да |
| GET | `/api/v1/notifications` | read | да |
| POST | `/api/v1/notifications/(d+)/advance` | read | да |
| GET | `/api/v1/zam` | read | да |
| POST | `/api/v1/zam` | edit_models_cond | да |
| POST | `/api/v1/zam/([w-]+)/advance` | read | да |
| POST | `/api/v1/telemetry` | read | да |
| GET | `/api/v1/audit` | audit_read | да |
| GET | `/api/v1/audit/verify` | audit_read | да |
| GET | `/api/v1/export/objects` | export_limited | да |
| POST | `/api/v1/baselines` | baseline_fix | да |
| GET | `/api/v1/baselines` | read | да |
| GET | `/api/v1/catalog/equipment` | read | да |
| GET | `/api/v1/catalog/org_units` | read | да |
| GET | `/api/v1/requirements` | read | да |
| GET | `/api/v1/terrain` | read | да |
| POST | `/api/v1/ai/ask` | read | да |
| GET | `/api/v1/ai/anomalies` | read | да |
| GET | `/api/v1/twins/([^/]+)` | read | да |
| POST | `/api/v1/twins/([^/]+)/divergence` | edit_models_cond | да |
| POST | `/api/v1/twins/calibrate` | run_calc | да |
| POST | `/api/v1/twins/predict` | run_calc | да |
| GET | `/api/v1/changes` | read | да |
| POST | `/api/v1/changes` | edit_models_cond | да |
| POST | `/api/v1/changes/([w.-]+)/advance` | read | да |
| GET | `/api/v1/rules` | read | да |
| POST | `/api/v1/rules/check` | read | да |
| POST | `/api/v1/validate` | read | да |
| GET | `/api/v1/conflicts` | read | да |
| POST | `/api/v1/conflicts` | edit_models_cond | да |
| POST | `/api/v1/conflicts/([w.-]+)/resolve` | read | да |
| POST | `/api/v1/archive/([w.-]+)` | baseline_fix | да |
| POST | `/api/v1/ntf` | read | да |
| POST | `/api/v1/escalation` | read | да |
| POST | `/api/v1/twins/([^/]+)/scenario` | run_calc | да |
| GET | `/api/v1/scenarios` | read | да |
| POST | `/api/v1/scenarios/([w.-]+)/validate` | verify | да |
| POST | `/api/v1/ai/explain` | read | да |
| POST | `/api/v1/ai/suggest` | read | да |
| POST | `/api/v1/surrogate/train` | run_calc | да |
| GET | `/api/v1/surrogates` | read | да |
| POST | `/api/v1/surrogate/predict` | read | да |
| POST | `/api/v1/surrogates/([w.-]+)/compare` | run_calc | да |
| POST | `/api/v1/surrogates/([w.-]+)/validate` | verify | да |
| POST | `/api/v1/surrogates/([w.-]+)/disable` | dict_edit | да |
| GET | `/api/v1/vt/cooperation` | read | да |
| POST | `/api/v1/vt/participants` | dict_edit | да |
| POST | `/api/v1/vt/transfers` | export_limited | да |
| GET | `/api/v1/vt/stages` | read | да |
| POST | `/api/v1/vt/stages` | assign | да |
| POST | `/api/v1/vt/stages/([w.-]+)/mark` | tasks | да |
| POST | `/api/v1/vt/stages/([w.-]+)/commission` | approve | да |
| GET | `/api/v1/vt/benches` | read | да |
| POST | `/api/v1/vt/benches` | dict_edit | да |
| POST | `/api/v1/vt/tests` | tasks | да |
| GET | `/api/v1/vt/tests/([w.-]+)/passport` | read | да |
| POST | `/api/v1/vt/tests/([w.-]+)/approve` | verify | да |
| GET | `/api/v1/vt/unification` | read | да |
| GET | `/api/v1/vt/readiness` | read | да |
| POST | `/api/v1/vt/conditions` | tasks | да |
| GET | `/api/v1/vt/conditions` | read | да |
| GET | `/api/v1/vt/aging` | read | да |
| GET | `/api/v1/vt/panel` | read | да |
| GET | `/api/v1/game/rules` | read | да |
| POST | `/api/v1/game/award` | read | да |
| GET | `/api/v1/game/scores` | read | да |
| POST | `/api/v1/game/revoke` | verify | да |
| GET | `/api/v1/game/polygon` | read | да |
| GET | `/api/v1/game/indices` | read | да |
| GET | `/api/v1/game/missions` | read | да |
| POST | `/api/v1/game/missions/([w-]+)/start` | read | да |
| POST | `/api/v1/game/missions/([w-]+)/check` | read | да |
| POST | `/api/v1/game/missions/([w-]+)/hint` | read | да |
| GET | `/api/v1/analytics/trends` | read | да |
| POST | `/api/v1/surrogate/demo_train` | run_calc | да |
| POST | `/api/v1/projects` | edit_models_cond | да |
| GET | `/api/v1/projects` | read | да |
| GET | `/api/v1/report/summary` | read | да |

## Схемы ключевых запросов
- `POST /calc` RF-03: `{function, model: OKUMURA_HATA|COST231_HATA|SUI|SPM|FSPL|INDOOR_P1238, width_m, height_m, cell_m, threshold_dbm, env, stations:[{id,x_m,y_m,h_ant_m,f_mhz,tx_power_dbm,ant_gain_dbi,sector}]}` → 202 `{job_id}`; результат: `{coverage_pct, grid, passport{run_id,model_version,inputs_hash,environment,uncertainty_db,applicability}}`.
- `POST /calc` OPT-01: `{variants, criteria:[{key,name,direction,weight,hard_min,hard_max}], constraints:[{type:subset_of|forbidden_value|all_required|numeric_bound,...}]}` → `{feasible_count, rejected[{variant,reason}], pareto_front, ranked, best, passport}`.
- `POST /calc` COSIM-01: `{scenario_id, seed:{w_nom,w_peak,t_ambient_c,t_limit_c,cooling_w,mtbf_h,mttr_h}}` → `{ok, data, log, versions, unit_checks, passport}`.
- Единицы и CRS: реестры ENG-004/GIS-001; даты — epoch seconds (DOUBLE), формат ответов — JSON.
- Корректный пример: см. `pmi/smoke_g4.py` (рабочие вызовы всех семейств). Некорректный: `POST /calc {function:'MAGIC-99'}` → 400 «функция не зарегистрирована» (тихая подмена запрещена).
