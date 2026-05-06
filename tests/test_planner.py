"""Unit tests for the planner (pure layer driving the optimizer + side effects)."""
from __future__ import annotations

from dataclasses import replace
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


class FakeLiveAverager:
    """Trivial LiveAverager that returns a configured kW value (or None)."""

    def __init__(self, value: float | None) -> None:
        self.value = value
        self.calls: list[tuple[str, datetime, datetime]] = []

    def average_kw(self, entity_id: str, since: datetime, until: datetime
                   ) -> float | None:
        self.calls.append((entity_id, since, until))
        return self.value


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
        eta_chg=1.0, eta_dis=1.0, cycle_cost_per_kwh=0.0,
    )
    return PlannerConfig(
        battery=bat,
        p_grid_imp_max_kw=10.0,
        p_grid_exp_max_kw=10.0,
        slot_minutes=60,
        horizon_hours=4,  # short horizon for fast tests
        **{**ENTITY_IDS, **overrides},
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
    # LP plans to import 1 kW to cover the load with the battery idle.
    # Force-hold-import (PRD §8.6) pins the grid set-point to the planned
    # import so the inverter doesn't silently drain the battery instead.
    first = cycle.result.slots[0]
    assert first.p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert first.p_chg_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)
    assert cycle.applied_feedin is False
    # number.set_value(0) still issued on the first cycle (no prior state),
    # and the feed-in switch is explicitly turned off.
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


def test_pv_surplus_triggers_feedin_on_without_forced_setpoint() -> None:
    # 5 kW PV against 1 kW load = 4 kW surplus. The LP wants to export it
    # but doesn't need the battery to do so — setpoint must stay at 0
    # (passive surplus export via the Multiplus's own logic), with feed-in on.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 0  # LP exports
    assert first.p_dis_kw == pytest.approx(0.0, abs=1e-3)  # but not from battery
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_feedin is True
    assert ("switch", "turn_on") in [(d, s) for d, s, _ in caller.calls]


def test_physical_soc_projection_charges_in_passive_surplus() -> None:
    # Same passive-surplus scenario as above. The LP's bookkeeping leaves
    # soc_start_kwh flat (no p_chg_kw because the export is profitable enough
    # without cycling), but the inverter physically absorbs the 4 kW surplus
    # into the battery — soc_physical_kwh should reflect that.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)
    slots = cycle.result.slots

    # Initial SoC = 50% of 10 kWh = 5 kWh; soc_max = 9 kWh.
    assert slots[0].soc_physical_kwh == pytest.approx(5.0, abs=1e-3)
    # 4 kW surplus * 1 h * eta_chg=1.0 = 4 kWh charged within headroom (4 kWh).
    assert slots[1].soc_physical_kwh == pytest.approx(9.0, abs=1e-3)
    # Battery full; subsequent slots stay pinned at soc_max.
    for s in slots[1:]:
        assert s.soc_physical_kwh == pytest.approx(9.0, abs=1e-3)
    # LP bookkeeping never cycled the battery — projection diverges from it.
    assert slots[1].soc_start_kwh == pytest.approx(5.0, abs=1e-3)


def test_physical_soc_projection_follows_lp_when_force_exporting() -> None:
    # PV surplus with force-export enabled: the inverter pushes the surplus
    # to the grid instead of charging the battery, so the projection must
    # stay flat (mirroring the LP's idle-battery decision) rather than
    # ramping up via the passive self-consumption rule.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)
    slots = cycle.result.slots

    # LP keeps battery idle (p_chg = p_dis = 0) and sells the surplus; the
    # projection must defer to that instead of inferring a passive charge.
    for s in slots:
        assert s.p_chg_kw == pytest.approx(0.0, abs=1e-3)
        assert s.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    # Initial SoC = 5 kWh; with no battery action the projection stays at 5.
    for s in slots:
        assert s.soc_physical_kwh == pytest.approx(5.0, abs=1e-3)


def test_physical_soc_projection_follows_lp_when_forced() -> None:
    # Same setup as ``test_force_charge_writes_positive_setpoint``: cheap
    # noon buy, expensive afterwards, no profitable export. The LP forces
    # a battery charge from the grid in slot 0; the projection must defer
    # to that LP decision rather than re-running self-consumption logic.
    states = _states(
        load_w=500.0,
        buy=_hourly(value_at_noon=0.01, value_elsewhere=5.0),
        sell=[0.0] * 24,
    )
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)
    slots = cycle.result.slots

    first = slots[0]
    assert first.p_buy_kw > 1e-3 and first.p_chg_kw > 1e-3  # force-charge triggered
    # Under force-charge the projection mirrors the LP exactly.
    assert first.soc_physical_kwh == pytest.approx(first.soc_start_kwh, abs=1e-3)
    # Next slot's projection equals LP's next soc_start (eta=1.0 in tests).
    assert slots[1].soc_physical_kwh == pytest.approx(slots[1].soc_start_kwh, abs=1e-3)



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
    # Load coverage with battery idle — force-hold-import (PRD §8.6) pins
    # the set-point to the planned import so the EMS doesn't drain the
    # battery instead.
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)


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
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)
    # Force-hold-import (PRD §8.6): pure-load coverage gets a positive
    # set-point so the EMS doesn't drain the battery instead.
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)


def test_pv_forecast_solcast_detailed_hourly_shape() -> None:
    # Solcast HACS integration exposes the forecast as ``detailedHourly``:
    # a list of {period_start: <iso>, pv_estimate: <kW>} entries. The
    # planner should accept this shape natively.
    detailed_hourly = [
        {"period_start": (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00+00:00"),
         "pv_estimate": 5.0,
         "pv_estimate10": 3.5,
         "pv_estimate90": 6.5}
        for h in range(4)
    ]
    states = _states()
    states["sensor.pv_forecast"] = StateView(
        state="0", attributes={"detailedHourly": detailed_hourly})
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    # 5 kW PV - 1 kW load = 4 kW surplus → LP exports, feed-in on, passive setpoint.
    first = cycle.result.slots[0]
    assert first.p_sell_kw == pytest.approx(4.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_feedin is True


def test_pv_forecast_detailed_hourly_accepts_datetime_period_start() -> None:
    # The Solcast HACS integration stores ``period_start`` as a tz-aware
    # ``datetime`` (not an ISO string). _parse_iso must accept that shape
    # so users can point ``pv_forecast_entity`` at the Solcast sensor
    # directly without a template-sensor coercion step.
    detailed_hourly = [
        {"period_start": (NOW + timedelta(hours=h)).replace(tzinfo=timezone.utc),
         "pv_estimate": 5.0}
        for h in range(4)
    ]
    states = _states()
    states["sensor.pv_forecast"] = StateView(
        state="0", attributes={"detailedHourly": detailed_hourly})
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result.slots[0].p_sell_kw == pytest.approx(4.0, abs=1e-3)


def test_pv_forecast_multiple_entities_merge_today_and_tomorrow() -> None:
    # Solcast splits the forecast across a today and a tomorrow sensor;
    # passing both as a list to ``pv_forecast_entity`` must merge them so
    # the LP sees a continuous horizon spanning the two days.
    today = [
        {"period_start": (NOW + timedelta(hours=h)).replace(tzinfo=timezone.utc),
         "pv_estimate": 5.0}
        for h in range(2)  # NOW=12:00 -> today covers slots 0-1 (12:00, 13:00).
    ]
    tomorrow = [
        {"period_start": (NOW + timedelta(hours=h)).replace(tzinfo=timezone.utc),
         "pv_estimate": 5.0}
        for h in range(2, 4)  # slots 2-3 (14:00, 15:00) supplied by tomorrow sensor.
    ]
    states = _states()
    states["sensor.pv_today"] = StateView(
        state="0", attributes={"detailedHourly": today})
    states["sensor.pv_tomorrow"] = StateView(
        state="0", attributes={"detailedHourly": tomorrow})
    cfg = _config(pv_forecast_entity=["sensor.pv_today", "sensor.pv_tomorrow"])
    planner = Planner(cfg, FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)

    assert cycle.error is None
    # All four slots see 4 kW surplus (5 PV - 1 load) once the merged map
    # covers the entire horizon. If the merge weren't happening, slots 2-3
    # would fall back to 0 PV and produce a 1 kW import instead.
    for s in cycle.result.slots:
        assert s.p_sell_kw == pytest.approx(4.0, abs=1e-3)


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
    # Forecaster reports 2.5 kW (vs the 1 kW current-load fallback). With
    # battery idle this is force-hold-import (PRD §8.6) — the LP's
    # forecaster-driven import target is mirrored 1:1 in the set-point.
    states = _states(buy=[0.10] * 24, sell=[0.05] * 24, load_w=1000.0)
    caller = FakeCaller()
    forecaster = LoadForecaster(
        LoadForecasterConfig(entity_id="sensor.load_w"),
        _FakeHistory(value_kw=2.5),
    )
    planner = Planner(_config(), FakeReader(states), caller, load_forecaster=forecaster)

    cycle = planner.step(NOW)

    assert cycle.error is None and cycle.result is not None
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(2.5, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(2500.0, abs=1e-3)


def test_built_in_forecaster_falls_back_when_no_history() -> None:
    # No history at all → forecaster returns 0 days_used, planner falls back
    # to the current-load reading (1 kW). Force-hold-import pins set-point
    # to that import.
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
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)



# ---------------------------------------------------------------------------
# Active modes. The setpoint should be a non-zero override only when the LP
# wants to move energy *between the battery and the grid*. Force-discharge
# (battery → grid arbitrage) gets a negative setpoint; force-charge (grid →
# battery, e.g. cheap-hour buying) gets a positive one.
# ---------------------------------------------------------------------------


def _hourly(value_at_noon: float, value_elsewhere: float) -> list[float]:
    """24-hour price array with a distinct value at hour 12 (= NOW's slot)."""
    out = [value_elsewhere] * 24
    out[12] = value_at_noon
    return out


def test_force_discharge_writes_negative_setpoint() -> None:
    # Slot 0 (hour 12) has a profitable sell price; the next 3 hours offer
    # cheap re-charge. The LP should drain the battery in slot 0 and refill
    # over the rest of the horizon — that requires *actively forcing* the
    # export, so the setpoint must go negative.
    # Slot-0 buy is high so the LP can't run a pure import→export arbitrage;
    # the only way to capture the slot-0 sell price is from the battery.
    states = _states(
        load_w=500.0,
        buy=_hourly(value_at_noon=5.0, value_elsewhere=0.01),
        sell=_hourly(value_at_noon=1.0, value_elsewhere=0.0),
    )
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    first = cycle.result.slots[0]
    assert first.p_dis_kw > 1e-3 and first.p_sell_kw > 1e-3   # active discharge to grid
    assert first.p_buy_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w < -100.0                  # negative, well past dead-band
    assert cycle.applied_setpoint_w == pytest.approx(
        (first.p_buy_kw - first.p_sell_kw) * 1000.0, abs=1e-3,
    )
    assert cycle.applied_feedin is True


def test_force_charge_writes_positive_setpoint() -> None:
    # Slot 0 (hour 12) has effectively free buy; the next 3 hours are very
    # expensive. The LP should fill the battery from the grid now and
    # discharge it to cover load later — that requires *actively forcing*
    # extra import beyond the load, so the setpoint must go positive.
    states = _states(
        load_w=500.0,
        buy=_hourly(value_at_noon=0.01, value_elsewhere=5.0),
        sell=[0.0] * 24,                      # no profitable export anywhere
    )
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    first = cycle.result.slots[0]
    assert first.p_chg_kw > 1e-3 and first.p_buy_kw > 1e-3    # active charge from grid
    assert first.p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w > 100.0                   # positive, well past dead-band
    assert cycle.applied_setpoint_w == pytest.approx(
        first.p_buy_kw * 1000.0, abs=1e-3,                    # p_sell == 0 here
    )
    assert cycle.applied_feedin is False


# ---------------------------------------------------------------------------
# Force-PV-export toggle. With the toggle on, a pure PV-surplus slot (battery
# idle, no LP-driven battery transfer) flips from passive (setpoint = 0,
# inverter prioritises self-consumption -> battery) to active (negative
# setpoint, inverter exports the surplus to the grid). This is only safe
# because slot-0 PV is clamped to ``min(forecast, live trailing avg)`` so
# the LP can't speculate above measured production.
# ---------------------------------------------------------------------------


def test_force_pv_export_off_keeps_passive_setpoint() -> None:
    # 5 kW PV vs 1 kW load surplus; toggle absent -> passive (setpoint = 0),
    # matching the existing self-consumption behaviour.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 1e-3 and first.p_chg_kw < 1e-3
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.force_pv_export_enabled is False


def test_force_pv_export_on_writes_negative_setpoint() -> None:
    # Same surplus scenario, toggle on -> active export. Setpoint mirrors the
    # LP's net grid flow (p_buy - p_sell) so the inverter pushes PV to the
    # grid instead of charging the battery.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 1e-3 and first.p_chg_kw < 1e-3 and first.p_dis_kw < 1e-3
    assert cycle.applied_setpoint_w == pytest.approx(
        (first.p_buy_kw - first.p_sell_kw) * 1000.0, abs=1e-3,
    )
    assert cycle.applied_setpoint_w < -100.0
    assert cycle.force_pv_export_enabled is True


def test_force_pv_export_on_inactive_when_lp_doesnt_export() -> None:
    # Toggle on but the LP has no surplus to sell (no PV, 1 kW load). The
    # force-export branch must remain dormant -- the LP plans pure import
    # for the load instead, which fires force-hold-import (PRD §8.6).
    states = _states()  # default: 0 PV, 1 kW load
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)
    assert cycle.force_pv_export_enabled is True  # toggle read, branch idle


# ---------------------------------------------------------------------------
# Slot-0 PV refinement. The planner clamps the slot-0 forecast against a
# trailing measured average so the force-export branch can't speculate above
# real-time production. The clamp is one-sided: live > forecast must be
# ignored, leaving the upstream forecaster's view intact.
# ---------------------------------------------------------------------------


def test_live_pv_average_caps_slot0_when_below_forecast() -> None:
    # Forecast: 5 kW PV across the horizon. Live trailing average reports
    # 1 kW (clouded over). Slot 0's PV must drop to 1 kW so the active
    # export branch only commits to what's actually being produced; later
    # slots stay at the forecast.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    averager = FakeLiveAverager(value=1.0)
    cycle = Planner(cfg, FakeReader(states), FakeCaller(),
                    live_averager=averager).step(NOW)

    slots = cycle.result.slots
    # Slot 0: 1 kW PV - 1 kW load = nothing left to sell; setpoint stays at 0
    # because the force-export branch only fires when p_sell > 0.
    assert slots[0].p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    # Subsequent slots still see the forecast and export as before.
    assert slots[1].p_sell_kw > 1e-3
    # The averager was queried for the configured PV power entity over a
    # window matching update_seconds.
    assert averager.calls and averager.calls[0][0] == "sensor.pv_w"
    window = averager.calls[0][2] - averager.calls[0][1]
    assert window == timedelta(seconds=cfg.update_seconds)


def test_live_pv_average_above_forecast_keeps_forecast() -> None:
    # Live PV briefly spikes above the forecast (e.g., burst of clear sky).
    # The clamp is one-sided -- slot 0 must stay at the forecast so the LP
    # never speculates beyond the upstream forecaster's slot-average view.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller(),
                    live_averager=FakeLiveAverager(value=8.0)).step(NOW)

    first = cycle.result.slots[0]
    # Slot-0 export equals the forecast surplus (5 - 1 = 4 kW), not 7 kW.
    assert first.p_sell_kw == pytest.approx(4.0, abs=1e-3)


def test_live_pv_average_none_keeps_forecast() -> None:
    # No history yet (averager returns None) -- the planner must fall back
    # to the unmodified forecast rather than zeroing PV out.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller(),
                    live_averager=FakeLiveAverager(value=None)).step(NOW)

    assert cycle.result.slots[0].p_sell_kw == pytest.approx(4.0, abs=1e-3)


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



# ---------------------------------------------------------------------------
# Minimum sell price floor. Slots whose sell price falls below the configured
# threshold get ``feedin_allowed=False``, which the optimizer enforces as
# ``p_sell[t] = 0``. Default 0.0 is a no-op (any non-negative sell price
# clears the floor) so this is opt-in and backward compatible.
# ---------------------------------------------------------------------------


def test_min_sell_price_default_zero_preserves_passive_export() -> None:
    # Regression guard: with the default floor (0.0), the existing passive-
    # surplus behaviour from test_pv_surplus_triggers_feedin_on_... must
    # still hold. Sell price 0.10 > 0.0, so no slot is gated off.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 1e-3
    assert cycle.applied_feedin is True


def test_min_sell_price_above_all_prices_disables_export() -> None:
    # Floor above the constant 0.10 sell price -> every slot has
    # feedin_allowed=False -> p_sell pinned to 0 across the horizon, slot-0
    # setpoint stays at 0, and the feed-in switch is turned off.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cfg = _config(min_sell_price_per_kwh=0.20)
    caller = FakeCaller()
    cycle = Planner(cfg, FakeReader(states), caller).step(NOW)

    assert all(s.p_sell_kw == pytest.approx(0.0, abs=1e-3) for s in cycle.result.slots)
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_feedin is False
    assert ("switch", "turn_off") in [(d, s) for d, s, _ in caller.calls]


def test_min_sell_price_partial_horizon_gates_only_cheap_slots() -> None:
    # Two-tier sell price: first two slots at 0.05 (below floor), last two at
    # 0.30 (above). With floor=0.10 the LP must keep PV in the battery during
    # the cheap morning slots and export it in the expensive afternoon ones.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    # NOW is hour 12 and the horizon is 4 slots, so indices 12..15 of the
    # 24-hour sell array drive the LP. Cheap-then-expensive within the
    # horizon makes the gate observable.
    sell = [0.30] * 12 + [0.05, 0.05, 0.30, 0.30] + [0.30] * 8
    states = _states(pv_forecast=pv_forecast, sell=sell)
    cfg = _config(min_sell_price_per_kwh=0.10)
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    slots = cycle.result.slots
    assert slots[0].p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert slots[1].p_sell_kw == pytest.approx(0.0, abs=1e-3)
    # At least one of the eligible later slots must export something.
    assert slots[2].p_sell_kw + slots[3].p_sell_kw > 1e-3
    # Slot 0 is gated -> feed-in switch off this cycle.
    assert cycle.applied_feedin is False


# ---------------------------------------------------------------------------
# Force-hold-import (PRD §8.6). When the LP plans pure grid coverage of the
# load with the battery idle (``p_buy > 0`` ∧ ``p_chg = p_dis = 0``), the
# planner must pin the grid set-point to the planned import. Otherwise the
# inverter's native EMS would silently drain the battery to cover the load —
# violating the LP's intent (typically driven by the §8.5 health-floor
# penalty making further discharge expensive, or by ``soc_min`` sitting
# above the inverter's BMS floor).
# ---------------------------------------------------------------------------


def test_force_hold_import_writes_positive_setpoint_matching_load() -> None:
    # Pure nighttime load (no PV, 1.5 kW load), nothing to arbitrage. The LP
    # plans grid coverage with the battery idle; force-hold-import pins the
    # set-point to ``p_buy * 1000`` so the EMS doesn't drain the battery.
    states = _states(load_w=1500.0)
    caller = FakeCaller()
    cycle = Planner(_config(), FakeReader(states), caller).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_buy_kw == pytest.approx(1.5, abs=1e-3)
    assert first.p_chg_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1500.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(
        (first.p_buy_kw - first.p_sell_kw) * 1000.0, abs=1e-3,
    )
    assert cycle.applied_feedin is False


def test_force_hold_import_dormant_when_lp_discharges_battery() -> None:
    # Slot 0 is expensive to buy from (0.50), the rest of the horizon is
    # cheap (0.05). The LP discharges the battery now and recharges later
    # (sell=0 throughout, so no export confounds the picture). Slot 0 has
    # p_dis > 0 ∧ p_buy = 0 -> force-hold-import does NOT fire and the
    # set-point stays passive (0); the inverter's self-consumption mode
    # handles the discharge naturally.
    states = _states(
        load_w=1000.0,
        buy=_hourly(value_at_noon=0.50, value_elsewhere=0.05),
        sell=[0.0] * 24,
    )
    cycle = Planner(_config(), FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_buy_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_dis_kw > 1e-3                               # battery covers load
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)


def test_physical_soc_projection_stays_flat_under_force_hold_import() -> None:
    # Same scenario as the basic force-hold-import test: pure-load coverage
    # from the grid with battery idle. The physical-SoC projection must
    # mirror the LP (battery flat) instead of running the passive-deficit
    # branch that would project a discharge equal to load × dt.
    #
    # Non-zero cycle_cost makes the all-slots-idle answer strictly cheaper
    # than gratuitous round-trips (LP would otherwise be indifferent under
    # 100% efficiency + zero cycle cost).
    cfg = _config()
    cfg = replace(cfg, battery=replace(cfg.battery, cycle_cost_per_kwh=0.01))
    states = _states(soc_pct=50.0, load_w=1500.0)  # 50% of 10 kWh -> 5.0 kWh
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    slots = cycle.result.slots
    # All slots are pure-load coverage -> all force-hold-import.
    for s in slots:
        assert s.p_buy_kw == pytest.approx(1.5, abs=1e-3)
        assert s.p_chg_kw == pytest.approx(0.0, abs=1e-3)
        assert s.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    # Projection stays flat across the whole horizon (no physical drain).
    for s in slots:
        assert s.soc_physical_kwh == pytest.approx(5.0, abs=1e-3)
