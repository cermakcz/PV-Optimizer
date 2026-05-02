"""Unit tests for the planner (pure layer driving the optimizer + side effects)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.pv_optimizer.load_forecaster import (
    LoadForecaster,
    LoadForecasterConfig,
)
from custom_components.pv_optimizer.models import BatteryParams
from custom_components.pv_optimizer.planner import (
    Planner,
    PlannerConfig,
    StateView,
    naive_utc_to_iso,
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
            buy=None, sell=None, buy_attr="today", sell_attr="today",
            pv_forecast=None, missing: tuple[str, ...] = ()) -> dict[str, StateView]:
    """Build a fake state map. ``buy``/``sell`` may be a list[24] or any other
    shape (dict) that the planner accepts under the given attribute name."""
    if buy is None:
        buy = [0.30] * 24
    if sell is None:
        sell = [0.10] * 24
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
        "sensor.buy": StateView(state="0.30", attributes={buy_attr: buy}),
        "sensor.sell": StateView(state="0.10", attributes={sell_attr: sell}),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": pv_forecast}),
    }
    for k in missing:
        raw.pop(k, None)
    return raw


def _prague_iso(utc_dt: datetime) -> str:
    """Format a naive-UTC datetime as a Prague-local ISO 8601 string (+02:00 in May).

    The user's tariff plugin emits timestamps in local time with explicit offset.
    """
    local = utc_dt + timedelta(hours=2)
    return local.strftime("%Y-%m-%dT%H:00:00+02:00")


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



# ---------------------------------------------------------------------------
# Dict-shaped (timestamp-keyed) price entity
# ---------------------------------------------------------------------------


def test_dict_price_format_happy_path() -> None:
    # User's tariff plugin shape: {"<iso-with-+02:00>": price}. Provide today
    # only (24 entries starting at Prague midnight, which is 22:00 UTC the
    # previous day). The 4-hour planning horizon at noon UTC = 14:00 local
    # falls comfortably inside today's data.
    today_local_midnight_utc = datetime(2026, 4, 30, 22, 0)  # 2026-05-01 00:00 +02:00
    buy_dict = {
        _prague_iso(today_local_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    sell_dict = {k: 0.10 for k in buy_dict}
    states = _states(buy=buy_dict, sell=sell_dict, buy_attr="prices", sell_attr="prices")
    caller = FakeCaller()
    cfg = _config()
    # Tell the planner which attribute name our integration uses.
    cfg = _config(price_today_attr="prices")
    planner = Planner(cfg, FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4  # full configured horizon used
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1.0)


def test_dict_price_format_stale_now_missing_raises() -> None:
    # Dict only contains hours from yesterday -> current slot key absent.
    yesterday_midnight_utc = datetime(2026, 4, 29, 22, 0)
    buy_dict = {
        _prague_iso(yesterday_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    sell_dict = {k: 0.10 for k in buy_dict}
    states = _states(buy=buy_dict, sell=sell_dict, buy_attr="prices", sell_attr="prices")
    caller = FakeCaller()
    planner = Planner(_config(price_today_attr="prices"), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.result is None
    assert cycle.error is not None and "stale" in cycle.error.lower()
    assert caller.calls == []  # no setpoint written when planning fails


def test_dict_price_format_partial_horizon_truncates() -> None:
    # Provide only the current hour and the next one (= 2 slots) out of the
    # configured 4-hour horizon. Planner must succeed with a 2-slot plan.
    buy_dict = {
        _prague_iso(NOW + timedelta(hours=h)): 0.30
        for h in range(2)
    }
    sell_dict = {k: 0.10 for k in buy_dict}
    states = _states(buy=buy_dict, sell=sell_dict, buy_attr="prices", sell_attr="prices")
    caller = FakeCaller()
    planner = Planner(_config(price_today_attr="prices"), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 2  # truncated from 4 to available 2



def test_dict_price_format_autodiscovers_alternate_attribute_name() -> None:
    # Realistic scenario: a user-built template publishes the price map under
    # ``prices`` while the planner config still uses the Nordpool default
    # ``today``. The auto-discovery step should pick the iso-keyed dict
    # regardless of which key wraps it — no config change required.
    today_local_midnight_utc = datetime(2026, 4, 30, 22, 0)
    iso_buy = {
        _prague_iso(today_local_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    iso_sell = {k: 0.10 for k in iso_buy}
    buy_attrs = {"prices": iso_buy, "hours_published": 24, "source": "x"}
    sell_attrs = {"prices": iso_sell, "hours_published": 24}

    raw = {
        "sensor.soc_pct": StateView(state="50", attributes={}),
        "sensor.load_w": StateView(state="1000", attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.30", attributes=buy_attrs),
        "sensor.sell": StateView(state="0.10", attributes=sell_attrs),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }}),
    }
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(raw), caller)  # default attr="today"

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4


def test_dict_price_format_top_level_iso_attributes() -> None:
    # Some integrations (e.g. spot_hodinovy_tarif) put the hour timestamps
    # *directly* on the entity's attributes dict — there is no wrapping
    # attribute. The planner should auto-detect this shape, ignore unrelated
    # metadata keys, and use whichever attr name the user configured (it
    # simply won't be found, triggering the fallback scan).
    today_local_midnight_utc = datetime(2026, 4, 30, 22, 0)  # 2026-05-01 +02:00
    iso_buy = {
        _prague_iso(today_local_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    iso_sell = {k: 0.10 for k in iso_buy}
    # Mix in non-price attributes to prove the lenient scan ignores them.
    buy_attrs = {**iso_buy, "unit_of_measurement": "EUR/kWh", "friendly_name": "Buy"}
    sell_attrs = {**iso_sell, "unit_of_measurement": "EUR/kWh"}

    raw = {
        "sensor.soc_pct": StateView(state="50", attributes={}),
        "sensor.load_w": StateView(state="1000", attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.30", attributes=buy_attrs),
        "sensor.sell": StateView(state="0.10", attributes=sell_attrs),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }}),
    }
    caller = FakeCaller()
    # The configured attr name doesn't exist on the entity — the fallback
    # scan over the whole attributes dict is what makes this work.
    planner = Planner(_config(price_today_attr="prices"), FakeReader(raw), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1.0)



class _FakeHistory:
    """Step-wise history with a single constant value, density 15 minutes."""

    def __init__(self, value_kw: float) -> None:
        self.value_kw = value_kw

    def get_history(self, entity_id: str, start: datetime, end: datetime
                    ) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        cursor = start
        while cursor < end:
            out.append((cursor, self.value_kw))
            cursor += timedelta(minutes=15)
        return out


def test_built_in_forecaster_drives_load_when_no_entity_set() -> None:
    # Sell price > buy price → optimizer wants to import as much as the load
    # demands. With the forecaster reporting 2.5 kW (and zero PV) the first
    # slot's setpoint must reflect that — proving the forecaster, not the
    # 1 kW current-load fallback, is feeding the planner.
    states = _states(buy=[0.10] * 24, sell=[0.05] * 24, load_w=1000.0)
    caller = FakeCaller()
    forecaster = LoadForecaster(
        LoadForecasterConfig(entity_id="sensor.load_w"),
        _FakeHistory(value_kw=2.5),
    )
    planner = Planner(_config(), FakeReader(states), caller, load_forecaster=forecaster)

    cycle = planner.step(NOW)

    assert cycle.error is None and cycle.result is not None
    assert cycle.applied_setpoint_w == pytest.approx(2500.0, abs=1.0)


def test_built_in_forecaster_falls_back_when_no_history() -> None:
    # No history at all → forecaster returns 0 days_used, planner falls back
    # to the current-load reading (1 kW).
    states = _states(buy=[0.10] * 24, sell=[0.05] * 24, load_w=1000.0)
    caller = FakeCaller()

    class _Empty:
        def get_history(self, *_a, **_k):
            return []

    forecaster = LoadForecaster(
        LoadForecasterConfig(entity_id="sensor.load_w"), _Empty(),
    )
    planner = Planner(_config(), FakeReader(states), caller, load_forecaster=forecaster)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1.0)



# ---------------------------------------------------------------------------
# Solver-failure surfacing. A crash inside the LP solver (e.g. PuLP raising
# PulpError, or CBC exec'ing into FileNotFoundError) must be caught and
# recorded as ``cycle.error`` so the coordinator stays green and the user
# sees the problem on the diagnostic sensor instead of in the HA log as an
# unhandled exception every 5 minutes.
# ---------------------------------------------------------------------------


def test_planner_surfaces_solver_oserror_as_cycle_error(monkeypatch) -> None:
    import pulp

    from custom_components.pv_optimizer import optimizer as optimizer_mod

    class _BoomSolver:
        def actualSolve(self, *_a, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "/missing/cbc")

    optimizer_mod._SOLVER_FACTORY = None
    monkeypatch.setattr(optimizer_mod, "_make_solver", lambda: _BoomSolver())

    states = _states()
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.result is None
    assert cycle.error is not None
    assert "subprocess failed" in cycle.error
    assert caller.calls == []  # no setpoint write on failure


def test_planner_surfaces_solver_pulperror_as_cycle_error(monkeypatch) -> None:
    import pulp

    from custom_components.pv_optimizer import optimizer as optimizer_mod

    class _BoomSolver:
        def actualSolve(self, *_a, **_kw):
            raise pulp.PulpError("synthetic")

    optimizer_mod._SOLVER_FACTORY = None
    monkeypatch.setattr(optimizer_mod, "_make_solver", lambda: _BoomSolver())

    states = _states()
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.result is None
    assert cycle.error is not None and "LP solver failed" in cycle.error



# ---------------------------------------------------------------------------
# Timestamp serialisation. Slot/forecast keys are naive-UTC internally;
# ``naive_utc_to_iso`` must emit explicit ``+00:00`` so apexcharts-card and
# similar frontends position the series at the correct local-time x-coord.
# ---------------------------------------------------------------------------


def test_naive_utc_to_iso_tags_naive_input_with_utc_offset() -> None:
    iso = naive_utc_to_iso(datetime(2026, 5, 2, 15, 0))
    assert iso == "2026-05-02T15:00:00+00:00"


def test_naive_utc_to_iso_normalises_aware_input_to_utc() -> None:
    prague = timezone(timedelta(hours=2))
    iso = naive_utc_to_iso(datetime(2026, 5, 2, 17, 0, tzinfo=prague))
    assert iso == "2026-05-02T15:00:00+00:00"
