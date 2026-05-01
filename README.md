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
  models.py        # Pure dataclasses (BatteryParams, OptimizerInputs, ...)
  optimizer.py     # LP formulation + solve()
  planner.py       # Pure read→solve→apply pipeline (testable, no HA)
  coordinator.py   # HA DataUpdateCoordinator shim around Planner
  config_flow.py   # Multi-step UI configuration
  sensor.py        # Diagnostic sensors
  const.py         # Configuration keys + defaults
  manifest.json    # HA manifest
tests/
  test_optimizer.py
  test_planner.py
PRD.md  TASKS.md
```

## Installation
1. Copy `custom_components/pv_optimizer/` into your Home Assistant
   `config/custom_components/` directory (HACS-compatible repo layout).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "PV Optimizer"**.

## Configuration
Three steps in the UI:

| Step | What you provide |
|---|---|
| Entities | Sensor IDs for load, PV, grid, SoC, buy/sell prices (today + optional tomorrow), PV forecast, optional load forecast and feed-in override; plus the `number` for the Victron grid set-point and the `switch` controlling feed-in. |
| Battery  | Usable capacity (kWh), SoC min/max %, max charge/discharge power (kW), round-trip efficiencies, **cycle cost in EUR/kWh of throughput** (≈ `battery_price / (cycles × usable_kWh × η_rt)`). |
| Solver   | Slot length (default 60 min), horizon (default 24 h), update interval (default 5 min), max grid import/export (kW), set-point write tolerance (W). |

### Expected attribute shapes
- **Hourly tariff sensors** — three shapes are auto-detected:
  - `list[float]` of length 24 under the configured attribute name (default
    `today` / `tomorrow`; Nordpool/OTE legacy shape);
  - `dict[str, float]` under the configured attribute name, keyed by ISO 8601
    timestamps with timezone offset, e.g. `{"2026-05-01T06:00:00+02:00": 0.12}`;
  - **top-level ISO-keyed attributes** — the entity's `attributes` dict itself
    is the price map, with each hour timestamp as its own attribute key (this
    is what `spot_hodinovy_tarif` and similar plugins do). Unrelated metadata
    keys are ignored; the configured attribute name doesn't have to match.

  With either dict shape today's and tomorrow's hours may live on a single
  entity or be split across today/tomorrow entities — both work.

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
Five sensors are created so the plan is visible in HA dashboards:

| Entity | Meaning |
|---|---|
| `sensor.pv_optimizer_planned_grid_setpoint` | Current-slot set-point in W (sign = Victron convention). |
| `sensor.pv_optimizer_planned_feed_in`       | `on`/`off` for the upcoming slot. |
| `sensor.pv_optimizer_expected_cost_horizon` | Total expected cost over the horizon (EUR). |
| `sensor.pv_optimizer_savings_vs_passive`    | Savings vs. doing nothing (battery idle). |
| `sensor.pv_optimizer_plan`                  | State = next-slot set-point; full per-slot plan in attributes. |

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
  including legacy list and timestamp-keyed dict price formats plus the
  stale-data hard-failure path.

The HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`) are
deliberately thin shims over the pure layer; they are exercised inside a
running HA instance, not in this repository's CI.

## License
MIT.
