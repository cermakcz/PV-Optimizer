# Implementation Task List

Living checklist tracking what is actually shipped vs. still open. Pure-layer
items (`models`, `optimizer`, `planner`, `load_forecaster`) are covered by
the in-repo `pytest` suite. HA-side items (`coordinator`, `config_flow`,
`sensor`) are exercised in a live HA instance, not in CI.

## 0. Documentation
- [x] **PRD.md** — product requirements (architecture, LP, config, tests).
- [x] **TASKS.md** — this file.
- [x] **README.md** — install, configuration walkthrough, ApexCharts examples.

## 1. Project skeleton & tooling
- [x] `pyproject.toml` — package metadata + dev deps (`pulp`, `pytest`,
      `pytest-cov`).
- [x] `custom_components/pv_optimizer/manifest.json` — HA integration
      manifest with runtime dep on `pulp`.
- [x] `custom_components/pv_optimizer/const.py` — `DOMAIN`, defaults, conf
      keys for every entity / parameter (incl. load-forecaster knobs).
- [x] `custom_components/pv_optimizer/__init__.py` — async setup/unload.
- [x] `tests/` — no HA mocks needed; pure layer is HA-free.

## 2. Data models (`models.py`)
- [x] `BatteryParams` — capacity, SoC bounds, p_chg/p_dis_max, η_chg, η_dis,
      `cycle_cost_per_kwh`.
- [x] `TariffSlot` — start (UTC), duration_h, buy/sell, feedin_allowed.
- [x] `OptimizerInputs` — slots, pv_kw, load_kw, initial_soc_kwh, battery,
      grid import/export caps, optional terminal SoC.
- [x] `SlotPlan` — t, p_buy, p_sell, p_chg, p_dis, soc_start.
- [x] `OptimizerResult` — slots, total_cost, status, solve_time.

## 3. Optimizer (`optimizer.py`)
- [x] `solve(inputs) -> OptimizerResult` via PuLP + bundled CBC.
- [x] All constraints from PRD §7. `OptimizerError` on infeasibility.
- [x] `passive_cost(inputs)` — used by the savings sensor.
- [x] Pure module: no HA imports.

## 4. Optimizer unit tests (`tests/test_optimizer.py`)
- [x] Free self-consumption, surplus-with-feed-in, surplus-without-feed-in,
      pure arbitrage, amortization gating, power-limit saturation, terminal
      SoC, infeasibility, round-trip-efficiency drag.

## 5. Planner (`planner.py`) — pure read→solve→apply pipeline
- [x] `Planner.step(now)` — reads state, solves, applies set-point + switch.
- [x] `_read_price_source` 4-stage fallback: configured-attr dict →
      configured-attr list → other-attr dict auto-discovery → top-level
      ISO-keyed attributes. Stale-data hard-fail on missing current hour.
- [x] `_read_pv_forecast` — `forecast.solar` `wh_hours` shape and
      `forecast: [{datetime, power_kw}]` shape.
- [x] Set-point write dead-band (`setpoint_tolerance_w`, default 50 W).
- [x] Surfaces `last_error`, `last_result`, `applied_setpoint_w`,
      `last_solve_time` on the cycle object.

## 6. Planner unit tests (`tests/test_planner.py`)
- [x] Happy path with fake state reader + service caller.
- [x] Each price-attribute shape (list, wrapped dict, alternate wrapper
      auto-discovery, top-level ISO-keyed).
- [x] Stale current-hour dict → planner refuses to run.
- [x] Set-point dead-band: identical-within-tolerance → no second write.
- [x] Infeasible LP → previous set-point retained, error surfaced.

## 7. Built-in load forecaster (`load_forecaster.py`)
- [x] `LoadForecaster.forecast_kw(now, n_slots, slot_h)` —
      median-over-`lookback_days` per hour-of-day bucket.
- [x] Time-weighted averaging within a day's hour bucket.
- [x] Optional `cap_kw` ceiling, optional `weekday_aware` filtering.
- [x] Result caching keyed on `(now_floor, n_slots, slot_h)` to avoid
      re-querying recorder every coordinator tick.
- [x] Falls back to current load reading when no usable history.

## 8. Load-forecaster unit tests (`tests/test_load_forecaster.py`)
- [x] Median rejects single-day spikes.
- [x] Partial / empty history → fallback path.
- [x] Time-weighted bucket averaging math.
- [x] Weekday filtering picks only same-DOW history.
- [x] Cache hit avoids second recorder query for identical args.

## 9. Coordinator (`coordinator.py`)
- [x] `PvOptimizerCoordinator(DataUpdateCoordinator)` wraps `Planner.step`
      in an executor; pulls load forecast from the built-in forecaster (or
      the external entity, if configured).
- [x] Surfaces `last_error`, `last_plan`, `last_solve_time`, latest load
      forecast on the coordinator's `data`.

## 10. Config flow (`config_flow.py`)
- [x] Multi-step install flow: **entities → battery → solver → load_forecast**.
- [x] `EntitySelector(domain=…)` for sensor / number / switch fields.
- [x] Combined `OptionsFlow` re-exposing battery + solver + load-forecast on
      one screen, pre-filled with current values.
- [x] HA 2025.x compatibility: no manual `self.config_entry` assignment in
      `OptionsFlow.__init__` (uses the read-only property).
- [x] Conditional `unit_of_measurement` (omit when not applicable; never
      pass `None` to voluptuous).

## 11. Diagnostic sensors (`sensor.py`)
- [x] `PlannedGridSetpointSensor`, `PlannedFeedInSensor`,
      `ExpectedCostSensor`, `SavingsSensor`, `PlanSensor` (full plan in
      `extra_state_attributes`), `LoadForecastSensor`.
- [x] Currency-agnostic units (`your_currency`) on cost / savings sensors.
- [x] Sensors become unavailable on solver failure / no data.

## 12. Live verification (HAOS)
- [x] Integration loads under HA 2025.x without `Invalid handler specified`.
- [x] Options flow round-trips current battery / solver / forecaster values.
- [x] Price discovery works against user template sensor publishing under
      a non-default attribute name (`prices`).
- [ ] Multi-day observation of `expected_cost_horizon` vs. realized cost.
- [ ] Observation of `cycle_cost` tuning effect on number of cycles/day.

## 13. Future work (not in scope of v1)
- [ ] MPC-style stochastic forecasts (price/PV uncertainty bands).
- [ ] EV smart-charging as a second controllable load.
- [ ] Heat-pump / DHW thermal storage as additional state.
- [ ] Demand-charge / capacity-tariff terms in the objective.
