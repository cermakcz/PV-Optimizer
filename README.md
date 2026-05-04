# PV Optimizer

Home Assistant custom integration that minimises electricity cost for a
PV + battery + grid system using **linear programming**. Designed for
installations where the inverter/charger is controlled via a Victron Cerbo GX
already integrated with Home Assistant.

> Detailed design: see [PRD.md](PRD.md). Implementation steps: [TASKS.md](TASKS.md).

## What it does
At every update tick the integration:

1. Reads household load, PV power, battery SoC, hourly buy/sell tariffs, and
   PV/load forecasts from user-configured HA sensor entities.
2. Builds an LP over a configurable horizon (default 24 h, hourly slots).
3. Solves it with PuLP, preferring **HiGHS** (in-process via `highspy`) and
   falling back to the bundled CBC binary, minimising
   `Σ (price_buy·p_buy − price_sell·p_sell + cycle_cost·throughput)·Δt`
   subject to power balance, battery dynamics, SoC bounds, charge/discharge
   limits, and grid import/export limits. Battery wear is included via a
   per-kWh **cycle (amortization) cost**.
4. Writes the optimal current-slot grid set-point (W) and feed-in switch
   state to the configured Victron-backed `number`/`switch` entities.
   The set-point is only forced non-zero when the LP actively wants to move
   energy between battery and grid; in passive slots it stays at 0 and the
   inverter handles self-consumption itself (see *Active vs passive control*
   below).

The optimizer is a pure Python module (`optimizer.py`) with no Home Assistant
imports, fully covered by unit tests.

## Repository layout
```
custom_components/pv_optimizer/
  models.py            # Pure dataclasses (BatteryParams, OptimizerInputs, ...)
  optimizer.py         # LP formulation + solve()
  planner.py           # Pure read→solve→apply pipeline (testable, no HA)
  load_forecaster.py   # Median-over-N-days load forecaster (testable, no HA)
  coordinator.py       # HA DataUpdateCoordinator shim around Planner
  config_flow.py       # Multi-step UI configuration
  sensor.py            # Diagnostic sensors
  const.py             # Configuration keys + defaults
  manifest.json        # HA manifest
tests/
  test_optimizer.py
  test_planner.py
  test_load_forecaster.py
PRD.md  TASKS.md
```

## Installation
1. Copy `custom_components/pv_optimizer/` into your Home Assistant
   `config/custom_components/` directory (HACS-compatible repo layout).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "PV Optimizer"**.

## Configuration
Four steps in the UI:

| Step | What you provide |
|---|---|
| Entities | Sensor IDs for load, PV, grid, SoC, buy/sell prices (today + optional tomorrow), PV forecast, optional load forecast, optional feed-in override, optional **force-PV-export** toggle; plus the `number` for the Victron grid set-point and the `switch` controlling feed-in. |
| Battery  | Usable capacity (kWh), SoC min/max %, max charge/discharge power (kW), round-trip efficiencies, **cycle cost in your currency / kWh of throughput** (≈ `battery_price / (cycles × usable_kWh × η_rt)`), optional **soft SoC health floor (%)** and **low-SoC dwell penalty (currency / kWh / h)** — see *Soft SoC health floor* below. |
| Solver   | Slot length (default 60 min), horizon (default 24 h), update interval (default 5 min), max grid import/export (kW), set-point write tolerance (W), **minimum sell price (currency/kWh; default 0)** — see *Minimum sell price* below. |
| Load forecast | Lookback days (default 7), optional cap (kW; 0 = no cap), weekday-aware mode (default off). Skipped at runtime when an external `load_forecast_entity` was set in the Entities step. |

Options flow re-exposes the **Battery + Solver + Load-forecast** knobs in a
single combined screen (pre-filled with current values), so cycle cost,
efficiencies, horizon, etc. can be tuned after install without re-adding the
integration. Entity selections stay locked to the install — change them by
removing & re-adding the integration.

> Currency note: the planner is currency-agnostic. All price-related fields
> (cycle cost in config, the cost / savings diagnostic sensors) carry the
> placeholder unit label `your_currency` — supply tariff sensors in whatever
> currency you use (EUR, USD, CZK, …) and the math comes out in that same
> unit. Just keep buy price, sell price, and cycle cost in **the same**
> currency.

### Expected attribute shapes
- **Hourly tariff sensors** — four shapes are auto-detected, tried in order:
  - `list[float]` of length 24 under the configured attribute name (default
    `today` / `tomorrow`; Nordpool/OTE legacy shape);
  - `dict[str, float]` under the configured attribute name, keyed by ISO 8601
    timestamps with timezone offset, e.g. `{"2026-05-01T06:00:00+02:00": 0.12}`;
  - **dict under any other attribute name** — if the configured attribute
    isn't found, the planner scans every other dict-valued attribute and
    picks the largest one whose keys parse as ISO timestamps. This means
    user-built template sensors can publish under whatever attribute name
    feels natural (`prices`, `raw_today`, …) without any planner config;
  - **top-level ISO-keyed attributes** — the entity's `attributes` dict itself
    is the price map, with each hour timestamp as its own attribute key (this
    is what `spot_hodinovy_tarif` and similar plugins do). Unrelated metadata
    keys are ignored.

  With any dict shape today's and tomorrow's hours may live on a single
  entity (e.g. one 48-entry `prices` dict) or be split across today/tomorrow
  entities — both work.

  **Staleness contract (dict shapes only):** if the *current* hour's key is
  missing, the planner refuses to run and records `last_error` rather than
  silently re-using yesterday's prices. Missing *future* hours simply
  truncate the planning horizon.
- **PV forecast** sensor must expose either a `wh_hours` dict
  (`forecast.solar` style: `{"<iso-hour>": Wh}`) or a `forecast` list of
  `{"datetime": "<iso>", "power_kw": <kW>}` entries.
- **Load forecast** is optional. When unset, a built-in forecaster derives a
  per-slot expected load from the recorder history of the configured load-power
  entity using the **median over the last `lookback_days` days at the same
  hour-of-day** (default `7`). Median naturally rejects one-off spikes (e.g.
  an EV charging session) without explicit detection. Optional knobs: `cap_kw`
  (hard ceiling) and `weekday_aware` (only days with the same weekday
  contribute; needs ~4 weeks of history to be useful). Setting an external
  `load_forecast_entity` disables the built-in forecaster (escape hatch).

## Active vs passive control
The planner only overrides the inverter when the LP wants to actively move
energy between the battery and the grid. Otherwise the set-point stays at
**0 W** and the Multiplus runs its own self-consumption logic (PV → load →
battery → grid surplus, gated by the feed-in switch).

| LP situation in slot 0 | Set-point written | Feed-in switch |
|---|---|---|
| Force-discharge (`p_dis > 0 ∧ p_sell > 0`) | `(p_buy − p_sell) · 1000` (negative) | `on` |
| Force-charge (`p_chg > 0 ∧ p_buy > 0`)     | `(p_buy − p_sell) · 1000` (positive) | `off` |
| Pure PV export, force-export toggle **on** (battery idle) | `(p_buy − p_sell) · 1000` (negative) | `on` |
| Force-hold-import (`p_buy > 0` with battery idle) | `(p_buy − p_sell) · 1000` (positive) | `off` |
| Anything else (PV deficit covered by battery, idle, pure export with toggle **off**) | `0` | `on` iff LP wants to sell |

Forcing a non-zero set-point speculatively is brittle: a PV undershoot would
make the inverter discharge the battery to defend a target it was never
asked to defend. Set-point = 0 hands those degrees of freedom back to the
inverter — but only when the LP and the inverter actually agree on what
"do nothing" means (see *Force-hold-import* below).

### Force-PV-export toggle
The default passive behaviour means morning PV ends up charging the battery
even when the LP would rather sell it (e.g. a high-price morning followed
by cheap noon recharge). Wire any `switch` / `input_boolean` / `binary_sensor`
to **Force PV export** in the options flow to opt into the third row above.

When the toggle is on the planner protects against forecast error by
clamping slot-0 PV to `min(forecast, trailing_avg)`, where the trailing
average is computed from the recorder over the configured update interval.
That way the LP can never speculate above measured production, and a cloud
that drops live PV below the forecast immediately reduces the export target
on the next planner cycle.

### Minimum sell price
A floor on the sell price (currency/kWh, default `0`) configurable in the
Solver options. Slots whose all-in sell price falls **strictly below** the
floor are treated as feed-in-disallowed for the purposes of the LP: the
optimizer pins `p_sell[t] = 0`, so it plans to keep that PV in the battery
for a better-priced slot (or curtails when the battery is full). When the
gate fires for slot 0 the feed-in switch is also turned off, which stops
the inverter's native logic from exporting at the floor price too. Useful
when the marginal sell revenue (e.g. 0.10 CZK/kWh) doesn't justify running
the inverter at full export power. Leave at `0` to disable the floor.

### Soft SoC health floor
The hard `SoC min` bound stops the LP at the inverter / chemistry safety
floor but says nothing about *dwelling* there. Without further input the
LP cheerfully drains to `SoC min` mid-afternoon and parks the battery at
the bottom for 18 hours when the next day's prices don't beat today's —
fine for revenue, bad for calendar aging (more so on NMC, mildly on LFP).

Two opt-in Battery knobs add a *soft* floor above `SoC min`:

| Knob | Default | Unit | Meaning |
|---|---|---|---|
| Soft SoC health floor | = `SoC min` (off) | % | SoC the LP is encouraged to stay above |
| Low-SoC dwell penalty | `0` (off) | currency / (kWh·h) | per-hour cost of each kWh below the floor |

The LP can still dip below the floor — that's the *soft* part — but only
when the slot's marginal economic gain (sell revenue, displaced buy)
beats the cumulative penalty over the rest of the horizon. A typical
configuration (`floor = 40 %`, `penalty = 0.5 CZK/(kWh·h)`) costs ~0.5
CZK/h per kWh of shortfall: enough to nudge the LP into recharging during
cheap hours, weak enough to yield to a real evening peak. Defaults make
the feature a regression no-op (no slack vars, no objective term).

### Force-hold-import
"Set-point = 0 means do nothing" only works when the LP and the inverter
agree on what "do nothing" is. They disagree in two situations where the
LP wants to cover the load purely from the grid with the battery idle:

- The soft health floor (above) makes further discharge expensive enough
  that grid import wins, but the inverter's native EMS doesn't know
  about that penalty and would drain the battery anyway.
- `SoC min` is set above the inverter's BMS floor (e.g. 20 % reserve on
  a battery the BMS allows down to 10 %); the LP respects `SoC min`,
  the EMS doesn't.

Both are silent plan violations: passive set-point `0` looks fine but the
battery quietly empties. The fourth row in the table above fixes it: when
the LP plans `p_buy > 0` with the battery idle, the planner pins the grid
set-point to that planned import (positive, typically tracking load).
Always-on, no toggle — when the predicate doesn't fire (e.g. battery
already at the hard floor with no arbitrage to defend) the forced
positive set-point produces the same physical behaviour as `0`.

## Diagnostic sensors
Up to six sensors are created so the plan is visible in HA dashboards:

| Entity | Meaning |
|---|---|
| `sensor.pv_optimizer_planned_grid_setpoint` | Current-slot set-point in W (sign = Victron convention). |
| `sensor.pv_optimizer_planned_feed_in`       | `on`/`off` for the upcoming slot. |
| `sensor.pv_optimizer_expected_cost_horizon` | Total expected cost over the horizon (`your_currency`). |
| `sensor.pv_optimizer_savings_vs_passive`    | Savings vs. doing nothing (battery idle), in `your_currency`. |
| `sensor.pv_optimizer_plan`                  | State = next-slot set-point in kW; per-slot plan + battery params in attributes (see below). |
| `sensor.pv_optimizer_load_forecast`         | Built-in load forecaster's next-slot kW; full `kw_per_slot` and `days_used_per_slot` in attributes. Only created when the built-in forecaster is active (no `load_forecast_entity` configured). |

`sensor.pv_optimizer_plan` attributes:

| Attribute | Meaning |
|---|---|
| `slots` | Per-slot list with ISO-tagged `start`, `duration_h`, `p_buy_kw`, `p_sell_kw`, `p_chg_kw`, `p_dis_kw`, `soc_start_kwh`, `soc_physical_kwh`, `setpoint_w`. |
| `capacity_kwh`, `soc_min_kwh`, `soc_max_kwh`, `soc_health_kwh` | Battery params, exposed so dashboards can compute SoC % and draw reserve / ceiling / health-floor lines without hardcoding. |
| `low_soc_penalty_per_kwh_h` | Active dwell-penalty rate (currency/(kWh·h); `0` = disabled). |
| `force_pv_export_enabled` | Mirrors the toggle's last-read value (`null` if unset, `true`/`false` otherwise). |
| `min_sell_price_per_kwh` | Active sell-price floor (currency/kWh; `0` = disabled). |
| `horizon_slots`, `status`, `solve_time_s`, `error` | Solver diagnostics. |

Each slot carries two SoC tracks: `soc_start_kwh` is the LP's bookkeeping
(stays flat across passive PV-surplus slots because the LP curtails what it
doesn't actively transfer); `soc_physical_kwh` is a planner-layer projection
of what the inverter will actually reach, simulating self-consumption in
passive slots and following the LP exactly in active / force-export slots.
Plot both to see where the two diverge.

`setpoint_w` is the grid set-point the planner *would* write for that slot
under the rules in *Active vs passive control* (positive = import,
negative = export; `0` = passive / hand control to the EMS). For the
current slot it matches `sensor.pv_optimizer_planned_grid_setpoint`; the
rest of the horizon is what the planner would write next if the LP plan
holds. Use it directly as the chart series instead of re-implementing the
predicates client-side.

## Development & tests
```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

The test suite runs in a plain virtualenv, **without** installing Home
Assistant. It covers:
- the LP formulation in `tests/test_optimizer.py`
- the read→solve→apply planner pipeline in `tests/test_planner.py`,
  including legacy list, timestamp-keyed dict, alternate wrapper-attribute
  auto-discovery, and top-level ISO-keyed price formats, plus the
  stale-data hard-failure path.
- the median-over-N-days load forecaster in `tests/test_load_forecaster.py`,
  including spike rejection, partial-history fallback, time-weighted bucket
  averaging, weekday filtering, and result caching.

The HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`) are
deliberately thin shims over the pure layer; they are exercised inside a
running HA instance, not in this repository's CI.

## License
MIT.
