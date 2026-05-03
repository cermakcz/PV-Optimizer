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
- [x] `solve(inputs) -> OptimizerResult` via PuLP, preferring HiGHS
      (`highspy`, in-process) with bundled CBC as fallback. Solver
      factory cached for the lifetime of the process; missing-binary /
      wrong-arch errors surfaced as `OptimizerError`.
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
- [x] Active vs passive split (PRD §8.1): set-point only forced non-zero
      on force-charge / force-discharge; passive slots stay at 0 and let
      the inverter EMS run self-consumption.
- [x] Optional force-PV-export branch (PRD §8.2): toggle entity flips
      pure PV-export slots from passive to active to override the
      inverter's bias toward charging the battery.
- [x] Slot-0 PV refinement (PRD §8.2): `min(forecast, trailing_avg)`
      against a `LiveAverager` over the last `update_seconds`. Clamp is
      one-sided; missing history falls back to the forecast.
- [x] Physical SoC projection (PRD §8.3): per-slot `soc_physical_kwh`
      attached to LP results — follows the LP exactly in active /
      force-export slots, simulates self-consumption in passive slots.
- [x] Surfaces `last_error`, `last_result`, `applied_setpoint_w`,
      `last_solve_time`, `force_pv_export_enabled` on the cycle object.

## 6. Planner unit tests (`tests/test_planner.py`)
- [x] Happy path with fake state reader + service caller.
- [x] Each price-attribute shape (list, wrapped dict, alternate wrapper
      auto-discovery, top-level ISO-keyed).
- [x] Stale current-hour dict → planner refuses to run.
- [x] Set-point dead-band: identical-within-tolerance → no second write.
- [x] Infeasible LP → previous set-point retained, error surfaced.
- [x] Force-charge / force-discharge / passive PV-surplus branches.
- [x] Force-PV-export toggle: off / on with surplus / on without surplus.
- [x] Live-PV clamp: live below forecast / live above forecast / no history.
- [x] Physical SoC projection: passive surplus, force-charge, force-export.

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
- [x] `_HassLiveAverager` — recorder-backed time-weighted avg supplied to
      the planner for the slot-0 PV refinement (PRD §8.2). Window length
      mirrors `update_seconds` so every replan looks back exactly one
      cycle.
- [x] Exposes the active `PlannerConfig` on the coordinator so HA-side
      entities (e.g. `PlanSensor`) can surface battery / horizon
      parameters as attributes without reaching into planner internals.

## 10. Config flow (`config_flow.py`)
- [x] Multi-step install flow: **entities → battery → solver → load_forecast**.
- [x] `EntitySelector(domain=…)` for sensor / number / switch fields,
      including the optional force-PV-export toggle.
- [x] Combined `OptionsFlow` re-exposing **entities + battery + solver +
      load-forecast** on one screen, pre-filled with current values, so
      every binding can be re-pointed without removing the integration.
- [x] HA 2025.x compatibility: no manual `self.config_entry` assignment in
      `OptionsFlow.__init__` (uses the read-only property).
- [x] Conditional `unit_of_measurement` (omit when not applicable; never
      pass `None` to voluptuous).

## 11. Diagnostic sensors (`sensor.py`)
- [x] `PlannedGridSetpointSensor`, `PlannedFeedInSensor`,
      `ExpectedCostSensor`, `SavingsSensor`, `PlanSensor` (full plan in
      `extra_state_attributes`), `LoadForecastSensor`.
- [x] `PlanSensor` attributes include `capacity_kwh`, `soc_min_kwh`,
      `soc_max_kwh` (for SoC % conversion / reserve lines in dashboards),
      `force_pv_export_enabled` (toggle state), and per-slot
      `soc_physical_kwh` alongside `soc_start_kwh`.
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
