"""Unit tests for the LP optimizer."""
from __future__ import annotations

from datetime import datetime, timedelta

import pulp
import pytest

from custom_components.pv_optimizer import optimizer as optimizer_mod
from custom_components.pv_optimizer.models import (
    BatteryParams,
    OptimizerError,
    OptimizerInputs,
    TariffSlot,
)
from custom_components.pv_optimizer.optimizer import (
    _make_solver,
    passive_cost,
    solve,
)


@pytest.fixture(autouse=True)
def _reset_solver_cache():
    """Each test starts with a clean solver cache so monkeypatching takes effect."""
    optimizer_mod._SOLVER_FACTORY = None
    yield
    optimizer_mod._SOLVER_FACTORY = None

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
        cycle_cost_per_kwh=0.0,
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
    assert r.total_cost == pytest.approx(0.0, abs=1e-6)
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
    # Net surplus = 2 kW * 4h = 8 kWh; sold at 0.10 -> -0.80 (profit).
    # Per-slot distribution is degenerate when cycle cost is zero, so check totals.
    assert r.total_cost == pytest.approx(-0.80, abs=1e-4)
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
    bat = _battery(cycle_cost_per_kwh=0.0)
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
    assert r.savings > 0.0


def test_high_cycle_cost_kills_arbitrage() -> None:
    # Spread is 0.10, throughput cost on charge+discharge would be 2 * 0.20 = 0.40 -> never pays.
    bat = _battery(cycle_cost_per_kwh=0.20)
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
    assert r_low.savings < r_high.savings
    assert r_low.savings > 0  # still some arbitrage value


def test_passive_cost_matches_manual() -> None:
    bat = _battery()
    slots = _slots([0.20, 0.30], [0.05, 0.05])
    inp = OptimizerInputs(slots, [0.0, 2.0], [1.0, 1.0], 5.0, bat, 10, 10)
    # Slot 0: net=+1 kW * 0.20 = 0.20; Slot 1: surplus 1 kW exported at 0.05 = -0.05
    assert passive_cost(inp) == pytest.approx(0.15, abs=1e-6)


# ---------------------------------------------------------------------------
# Soft "health" floor (PRD §8.5). A linear per-slot penalty on
# (soc_health - soc[t])+ teaches the LP that long dwells at low SoC cost
# money, without rigidly forbidding deep discharges when prices warrant.
# Defaults (floor == soc_min, penalty == 0) make the feature a no-op.
# ---------------------------------------------------------------------------


def test_low_soc_penalty_default_zero_is_noop() -> None:
    # With penalty == 0 the LP must produce the same plan whether the
    # health floor sits at soc_min or above it. Cheap buy + zero sell, no
    # PV, no load -> baseline LP keeps the battery at the initial (= min)
    # SoC because there is no economic reason to charge.
    bat = _battery(
        soc_health_kwh=5.0, low_soc_penalty_per_kwh_h=0.0,
    )
    slots = _slots([0.05] * 4, [0.0] * 4)
    inp = OptimizerInputs(slots, [0.0] * 4, [0.0] * 4, 1.0, bat, 10, 10)
    r = solve(inp)
    assert r.status == "Optimal"
    # No charging anywhere -> battery stays at the initial soc_min (1 kWh).
    for sp in r.slots:
        assert sp.p_chg_kw == pytest.approx(0.0, abs=1e-3)
        assert sp.soc_start_kwh == pytest.approx(1.0, abs=1e-3)
    assert r.total_cost == pytest.approx(0.0, abs=1e-6)


def test_low_soc_penalty_pulls_soc_to_health_floor_when_cheap() -> None:
    # Same scenario as above but with the penalty turned on. Now it is
    # cheaper to charge to the health floor (paying buy*4 kWh + cycle_cost)
    # than to keep paying the dwell penalty for four slots. Battery should
    # land at soc_health by the start of slot 1 and stay there.
    bat = _battery(
        soc_health_kwh=5.0, low_soc_penalty_per_kwh_h=1.0,
    )
    slots = _slots([0.05] * 4, [0.0] * 4)
    inp = OptimizerInputs(slots, [0.0] * 4, [0.0] * 4, 1.0, bat, 10, 10)
    r = solve(inp)
    assert r.status == "Optimal"
    # Slot 0 charges 4 kWh; subsequent slots sit at the health floor with
    # zero penalty contribution.
    assert r.slots[0].p_chg_kw == pytest.approx(4.0, abs=1e-3)
    assert r.slots[0].p_buy_kw == pytest.approx(4.0, abs=1e-3)
    for sp in r.slots[1:]:
        assert sp.soc_start_kwh == pytest.approx(5.0, abs=1e-3)
        assert sp.p_chg_kw == pytest.approx(0.0, abs=1e-3)
    # soc_end honoured too (terminal default == initial == 1, so anything
    # >= 1 is acceptable; LP keeps it at the health floor since lowering
    # it again costs cycle/penalty for no benefit).
    assert r.extras["soc_end_kwh"] == pytest.approx(5.0, abs=1e-3)


def test_low_soc_penalty_yields_to_strong_sell_opportunity() -> None:
    # Battery starts full (9 kWh = soc_max). Slot 0 has a high sell price
    # (10.0/kWh), the other slots have zero sell price. With a non-trivial
    # dwell penalty (0.5/(kWh*h)) and a generous health floor (5 kWh),
    # the LP should discharge the battery as deep as the discharge-power
    # limit allows in slot 0 and accept the subsequent dwell-below-floor
    # penalty, because the export revenue vastly outweighs it. Grid
    # import is disabled so the battery is the only source for the
    # lucrative export — otherwise the LP would saturate the export cap
    # via grid arbitrage and the marginal value of the battery dip would
    # collapse to (sell - buy) instead of the full sell price.
    bat = _battery(
        soc_health_kwh=5.0, low_soc_penalty_per_kwh_h=0.5,
    )
    slots = _slots(
        [10.0, 10.0, 10.0, 10.0],   # buy (disabled by import limit anyway)
        [10.0, 0.0, 0.0, 0.0],      # sell — only slot 0 is lucrative
    )
    inp = OptimizerInputs(slots, [0.0] * 4, [0.0] * 4, 9.0, bat, 0, 50,
                          terminal_soc_kwh=1.0)
    r = solve(inp)
    assert r.status == "Optimal"
    # Slot 0 discharges at the power limit (5 kW * 1 h = 5 kWh), taking
    # SoC from 9 to 4 — strictly below the 5 kWh health floor.
    assert r.slots[0].p_dis_kw == pytest.approx(5.0, abs=1e-3)
    post_dis_socs = [sp.soc_start_kwh for sp in r.slots[1:]]
    assert any(s < 5.0 - 1e-3 for s in post_dis_socs)
    # And the LP must have actually exported in slot 0 (otherwise the
    # test isn't exercising the "yields to revenue" branch).
    assert r.slots[0].p_sell_kw > 1e-3
    # Net cost negative — selling at 10/kWh dominates the small penalty.
    assert r.total_cost < 0.0


# ---------------------------------------------------------------------------
# Solver-selection coverage. The integration must not depend on the CBC
# subprocess (which is missing on Python 3.14 + sdist installs of pulp).
# ---------------------------------------------------------------------------


def test_make_solver_prefers_highs_over_cbc(monkeypatch) -> None:
    # Both available -> HiGHS wins.
    monkeypatch.setattr(pulp, "listSolvers", lambda onlyAvailable=False: ["PULP_CBC_CMD", "HiGHS"])
    solver = _make_solver()
    assert type(solver).__name__ == "HiGHS"


def test_make_solver_falls_back_to_cbc_when_highs_missing(monkeypatch) -> None:
    monkeypatch.setattr(pulp, "listSolvers", lambda onlyAvailable=False: ["PULP_CBC_CMD"])
    solver = _make_solver()
    assert type(solver).__name__ == "PULP_CBC_CMD"


def test_make_solver_raises_when_no_solver_available(monkeypatch) -> None:
    monkeypatch.setattr(pulp, "listSolvers", lambda onlyAvailable=False: [])
    with pytest.raises(OptimizerError, match="No LP solver available"):
        _make_solver()


def test_make_solver_caches_factory(monkeypatch) -> None:
    calls = {"n": 0}

    def _list(onlyAvailable=False):
        calls["n"] += 1
        return ["HiGHS"]

    monkeypatch.setattr(pulp, "listSolvers", _list)
    _make_solver()
    _make_solver()
    _make_solver()
    # Discovery must run only on the first call; subsequent calls re-use
    # the cached factory.
    assert calls["n"] == 1


def test_solve_wraps_pulp_error_as_optimizer_error(monkeypatch) -> None:
    """A PulpError from the underlying solver must surface as OptimizerError."""

    class _BoomSolver:
        def actualSolve(self, *_a, **_kw):
            raise pulp.PulpError("synthetic solver crash")

    monkeypatch.setattr(optimizer_mod, "_make_solver", lambda: _BoomSolver())
    bat = _battery()
    inp = OptimizerInputs(_slots([0.20]), [0.0], [1.0], 5.0, bat, 10, 10)
    with pytest.raises(OptimizerError, match="LP solver failed"):
        solve(inp)


def test_solve_wraps_oserror_as_optimizer_error(monkeypatch) -> None:
    """An OSError (e.g. CBC exec failure) must surface as OptimizerError too."""

    class _BoomSolver:
        def actualSolve(self, *_a, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "/missing/cbc")

    monkeypatch.setattr(optimizer_mod, "_make_solver", lambda: _BoomSolver())
    bat = _battery()
    inp = OptimizerInputs(_slots([0.20]), [0.0], [1.0], 5.0, bat, 10, 10)
    with pytest.raises(OptimizerError, match="LP solver subprocess failed"):
        solve(inp)
