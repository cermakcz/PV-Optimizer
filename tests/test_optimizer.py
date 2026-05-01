"""Unit tests for the LP optimizer."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.pv_optimizer.models import (
    BatteryParams,
    OptimizerError,
    OptimizerInputs,
    TariffSlot,
)
from custom_components.pv_optimizer.optimizer import passive_cost, solve

T0 = datetime(2026, 1, 1, 0, 0, 0)


def _slots(prices_buy, prices_sell=None, *, feedin=True, dt_h=1.0):
    if prices_sell is None:
        prices_sell = prices_buy
    feedin_list = feedin if isinstance(feedin, (list, tuple)) else [feedin] * len(prices_buy)
    return [
        TariffSlot(
            start=T0 + timedelta(hours=i * dt_h),
            duration_h=dt_h,
            price_buy=pb,
            price_sell=ps,
            feedin_allowed=fa,
        )
        for i, (pb, ps, fa) in enumerate(zip(prices_buy, prices_sell, feedin_list))
    ]


def _battery(**overrides) -> BatteryParams:
    base = dict(
        capacity_kwh=10.0,
        soc_min_kwh=1.0,
        soc_max_kwh=9.0,
        p_chg_max_kw=5.0,
        p_dis_max_kw=5.0,
        eta_chg=1.0,
        eta_dis=1.0,
        cycle_cost_eur_per_kwh=0.0,
    )
    base.update(overrides)
    return BatteryParams(**base)


# ---------------------------------------------------------------------------
# Basic balance / no-action scenarios
# ---------------------------------------------------------------------------

def test_pv_equals_load_zero_action() -> None:
    bat = _battery()
    slots = _slots([0.2] * 4)
    inp = OptimizerInputs(slots, [1.0] * 4, [1.0] * 4, 5.0, bat, 10, 10)
    r = solve(inp)
    assert r.status == "Optimal"
    assert r.total_cost_eur == pytest.approx(0.0, abs=1e-6)
    for sp in r.slots:
        assert sp.p_buy_kw == pytest.approx(0.0, abs=1e-6)
        assert sp.p_sell_kw == pytest.approx(0.0, abs=1e-6)
        assert sp.p_chg_kw == pytest.approx(0.0, abs=1e-6)
        assert sp.p_dis_kw == pytest.approx(0.0, abs=1e-6)


def test_surplus_pv_exports_when_feedin_allowed() -> None:
    bat = _battery()
    slots = _slots([0.30] * 4, [0.10] * 4)  # buy high, sell low
    inp = OptimizerInputs(slots, [3.0] * 4, [1.0] * 4, 5.0, bat, 10, 10)
    r = solve(inp)
    # Net surplus = 2 kW * 4h = 8 kWh; sold at 0.10 -> -0.80 EUR (profit).
    # Per-slot distribution is degenerate when cycle cost is zero, so check totals.
    assert r.total_cost_eur == pytest.approx(-0.80, abs=1e-4)
    total_sell = sum(sp.p_sell_kw * sp.duration_h for sp in r.slots)
    total_buy = sum(sp.p_buy_kw * sp.duration_h for sp in r.slots)
    assert total_sell - total_buy == pytest.approx(8.0, abs=1e-4)


def test_surplus_pv_charges_battery_when_feedin_disabled() -> None:
    bat = _battery()
    slots = _slots([0.30] * 4, [0.10] * 4, feedin=False)
    inp = OptimizerInputs(slots, [3.0] * 4, [1.0] * 4, 5.0, bat, 10, 10)
    r = solve(inp)
    # Cannot export -> battery must absorb what it can (5 -> 9 = 4 kWh), rest is curtailed.
    for sp in r.slots:
        assert sp.p_sell_kw == pytest.approx(0.0, abs=1e-6)
    soc_end = r.extras["soc_end_kwh"]
    assert soc_end == pytest.approx(9.0, abs=1e-4)
    # No grid import either (would only worsen cost): all load is covered by PV.
    assert sum(sp.p_buy_kw for sp in r.slots) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Arbitrage and amortization
# ---------------------------------------------------------------------------

def test_arbitrage_cheap_to_expensive_no_cycle_cost() -> None:
    bat = _battery(cycle_cost_eur_per_kwh=0.0)
    # 4 cheap hours then 4 expensive hours; load 1 kW; no PV.
    prices = [0.05] * 4 + [0.30] * 4
    slots = _slots(prices, [0.0] * 8)  # no export to keep test focused
    inp = OptimizerInputs(slots, [0.0] * 8, [1.0] * 8, 5.0, bat, 10, 10)
    r = solve(inp)
    # Optimal: buy enough cheap to cover expensive load via battery.
    cheap_buy = sum(sp.p_buy_kw for sp in r.slots[:4])
    expensive_buy = sum(sp.p_buy_kw for sp in r.slots[4:])
    assert cheap_buy > expensive_buy
    # Must be cheaper than passive (always buying at hourly price).
    assert r.savings_eur > 0.0


def test_high_cycle_cost_kills_arbitrage() -> None:
    # Spread is 0.10, throughput cost on charge+discharge would be 2 * 0.20 = 0.40 -> never pays.
    bat = _battery(cycle_cost_eur_per_kwh=0.20)
    prices = [0.05] * 4 + [0.15] * 4
    slots = _slots(prices, [0.0] * 8)
    inp = OptimizerInputs(slots, [0.0] * 8, [1.0] * 8, 5.0, bat, 10, 10)
    r = solve(inp)
    for sp in r.slots:
        assert sp.p_chg_kw == pytest.approx(0.0, abs=1e-4)
        assert sp.p_dis_kw == pytest.approx(0.0, abs=1e-4)


# ---------------------------------------------------------------------------
# Limits, terminal SoC, infeasibility
# ---------------------------------------------------------------------------

def test_charge_power_limit_saturates() -> None:
    bat = _battery(p_chg_max_kw=2.0)
    slots = _slots([0.30], [0.10], feedin=False, dt_h=1.0)  # no export
    inp = OptimizerInputs(slots, [10.0], [1.0], 5.0, bat, 10, 0.0)
    r = solve(inp)
    # 9 kW surplus, charge capped at 2 kW, rest is PV-curtailed.
    assert r.slots[0].p_chg_kw == pytest.approx(2.0, abs=1e-4)


def test_terminal_soc_enforced() -> None:
    bat = _battery()
    slots = _slots([0.10] * 2, [0.0] * 2)
    inp = OptimizerInputs(slots, [0.0] * 2, [0.0] * 2, 5.0, bat, 10, 10, terminal_soc_kwh=8.0)
    r = solve(inp)
    assert r.extras["soc_end_kwh"] == pytest.approx(8.0, abs=1e-4)


def test_infeasible_grid_too_small() -> None:
    bat = _battery(p_dis_max_kw=0.0, p_chg_max_kw=0.0)
    slots = _slots([0.10])
    inp = OptimizerInputs(slots, [0.0], [5.0], 5.0, bat, p_grid_imp_max_kw=1.0, p_grid_exp_max_kw=0.0)
    with pytest.raises(OptimizerError):
        solve(inp)


def test_lower_round_trip_eta_reduces_savings() -> None:
    prices = [0.05] * 4 + [0.30] * 4
    slots = _slots(prices, [0.0] * 8)
    base_inputs = dict(slots=slots, pv_kw=[0.0] * 8, load_kw=[1.0] * 8,
                       initial_soc_kwh=5.0, p_grid_imp_max_kw=10, p_grid_exp_max_kw=10)
    r_high = solve(OptimizerInputs(battery=_battery(eta_chg=1.0, eta_dis=1.0), **base_inputs))
    r_low = solve(OptimizerInputs(battery=_battery(eta_chg=0.8, eta_dis=0.8), **base_inputs))
    assert r_low.savings_eur < r_high.savings_eur
    assert r_low.savings_eur > 0  # still some arbitrage value


def test_passive_cost_matches_manual() -> None:
    bat = _battery()
    slots = _slots([0.20, 0.30], [0.05, 0.05])
    inp = OptimizerInputs(slots, [0.0, 2.0], [1.0, 1.0], 5.0, bat, 10, 10)
    # Slot 0: net=+1 kW * 0.20 = 0.20; Slot 1: surplus 1 kW exported at 0.05 = -0.05
    assert passive_cost(inp) == pytest.approx(0.15, abs=1e-6)
