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
- **Hourly tariff sensors** (e.g. Nordpool, OTE) must expose a `today` list
  of 24 floats; an optional `tomorrow` list is used after midnight.
- **PV forecast** sensor must expose either a `wh_hours` dict
  (`forecast.solar` style: `{"<iso-hour>": Wh}`) or a `forecast` list of
  `{"datetime": "<iso>", "power_kw": <kW>}` entries.
- **Load forecast** is optional; with no forecast, the planner uses the
  current load reading as a flat estimate.

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
- the LP formulation (10 scenarios in `tests/test_optimizer.py`)
- the read→solve→apply planner pipeline (4 scenarios in
  `tests/test_planner.py`).

The HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`) are
deliberately thin shims over the pure layer; they are exercised inside a
running HA instance, not in this repository's CI.

## License
MIT.
