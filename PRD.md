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
- amortized battery wear cost per kWh delivered out of the battery.

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
| Force PV export toggle | optional | external `switch`/`input_boolean`/`binary_sensor`; when on, opts into the active-export branch in §8.1 |

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
   `tomorrow`) and the optional feed-in override and force-PV-export
   toggles.
2. **Battery** — usable capacity (kWh), SoC min/max (%), max charge/discharge
   power (kW), round-trip efficiencies (η_chg, η_dis), **cycle cost
   (`your_currency`/kWh delivered out of the battery — LCOS convention)**
   ≈ `battery_price / (cycles · usable_kWh · η_rt)`, **soft SoC health
   floor (%; default = SoC min, i.e. disabled)** and **low-SoC dwell
   penalty (`your_currency`/(kWh·h); default `0`)** — together they
   implement the §8.5 soft floor.
3. **Solver** — slot length (default 60 min), horizon (default 24 h, max 48 h),
   update interval (default 5 min), max grid import/export (kW), set-point
   write tolerance (W), **minimum sell price (`your_currency`/kWh; default
   `0`)** — slots whose all-in sell price falls below this floor are gated
   off (see §8.4).
4. **Load forecast** — lookback days (default 7), optional cap (kW; 0 = off),
   weekday-aware mode (default off). Skipped when an external
   `load_forecast_entity` was selected in step 1.

Currency is left unspecified on purpose: the planner is currency-agnostic and
all monetary fields use the placeholder unit `your_currency`. As long as buy
price, sell price and cycle cost are expressed in the same currency, the
diagnostic cost / savings sensors come out in that same currency.

The options flow re-exposes both the **Entities** screen and the
**Battery + Solver + Load-forecast** combined form, pre-filled with the
currently active values, so every entity binding and parameter can be
re-pointed after install without removing the integration. Tariff
surcharges (distribution, taxes) are expected to live in the user's price
template (e.g. a Jinja `template.sensor` that adds them on top of the spot
price), not in the integration itself.

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
           + c_cycle·p_dis[t]
           + c_low_soc·deficit[t] ]
```
`c_cycle` is amortised on the discharge leg only — interpret the input as
"currency per kWh delivered out of the battery" (LCOS convention). The
matching charge-side wear is implicit in the input number, which the
config recipe derives as `battery_price / (cycles · usable_kWh · η_rt)`.
Round-trip efficiency (`η_rt < 1`) plus a tiny `eps_cycle · (p_chg+p_dis)`
regulariser keep the LP from co-charging/discharging in the same slot
without needing binary variables, so the problem stays linear. The
optional `deficit[t] ≥ max(0, soc_health − soc[t])` slack and its rate
`c_low_soc` implement the §8.5 soft health floor; both default off
(`c_low_soc = 0`, `soc_health = soc_min`) so the term vanishes for
upgrading users.

Solver: **PuLP** with HiGHS preferred (`highspy`, in-process, no subprocess
exec, ships pre-built wheels) and the bundled CBC binary as fallback. The
chosen backend is cached for the lifetime of the process.

## 8. Plan Execution
Each coordinator tick:
1. Build `OptimizerInputs` from the current HA state and forecasts.
2. Refine slot-0 PV against live measurements (§8.2).
3. Solve the LP for `T` slots from "now".
4. Decide whether slot 0 is **active** or **passive** (§8.1) and translate
   into a Victron set-point in **W** with sign per §5; call
   `number.set_value` on the configured entity (subject to dead-band).
5. Toggle the feed-in switch: ON iff `p_sell[0] > ε` and global feed-in is allowed.
6. Project a *physical* SoC track (§8.3) for diagnostic sensors.
7. Publish diagnostic sensors with the full plan.

Failures (missing sensor, infeasible LP, write error) leave the previous
set-point in place, log a warning, and surface a `last_error` attribute.

### 8.1 Active vs passive control
The Multiplus's native EMS already runs self-consumption (PV → load →
battery → grid surplus, gated by the feed-in switch) when the set-point is
0. Forcing a non-zero set-point speculatively is brittle to forecast
error: a PV undershoot would make the inverter discharge the battery to
defend a target it was never asked to defend. The planner therefore only
overrides the EMS when the LP actively wants to move energy between the
battery and the grid:

| Branch | Predicate (slot 0) | Set-point |
|---|---|---|
| force-discharge | `p_dis > ε ∧ p_sell > ε` | `(p_buy − p_sell) · 1000` (negative) |
| force-charge    | `p_buy > ε ∧ p_chg > ε` | `(p_buy − p_sell) · 1000` (positive) |
| force-export *(opt-in §8.2)* | `toggle_on ∧ p_sell > ε ∧ p_chg < ε ∧ p_dis < ε` | `(p_buy − p_sell) · 1000` (negative) |
| force-hold-import *(§8.6)* | `p_buy > ε ∧ p_chg < ε ∧ p_dis < ε` | `(p_buy − p_sell) · 1000` (positive) |
| passive | none of the above | `0` |

`ε = 1e-3 kW`, shared between control and the §8.3 projection so they
agree on which slots are active.

### 8.2 Force-PV-export & live-PV refinement
Pure-export slots (LP wants to sell PV with the battery idle) are passive
by default. In some scenarios the user wants the surplus pushed to the
grid instead of self-consumed into the battery — typically when a high
morning sell price will be followed by cheap noon recharge anyway. The
optional `force_pv_export_entity` toggle activates the third branch above.

To make force-export safe against forecast error the planner refines slot 0
before solving:

```
pv_kw[0] := min(forecast[0], max(0, live_avg))
```

`live_avg` is a time-weighted average of the configured `pv_power_entity`
over the last `update_seconds` (the coordinator's update cadence, mirrored
into planner config). Recorder-backed; if no samples cover the window
(e.g. fresh install) the forecast is used unchanged. The clamp is
one-sided on purpose — a transient PV burst above the forecast must never
make the LP commit to a sell that the next minute's cloud will retract.

### 8.3 Physical SoC projection
The LP's `soc_start_kwh` is bookkeeping: in passive PV-surplus slots it
stays flat (the LP curtails what it doesn't actively transfer). To show
the user where the battery will *physically* end up, the planner attaches
a second SoC track `soc_physical_kwh` to every slot:

- For active, force-export, and force-hold-import slots, the projection
  follows the LP exactly (`Δsoc = Δt · (η_c · p_chg − p_dis / η_d)`; this
  is `0` for force-export and force-hold-import by construction).
- For passive slots, the projection re-runs the inverter's
  self-consumption rule: surplus PV charges the battery up to `soc_max`,
  deficit discharges down to `soc_min`, the rest spills to grid /
  curtailment.

Plotting both tracks reveals where bookkeeping and reality diverge.

### 8.4 Minimum sell price floor
A user-configurable floor on the slot sell price (default `0`). For every
slot the planner computes:

```
feedin_allowed[t] := feedin_global ∧ (price_sell[t] ≥ min_sell_price)
```

`feedin_allowed=False` is enforced by the optimizer as `p_sell[t] = 0`
(via the per-slot `sell_ub` in §7), so a sub-threshold slot can neither
export PV nor discharge the battery to the grid. The LP then plans
around the lost revenue — typically by storing PV in the battery for a
later, higher-priced slot, or by curtailing if the battery is full.

When the gate fires for slot 0 the §8.1 set-point logic naturally falls
into the passive branch (since both `p_sell` and `p_dis` are 0) and the
feed-in switch goes off (predicate `p_sell[0] > ε` is false), which also
prevents the inverter's native EMS from exporting at the floor price.
Useful when the marginal sell revenue (e.g. 0.10 CZK/kWh) doesn't justify
running the inverter at full export power. Default `0` disables the floor
entirely (any non-negative sell price clears the gate).

### 8.5 Soft SoC health floor
The hard `soc_min` bound prevents the LP from going below the inverter /
chemistry safety floor, but says nothing about *dwelling* there. Without
further input the LP happily drains to `soc_min` mid-afternoon and parks
the battery at the bottom for 18 hours if the next day's prices don't
beat today's; this is fine for revenue but bad for calendar aging,
particularly on NMC and to a lesser extent LFP.

Two opt-in battery parameters add a soft floor *above* `soc_min`:

| Parameter | Default | Unit | Meaning |
|---|---|---|---|
| `soc_health_pct` | `soc_min_pct` | % | SoC the LP is encouraged to stay above |
| `low_soc_penalty_per_kwh_h` | `0` | currency / (kWh·h) | dwell-cost rate below the floor |

Mechanics: per slot the LP gains a slack `deficit[t] ≥ max(0,
soc_health − soc[t])` (and an equivalent `deficit_end` on `soc_end`),
priced into the objective at `Δt · c_low_soc · deficit[t]`. The LP can
still dip below the floor — that's the *soft* part — but only when the
slot's marginal economic gain (sell revenue, displaced buy, etc.) beats
the cumulative penalty of the resulting low-SoC dwell over the rest of
the horizon. A typical configuration (`soc_health = 40 %`,
`c_low_soc = 0.5 CZK/(kWh·h)`) penalises a 1 kWh shortfall at ~0.5 CZK/h,
enough to nudge the LP to recharge during cheap hours, weak enough to
yield to a real evening peak.

Defaults are deliberately a regression no-op: with `soc_health = soc_min`
or `c_low_soc = 0` the slack variables and penalty term aren't created
at all (`health_active = False` short-circuits the LP construction).

### 8.6 Force-hold-import
The §8.1 baseline rule "passive when the LP isn't moving energy between
the battery and the grid" implicitly assumes that the inverter's native
EMS, given set-point `0`, does the same thing the LP planned. That holds
for PV-surplus slots (both prefer self-consumption) but breaks down in
two cases where the LP plans pure grid coverage of the load with the
battery idle:

1. The §8.5 health floor (or any near-floor condition) makes further
   discharge expensive enough that the LP would rather buy from the grid
   — but the inverter's EMS doesn't know about that penalty and drains
   the battery anyway.
2. The configured `soc_min` sits above the inverter's BMS floor (e.g.
   reserve set to 20 % when the BMS allows 10 %); the LP respects
   `soc_min`, the EMS doesn't.

In both cases passive set-point `0` causes the inverter to silently
violate the plan. The fourth branch in the §8.1 table fixes this:

```
force_hold_import := p_buy > ε ∧ p_chg < ε ∧ p_dis < ε
                  → set-point := (p_buy − p_sell) · 1000
```

The set-point pins the grid draw to the LP's planned import (positive,
typically tracking load), which forces the inverter to cover the load
from the grid and leaves the battery idle. Always-on, no toggle: when
the predicate doesn't fire (e.g. battery already at hard floor with no
arbitrage to defend) the forced positive set-point produces the same
physical behaviour as set-point `0`. The §8.3 projection mirrors the
predicate so the displayed physical SoC stays flat.

## 9. Diagnostic Sensors
- `sensor.pv_optimizer_planned_grid_setpoint` (W, current slot)
- `sensor.pv_optimizer_planned_feed_in` (`on`/`off`)
- `sensor.pv_optimizer_expected_cost_horizon` (`your_currency`)
- `sensor.pv_optimizer_savings_vs_passive` (`your_currency`; cost of doing nothing − optimal)
- `sensor.pv_optimizer_plan` — state = next-slot set-point in kW.
  Attributes:
  - `slots`: per-slot list with ISO-tagged `start`, `duration_h`,
    `p_buy_kw`, `p_sell_kw`, `p_chg_kw`, `p_dis_kw`, `soc_start_kwh`,
    `soc_physical_kwh`, `setpoint_w` (the §8.1 set-point the planner
    *would* write for that slot — slot 0 matches the realised
    `planned_grid_setpoint`; the rest is what the planner would write
    next if the LP plan holds, suitable as a chart series without
    re-implementing the predicates).
  - `capacity_kwh`, `soc_min_kwh`, `soc_max_kwh`, `soc_health_kwh`:
    battery params, exposed so frontend cards can compute SoC % and draw
    reserve / ceiling / health-floor lines without hardcoding.
  - `low_soc_penalty_per_kwh_h`: active dwell-penalty rate from §8.5
    (`your_currency`/(kWh·h); `0` = disabled).
  - `force_pv_export_enabled`: last-read state of the §8.2 toggle.
  - `min_sell_price_per_kwh`: active sell-price floor from §8.4
    (`your_currency`/kWh; `0` = disabled).
  - `horizon_slots`, `status`, `solve_time_s`, `error`: solver diagnostics.
- `sensor.pv_optimizer_load_forecast` (next-slot kW; full per-slot series in
  attributes). Only published when the built-in forecaster is active.

## 10. Testing Strategy
- `optimizer.py` covered by deterministic unit tests with hand-designed
  scenarios (free PV, pure arbitrage, feed-in disabled, amortization, limits).
- `planner.py` covered end-to-end via `tests/test_planner.py` against a fake
  state reader / service caller — exercises every supported price shape
  (legacy list, wrapped dict, alternate wrapper auto-discovery, top-level
  ISO-keyed) plus the stale-data hard-fail and set-point dead-band; the
  active vs passive split (§8.1), force-PV-export branch (§8.2) including
  toggle-off / toggle-on / toggle-on-but-no-surplus and live-PV clamp
  below / above / no-history cases; the physical SoC projection
  (§8.3) for passive surplus, force-charge, and force-export slots; and
  the minimum-sell-price gate (§8.4) — default zero is a regression
  no-op, floor above all prices disables export horizon-wide, partial
  floor gates only the cheap slots within the horizon; the soft SoC
  health floor (§8.5) — default penalty zero is a regression no-op,
  cheap charge slot pulls SoC up to the floor, strong sell opportunity
  still allowed to dip below it.
- `load_forecaster.py` covered by `tests/test_load_forecaster.py` —
  median-based spike rejection, partial-history fallback, time-weighted
  bucket averaging, weekday filtering, result caching.
- HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`) are thin
  shims over the pure layer and are exercised in a live HA instance, not in
  this repository's CI.
- No live HA instance required for the pure-layer suite; CI runs `pytest`.
