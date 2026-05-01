# Implementation Task List

Stable, ordered checklist that mirrors the in-tool task list. Tick items as
they are completed.

## 0. Documentation
- [x] **PRD.md** — product requirements (architecture, LP, config, tests).
- [x] **TASKS.md** — this file.

## 1. Project skeleton & tooling
- [ ] `pyproject.toml` (package metadata, dev deps: `pulp`, `pytest`,
      `pytest-cov`, optional `pytest-asyncio`).
- [ ] `custom_components/pv_optimizer/manifest.json` (HA integration manifest,
      runtime dep on `pulp`).
- [ ] `custom_components/pv_optimizer/const.py` (DOMAIN, default values, conf
      keys for every entity / parameter).
- [ ] `custom_components/pv_optimizer/__init__.py` (async setup/unload entry
      stubs).
- [ ] `tests/__init__.py`, `tests/conftest.py` (lightweight HA mocks).

## 2. Data models (`models.py`)
- [ ] `BatteryParams` — capacity_kwh, soc_min/max_kwh, p_chg/p_dis_max_kw,
      eta_chg, eta_dis, cycle_cost_eur_per_kwh.
- [ ] `TariffSlot` — start (UTC), duration_h, price_buy, price_sell,
      feedin_allowed.
- [ ] `OptimizerInputs` — slots: list[TariffSlot], pv_kw: list[float],
      load_kw: list[float], initial_soc_kwh, battery, p_grid_imp/exp_max_kw,
      terminal_soc_kwh (optional).
- [ ] `SlotPlan` — t, p_buy, p_sell, p_chg, p_dis, soc_start.
- [ ] `OptimizerResult` — slots: list[SlotPlan], total_cost, status, solve_time.

## 3. Optimizer (`optimizer.py`)
- [ ] `solve(inputs: OptimizerInputs) -> OptimizerResult` using PuLP CBC.
- [ ] All constraints from PRD §7. Raise `OptimizerError` on infeasibility.
- [ ] Helper `passive_cost(inputs)` — cost of doing nothing (battery idle,
      curtail surplus when feed-in disabled). Used for savings sensor.
- [ ] Pure module: no HA imports.

## 4. Optimizer unit tests (`tests/test_optimizer.py`)
- [ ] **Free self-consumption**: pv == load → zero cost, no battery action.
- [ ] **Surplus PV with feed-in allowed**: exports at sell price, profit.
- [ ] **Surplus PV with feed-in disabled**: charges battery instead;
      curtails when battery full.
- [ ] **Arbitrage**: cheap night → expensive day → battery charges then
      discharges; check savings vs passive.
- [ ] **Amortization**: with high `cycle_cost`, no arbitrage cycles unless
      spread > cycle cost; with low cost, cycles happen.
- [ ] **Power limits**: p_chg/p_dis bounded; verify saturation.
- [ ] **Terminal SoC**: cannot end below target.
- [ ] **Infeasible**: too-small grid limit raises `OptimizerError`.
- [ ] **Round-trip efficiency**: `eta < 1` reduces savings as expected.

## 5. Coordinator (`coordinator.py`)
- [ ] `PvOptimizerCoordinator(DataUpdateCoordinator)` —
      `_async_update_data` reads states, builds `OptimizerInputs`, runs
      optimizer in executor, applies first-slot setpoint via service calls.
- [ ] Helpers: `_read_hourly_prices(entity_id)` reads attribute lists like
      Nordpool's `today`/`tomorrow`; tolerant of missing tomorrow.
- [ ] Helpers: `_read_pv_forecast(entity_id)`, `_read_load_forecast(entity_id)`
      — read attribute time series, resample to slot grid.
- [ ] `_apply_setpoint(plan)` — call `number.set_value` and
      `switch.turn_on/off`. Skip when value unchanged within tolerance.
- [ ] Surface `last_error`, `last_plan`, `last_solve_time` on the coordinator.

## 6. Coordinator unit tests (`tests/test_coordinator.py`)
- [ ] Happy path with mocked `hass.states.get` returning fake `State`s and
      mocked `hass.services.async_call`.
- [ ] Missing required sensor → coordinator raises `UpdateFailed`,
      no service call made.
- [ ] Stale forecast (timestamps too old) → fallback path engaged.
- [ ] Set-point write within tolerance → service not called twice.
- [ ] Infeasible LP → keeps previous set-point, error surfaced.

## 7. Config flow (`config_flow.py`)
- [ ] Multi-step flow with steps: `entities`, `battery`, `solver`, `tariffs`.
- [ ] Voluptuous schemas using `EntitySelector(domain=...)` for sensor /
      number / switch fields.
- [ ] `OptionsFlow` mirroring the install steps.

## 8. Config flow unit tests (`tests/test_config_flow.py`)
- [ ] Schema accepts well-formed input, produces correct entry data.
- [ ] Missing required field → `errors` populated.
- [ ] Options flow round-trips values.

## 9. Diagnostic sensors (`sensor.py`)
- [ ] `PlannedGridSetpointSensor`, `PlannedFeedInSensor`,
      `ExpectedCostSensor`, `SavingsSensor`, `PlanSensor` (with full plan in
      `extra_state_attributes`).
- [ ] Use coordinator's `data` and become unavailable on solver failure.

## 10. Sensor unit tests (`tests/test_sensors.py`)
- [ ] Each sensor surfaces the expected value from a fake coordinator.
- [ ] Unavailable when coordinator has no data / last error.

## 11. README & wrap-up
- [ ] `README.md` — installation (HACS-compatible repo layout), configuration
      walkthrough, screenshot placeholders, troubleshooting.
- [ ] `pytest -q` clean run; coverage report attached in README.
