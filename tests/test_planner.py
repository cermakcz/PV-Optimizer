"""Unit tests for the planner (pure layer driving the optimizer + side effects)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.pv_optimizer.models import BatteryParams
from custom_components.pv_optimizer.planner import (
    Planner,
    PlannerConfig,
    StateView,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeReader:
    def __init__(self, states: dict[str, StateView]) -> None:
        self._states = states

    def get(self, entity_id: str) -> StateView | None:
        return self._states.get(entity_id)


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.calls.append((domain, service, data))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 1, 12, 0, 0)  # noon, naive UTC
ENTITY_IDS = dict(
    load_power_entity="sensor.load_w",
    pv_power_entity="sensor.pv_w",
    grid_power_entity="sensor.grid_w",
    battery_soc_entity="sensor.soc_pct",
    buy_price_today_entity="sensor.buy",
    sell_price_today_entity="sensor.sell",
    pv_forecast_entity="sensor.pv_forecast",
    grid_setpoint_entity="number.grid_setpoint",
    feedin_switch_entity="switch.feedin",
)


def _config(**overrides) -> PlannerConfig:
    bat = BatteryParams(
        capacity_kwh=10.0, soc_min_kwh=1.0, soc_max_kwh=9.0,
        p_chg_max_kw=5.0, p_dis_max_kw=5.0,
        eta_chg=1.0, eta_dis=1.0, cycle_cost_eur_per_kwh=0.0,
    )
    return PlannerConfig(
        **ENTITY_IDS,
        battery=bat,
        p_grid_imp_max_kw=10.0,
        p_grid_exp_max_kw=10.0,
        slot_minutes=60,
        horizon_hours=4,  # short horizon for fast tests
        **overrides,
    )


def _states(*, soc_pct=50.0, load_w=1000.0,
            buy_today=None, sell_today=None,
            pv_forecast=None, missing: tuple[str, ...] = ()) -> dict[str, StateView]:
    if buy_today is None:
        buy_today = [0.30] * 24
    if sell_today is None:
        sell_today = [0.10] * 24
    if pv_forecast is None:
        # 4-hour horizon starting at noon -> hours 12..15 in wh_hours.
        pv_forecast = {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }
    raw = {
        "sensor.soc_pct": StateView(state=str(soc_pct), attributes={}),
        "sensor.load_w": StateView(state=str(load_w), attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.30", attributes={"today": buy_today}),
        "sensor.sell": StateView(state="0.10", attributes={"today": sell_today}),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": pv_forecast}),
    }
    for k in missing:
        raw.pop(k, None)
    return raw


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_step_happy_path_writes_setpoint_and_feedin() -> None:
    states = _states()  # constant 0.30 buy, 0.10 sell, no PV, 1 kW load
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None and cycle.result.status == "Optimal"
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1.0)  # buy 1 kW
    assert cycle.applied_feedin is False
    # number.set_value + switch.turn_off both called.
    services = [(d, s) for d, s, _ in caller.calls]
    assert ("number", "set_value") in services
    assert ("switch", "turn_off") in services


def test_missing_required_sensor_records_error_and_no_writes() -> None:
    states = _states(missing=("sensor.load_w",))
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is not None and "load_w" in cycle.error
    assert cycle.result is None
    assert caller.calls == []


def test_setpoint_within_tolerance_not_rewritten() -> None:
    states = _states()
    caller = FakeCaller()
    cfg = _config(setpoint_tolerance_w=200.0)
    planner = Planner(cfg, FakeReader(states), caller)

    planner.step(NOW)
    n_after_first = len(caller.calls)
    # Same state -> same plan -> setpoint unchanged within tolerance.
    planner.step(NOW + timedelta(minutes=5))
    n_after_second = len(caller.calls)
    # Only feedin may be re-applied (it isn't, since unchanged); definitely no second number.set_value.
    assert all(s != "set_value" for _, s, _ in caller.calls[n_after_first:])
    assert n_after_second >= n_after_first  # sanity


def test_pv_surplus_triggers_feedin_on() -> None:
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}  # 5 kWh per slot -> 5 kW
    states = _states(pv_forecast=pv_forecast)
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.applied_feedin is True
    assert cycle.applied_setpoint_w < 0  # net export
    assert ("switch", "turn_on") in [(d, s) for d, s, _ in caller.calls]
