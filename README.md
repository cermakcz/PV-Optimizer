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
3. Solves it with PuLP+CBC, minimising
   `Σ (price_buy·p_buy − price_sell·p_sell + cycle_cost·throughput)·Δt`
   subject to power balance, battery dynamics, SoC bounds, charge/discharge
   limits, and grid import/export limits. Battery wear is included via a
   per-kWh **cycle (amortization) cost**.
4. Writes the optimal current-slot grid set-point (W) and feed-in switch
   state to the configured Victron-backed `number`/`switch` entities.

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
| Entities | Sensor IDs for load, PV, grid, SoC, buy/sell prices (today + optional tomorrow), PV forecast, optional load forecast and feed-in override; plus the `number` for the Victron grid set-point and the `switch` controlling feed-in. |
| Battery  | Usable capacity (kWh), SoC min/max %, max charge/discharge power (kW), round-trip efficiencies, **cycle cost in your currency / kWh of throughput** (≈ `battery_price / (cycles × usable_kWh × η_rt)`). |
| Solver   | Slot length (default 60 min), horizon (default 24 h), update interval (default 5 min), max grid import/export (kW), set-point write tolerance (W). |
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

## Diagnostic sensors
Up to six sensors are created so the plan is visible in HA dashboards:

| Entity | Meaning |
|---|---|
| `sensor.pv_optimizer_planned_grid_setpoint` | Current-slot set-point in W (sign = Victron convention). |
| `sensor.pv_optimizer_planned_feed_in`       | `on`/`off` for the upcoming slot. |
| `sensor.pv_optimizer_expected_cost_horizon` | Total expected cost over the horizon (`your_currency`). |
| `sensor.pv_optimizer_savings_vs_passive`    | Savings vs. doing nothing (battery idle), in `your_currency`. |
| `sensor.pv_optimizer_plan`                  | State = next-slot set-point; full per-slot plan in attributes. |
| `sensor.pv_optimizer_load_forecast`         | Built-in load forecaster's next-slot kW; full `kw_per_slot` and `days_used_per_slot` in attributes. Only created when the built-in forecaster is active (no `load_forecast_entity` configured). |

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
