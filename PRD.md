# PV Optimizer — Product Requirements Document

## 1. Purpose
A Home Assistant custom integration (`pv_optimizer`) that minimizes the total
electricity cost of a household equipped with a photovoltaic plant and a battery
(controlled via a Victron Cerbo GX that is already integrated in HA). It produces
a rolling plan over a configurable horizon (default: end of the last known
tariff hour, typically 24–48 h) using **linear programming** and writes the
control set-points for the *current* slot back to HA entities.

The optimizer trades off:
- buying energy from the grid at hourly buy prices,
- selling surplus energy at hourly sell prices,
- (dis)charging the battery,
- amortized battery wear cost per kWh of throughput.

## 2. Scope and Non-Goals
**In scope**
- Reading sensor entities from HA for the inputs listed in §4.
- Solving an LP over a discrete time horizon split into uniform slots.
- Writing the optimal current-slot grid set-point and feed-in switch state to
  user-configured HA entities (typically Victron-backed `number`/`switch`).
- Exposing diagnostic sensors with the latest plan, expected cost and savings.
- Configurable via the HA UI (config flow + options flow).
- Unit-tested business logic (optimizer + coordinator with mocked `hass`).

**Out of scope (initially)**
- Direct MQTT/dbus communication with the Cerbo (we go through HA entities).
- Demand-charge tariffs, tiered/block tariffs, capacity tariffs.
- Multi-vector optimization (heat, EV smart-charging) — left as future work.
- Stochastic / robust optimization — we use deterministic forecasts.

## 3. High-Level Architecture
```
HA sensor entities ──► Coordinator ──► Optimizer (pure) ──► OptimizerResult
                          │                                       │
                          ├──► number.set_value (grid setpoint) ◄─┘
                          ├──► switch.turn_on/off (feed-in)
                          └──► diagnostic sensors (plan, cost)
```
- `optimizer.py` is **pure Python**, no HA imports — fully unit-testable.
- `coordinator.py` (subclass of `DataUpdateCoordinator`) reads states, builds
  an `OptimizerInputs`, runs the optimizer, and applies the first slot.
- `config_flow.py` collects all entity IDs and parameters via the UI.

## 4. Inputs (sensor entities, configurable)
| Input | Required | Notes |
|---|---|---|
| Current household load (W) | yes | instantaneous; used as fallback if no forecast |
| Battery state of charge (%) | yes | converted to kWh via configured capacity |
| PV current power (W) | yes | informational + plan execution feedback |
| Grid power (W, signed) | yes | informational |
| Buy price today, hourly (`your_currency`/kWh) | yes | `list[24]` (Nordpool-style) **or** `dict[iso_timestamp, float]` (timestamp-keyed; preferred — see §4.1) |
| Buy price tomorrow, hourly | optional | same shape; if absent, the horizon is truncated to today's last known hour |
| Sell price today, hourly | yes | may equal buy with FiT subtractor |
| Sell price tomorrow, hourly | optional | as above |
| PV power forecast, hourly (kW) | yes | e.g. `forecast.solar` |
| Load forecast, hourly (kW) | optional | escape hatch; if unset the built-in forecaster (§4.2) is used |
| Feed-in allowed override | optional | external switch forcing feed-in off |

### 4.1 Price-attribute shapes & staleness contract
The tariff attribute (configurable name, default `today` / `tomorrow`) may be
any of four shapes — the planner auto-detects which is in use, in this order:

- **`list[float]` of length 24** under the configured attribute name —
  legacy Nordpool/OTE shape. Indexed by hour of the local day; assumed to
  always represent "today's" 24 hours.
- **`dict[str, float]`** under the configured attribute name, keyed by ISO 8601
  timestamps with timezone offset (e.g. `"2026-05-01T06:00:00+02:00"`).
- **Dict under any other attribute name** — if the configured attribute is
  absent, the planner scans every other dict-valued attribute and picks the
  largest one whose keys parse as ISO timestamps. This lets user-built
  template sensors publish under whatever wrapper name feels natural
  (`prices`, `raw_today`, …) without changing planner config.
- **Top-level ISO-keyed attributes** — the entity's *entire* `attributes`
  dict carries hour-timestamp keys directly, with no wrapping attribute
  (this is what `spot_hodinovy_tarif` and similar Czech/CEE plugins do).
  Unrelated metadata keys (`unit_of_measurement`, `friendly_name`, …) are
  ignored.

The dict shapes are preferred because they carry an explicit "as-of" anchor.

When the dict shape is used:
- Today and tomorrow may be merged into a single entity attribute, or split
  across the today/tomorrow entities — both work.
- If the **current** slot's hour key is absent, the planner refuses to run
  and surfaces a `last_error` ("price data for current slot … is missing —
  stale tariff sensor?"). This protects against a frozen/last-good price
  source where a `list[24]` would silently look fresh.
- If a **future** slot's hour key is absent, the horizon is truncated at the
  first gap. The optimizer simply plans over the shorter window.

### 4.2 Built-in load forecaster
When the optional `load_forecast_entity` is unset, the integration uses an
internal forecaster (`load_forecaster.py`) that derives a per-slot expected
household load from the recorder history of the configured load-power entity.

Algorithm: **median over the last *N* days at the same hour-of-day**, with
optional cap and weekday awareness.

```
forecast[H] = clip(median(load[d, H] for d in last_N_days), 0, cap_kw)
```

Median (rather than mean) is used so that one-off spikes — e.g. a single
EV-charging session, an unusual guest day, an HVAC outlier — do not bias the
forecast. No spike *detection* is required.

Configuration knobs (with defaults):
| Knob | Default | Notes |
|---|---|---|
| `lookback_days` | `7` | Window used per hour-bucket. |
| `cap_kw` | `None` | Optional hard ceiling (kW). |
| `weekday_aware` | `false` | If true, only days with the same weekday as the target slot contribute; needs ~4 weeks of history to be useful. |

The forecaster is consumed directly by the planner (no roundtrip through HA
state). A diagnostic sensor `sensor.pv_optimizer_load_forecast` is also
published with `attributes.kw_per_slot` and `attributes.lookback_days_used`
for observability — nothing reads it back. Setting `load_forecast_entity` to
any user-supplied entity disables the built-in forecaster (escape hatch).

If no usable history exists (fresh install, or no samples at the target hour
for any of the lookback days), the planner falls back to the current
load-power reading for that slot — same behavior as if no forecaster were
present at all.

## 5. Outputs (controlled HA entities, configurable)
| Output | Type | Semantics |
|---|---|---|
| Grid set-point (W) | `number` | Victron AcPowerSetPoint. Sign convention:
  positive = target import (W) at the grid meter, negative = target export. |
| Feed-in allowed | `switch` | When off, optimizer must set `p_sell[t] = 0`. |
| (optional) Max charge power | `number` | If exposed, plan respects it. |
| (optional) Max discharge power | `number` | As above. |

## 6. Configuration (config flow)
Single flow, four steps:
1. **Entities** — pick all sensor & control entity IDs from §4 and §5,
   including the configurable price-attribute names (default `today` /
   `tomorrow`).
2. **Battery** — usable capacity (kWh), SoC min/max (%), max charge/discharge
   power (kW), round-trip efficiencies (η_chg, η_dis), **cycle cost
   (`your_currency`/kWh throughput)** ≈ `battery_price / (cycles ·
   usable_kWh · η_rt)`.
3. **Solver** — slot length (default 60 min), horizon (default 24 h, max 48 h),
   update interval (default 5 min), max grid import/export (kW), set-point
   write tolerance (W).
4. **Load forecast** — lookback days (default 7), optional cap (kW; 0 = off),
   weekday-aware mode (default off). Skipped when an external
   `load_forecast_entity` was selected in step 1.

Currency is left unspecified on purpose: the planner is currency-agnostic and
all monetary fields use the placeholder unit `your_currency`. As long as buy
price, sell price and cycle cost are expressed in the same currency, the
diagnostic cost / savings sensors come out in that same currency.

The options flow re-exposes the **Battery + Solver + Load-forecast** screens
in a single combined form, pre-filled with the currently active values.
Entity selections are install-time only — change them by removing and
re-adding the integration. Tariff surcharges (distribution, taxes) are
expected to live in the user's price template (e.g. a Jinja `template.sensor`
that adds them on top of the spot price), not in the integration itself.

## 7. LP Formulation
Discrete slots `t = 0..T-1` of length `Δt` hours. All variables ≥ 0 unless
noted; `pv[t]`, `load[t]`, `price_buy[t]`, `price_sell[t]`, `feedin_ok[t]`
are parameters.

**Decision variables**
- `p_buy[t]`            — grid import power (kW)
- `p_sell[t]`           — grid export power (kW), upper-bounded by 0 when `!feedin_ok[t]`
- `p_chg[t]`, `p_dis[t]`— battery charge / discharge power (kW)
- `soc[t]`              — energy stored at *start* of slot (kWh), `soc_min ≤ soc[t] ≤ soc_max`

**Constraints (per t)**
- Power balance: `pv[t] + p_dis[t] + p_buy[t] = load[t] + p_chg[t] + p_sell[t]`
- SoC update:    `soc[t+1] = soc[t] + Δt·(η_c·p_chg[t] − p_dis[t]/η_d)`
- Bounds: `0 ≤ p_chg[t] ≤ P_chg_max`, `0 ≤ p_dis[t] ≤ P_dis_max`,
  `0 ≤ p_buy[t] ≤ P_imp_max`, `0 ≤ p_sell[t] ≤ P_exp_max·feedin_ok[t]`
- Terminal SoC ≥ `soc_target` (default = initial SoC, prevents end-of-horizon dump)

**Objective (minimize)**
```
Σ_t Δt · [ price_buy[t]·p_buy[t] − price_sell[t]·p_sell[t]
           + c_cycle·(p_chg[t] + p_dis[t]) ]
```
With `c_cycle > 0` the LP will not co-charge/discharge in the same slot, so no
binary variables are needed and the problem stays linear.

Solver: **PuLP** with the bundled CBC backend (no system deps).

## 8. Plan Execution
Each coordinator tick:
1. Build `OptimizerInputs` from the current HA state and forecasts.
2. Solve the LP for `T` slots from "now".
3. Compute target net grid power for slot 0: `net = p_buy[0] − p_sell[0]` (kW).
4. Convert to Victron set-point in **W** with sign per §5 and call
   `number.set_value` on the configured entity.
5. Toggle the feed-in switch: ON iff `p_sell[0] > ε` and global feed-in is allowed.
6. Publish diagnostic sensors with the full plan.

Failures (missing sensor, infeasible LP, write error) leave the previous
set-point in place, log a warning, and surface a `last_error` attribute.

## 9. Diagnostic Sensors
- `sensor.pv_optimizer_planned_grid_setpoint` (W, current slot)
- `sensor.pv_optimizer_planned_feed_in` (`on`/`off`)
- `sensor.pv_optimizer_expected_cost_horizon` (`your_currency`)
- `sensor.pv_optimizer_savings_vs_passive` (`your_currency`; cost of doing nothing − optimal)
- `sensor.pv_optimizer_plan` (state = next-slot setpoint, attributes = full plan)
- `sensor.pv_optimizer_load_forecast` (next-slot kW; full per-slot series in
  attributes). Only published when the built-in forecaster is active.

## 10. Testing Strategy
- `optimizer.py` covered by deterministic unit tests with hand-designed
  scenarios (free PV, pure arbitrage, feed-in disabled, amortization, limits).
- `planner.py` covered end-to-end via `tests/test_planner.py` against a fake
  state reader / service caller — exercises every supported price shape
  (legacy list, wrapped dict, alternate wrapper auto-discovery, top-level
  ISO-keyed) plus the stale-data hard-fail and set-point dead-band.
- `load_forecaster.py` covered by `tests/test_load_forecaster.py` —
  median-based spike rejection, partial-history fallback, time-weighted
  bucket averaging, weekday filtering, result caching.
- HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`) are thin
  shims over the pure layer and are exercised in a live HA instance, not in
  this repository's CI.
- No live HA instance required for the pure-layer suite; CI runs `pytest`.
