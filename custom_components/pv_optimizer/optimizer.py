"""Linear-programming optimizer for the PV+battery+grid system.

Pure Python, no Home Assistant imports. See PRD.md §7 for the formulation.
"""
from __future__ import annotations

import logging
import time
from typing import Sequence

import pulp

from .models import (
    OptimizerError,
    OptimizerInputs,
    OptimizerResult,
    SlotPlan,
    TariffSlot,
)

_LOGGER = logging.getLogger(__name__)

# Cache the chosen solver factory across solves: discovery is non-trivial
# (PuLP imports highspy / probes CBC binary) and the result is stable for
# the lifetime of the process.
_SOLVER_FACTORY = None  # type: ignore[var-annotated]


def _make_solver() -> "pulp.LpSolver":
    """Return an available LP solver, preferring in-process HiGHS over CBC.

    HiGHS (via ``highspy``) is preferred because it ships pre-built wheels
    for every supported Python version and runs in-process — no subprocess
    exec, no missing-binary failure mode. CBC is kept as a fallback so an
    install that already has a working CBC keeps using it.

    Raises ``OptimizerError`` if no LP solver is available.
    """
    global _SOLVER_FACTORY
    if _SOLVER_FACTORY is not None:
        return _SOLVER_FACTORY()

    candidates = (
        ("HiGHS", lambda: pulp.HiGHS(msg=False)),
        ("PULP_CBC_CMD", lambda: pulp.PULP_CBC_CMD(msg=False)),
    )
    available = set(pulp.listSolvers(onlyAvailable=True))
    for name, factory in candidates:
        if name not in available:
            continue
        try:
            solver = factory()
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.debug("Solver %s rejected at construction: %s", name, exc)
            continue
        _LOGGER.info("PV Optimizer using LP solver: %s", name)
        _SOLVER_FACTORY = factory
        return solver
    raise OptimizerError(
        "No LP solver available. Install 'highspy' (preferred) or ensure the "
        "CBC binary bundled with 'pulp' is executable on this platform."
    )


def solve(inputs: OptimizerInputs) -> OptimizerResult:
    """Solve the cost-minimisation LP and return the plan."""
    n = len(inputs.slots)
    bat = inputs.battery
    dt = [s.duration_h for s in inputs.slots]
    terminal = (
        inputs.terminal_soc_kwh
        if inputs.terminal_soc_kwh is not None
        else inputs.initial_soc_kwh
    )

    prob = pulp.LpProblem("pv_optimizer", pulp.LpMinimize)

    p_buy = [pulp.LpVariable(f"buy_{t}", lowBound=0, upBound=inputs.p_grid_imp_max_kw) for t in range(n)]
    p_sell, p_chg, p_dis, p_curt, soc = [], [], [], [], []
    for t in range(n):
        sell_ub = inputs.p_grid_exp_max_kw if inputs.slots[t].feedin_allowed else 0.0
        p_sell.append(pulp.LpVariable(f"sell_{t}", lowBound=0, upBound=sell_ub))
        p_chg.append(pulp.LpVariable(f"chg_{t}", lowBound=0, upBound=bat.p_chg_max_kw))
        p_dis.append(pulp.LpVariable(f"dis_{t}", lowBound=0, upBound=bat.p_dis_max_kw))
        # PV curtailment: free to dump excess PV when battery full and no feed-in.
        p_curt.append(pulp.LpVariable(f"curt_{t}", lowBound=0, upBound=inputs.pv_kw[t]))
        soc.append(pulp.LpVariable(f"soc_{t}", lowBound=bat.soc_min_kwh, upBound=bat.soc_max_kwh))
    soc_end = pulp.LpVariable("soc_end", lowBound=bat.soc_min_kwh, upBound=bat.soc_max_kwh)

    # Initial SoC
    prob += soc[0] == inputs.initial_soc_kwh, "soc_init"

    # Per-slot constraints
    for t in range(n):
        # Power balance: pv + p_dis + p_buy = load + p_chg + p_sell + p_curt
        prob += (
            inputs.pv_kw[t] + p_dis[t] + p_buy[t]
            == inputs.load_kw[t] + p_chg[t] + p_sell[t] + p_curt[t]
        ), f"balance_{t}"
        # SoC dynamics
        next_soc = soc[t + 1] if t + 1 < n else soc_end
        prob += (
            next_soc == soc[t] + dt[t] * (bat.eta_chg * p_chg[t] - p_dis[t] / bat.eta_dis)
        ), f"soc_{t}"

    # Terminal SoC (at end of horizon)
    prob += soc_end >= terminal, "soc_terminal"

    # Optional soft "health" floor above ``soc_min``. For each slot we add
    # a slack ``deficit[t] >= max(0, soc_health - soc[t])`` and pay
    # ``penalty * dt[t] * deficit[t]`` in the objective. This costs the LP
    # money for parking the battery near the bottom of its operating range
    # without rigidly forbidding deep discharges when prices warrant. The
    # end-of-horizon ``soc_end`` gets the same treatment so the LP can't
    # game the boundary by parking low at the last slot. Disabled (no
    # variables, no constraints) when penalty == 0 or the floor sits at /
    # below ``soc_min`` — both defaults, so this is a no-op for upgrading
    # users until they explicitly opt in. See PRD §8.5.
    health_active = (
        bat.low_soc_penalty_per_kwh_h > 0
        and bat.soc_health_kwh > bat.soc_min_kwh
    )
    deficit: list = []
    deficit_end = None
    if health_active:
        for t in range(n):
            d = pulp.LpVariable(f"deficit_{t}", lowBound=0,
                                upBound=bat.soc_health_kwh - bat.soc_min_kwh)
            prob += d >= bat.soc_health_kwh - soc[t], f"deficit_{t}_lb"
            deficit.append(d)
        deficit_end = pulp.LpVariable(
            "deficit_end", lowBound=0,
            upBound=bat.soc_health_kwh - bat.soc_min_kwh,
        )
        prob += deficit_end >= bat.soc_health_kwh - soc_end, "deficit_end_lb"

    # Objective. Two tiny regularizers break degeneracy without changing
    # economically meaningful decisions:
    #   - eps_curt penalises curtailment so free PV is stored when possible.
    #   - eps_cycle (much smaller) prefers idle over arbitrary cycling when
    #     cycling has zero economic value. eps_curt > eps_cycle ensures
    #     "charge instead of curtailing" wins.
    eps_curt = 1e-4
    eps_cycle = 1e-5
    cost_terms = []
    for t in range(n):
        s = inputs.slots[t]
        cost_terms.append(
            dt[t] * (
                s.price_buy * p_buy[t]
                - s.price_sell * p_sell[t]
                + bat.cycle_cost_per_kwh * (p_chg[t] + p_dis[t])
                + eps_curt * p_curt[t]
                + eps_cycle * (p_chg[t] + p_dis[t])
            )
        )
        if health_active:
            cost_terms.append(
                dt[t] * bat.low_soc_penalty_per_kwh_h * deficit[t]
            )
    if health_active:
        # Charge ``deficit_end`` over the duration of the final slot so the
        # boundary penalty is dimensionally consistent with the per-slot
        # ones (currency/(kWh*h) * kWh * h).
        cost_terms.append(
            dt[-1] * bat.low_soc_penalty_per_kwh_h * deficit_end
        )
    prob += pulp.lpSum(cost_terms)

    t0 = time.perf_counter()
    try:
        status = prob.solve(_make_solver())
    except pulp.PulpError as exc:
        raise OptimizerError(f"LP solver failed: {exc}") from exc
    except OSError as exc:
        # CBC subprocess exec failures (missing/non-executable binary,
        # wrong-arch wheel) bubble up as OSError; surface as OptimizerError
        # so the planner records a clean last_error instead of crashing.
        raise OptimizerError(f"LP solver subprocess failed: {exc}") from exc
    solve_time = time.perf_counter() - t0
    status_name = pulp.LpStatus[status]
    if status_name != "Optimal":
        raise OptimizerError(f"LP not optimal: {status_name}")

    plan = [
        SlotPlan(
            index=t,
            start=inputs.slots[t].start,
            duration_h=inputs.slots[t].duration_h,
            p_buy_kw=float(p_buy[t].value() or 0.0),
            p_sell_kw=float(p_sell[t].value() or 0.0),
            p_chg_kw=float(p_chg[t].value() or 0.0),
            p_dis_kw=float(p_dis[t].value() or 0.0),
            soc_start_kwh=float(soc[t].value() or 0.0),
        )
        for t in range(n)
    ]
    total = float(pulp.value(prob.objective))

    return OptimizerResult(
        slots=plan,
        total_cost=total,
        passive_cost=passive_cost(inputs),
        status=status_name,
        solve_time_s=solve_time,
        extras={"soc_end_kwh": float(soc_end.value() or 0.0)},
    )


def passive_cost(inputs: OptimizerInputs) -> float:
    """Cost of doing nothing: battery idle, surplus exported (if allowed) or curtailed.

    This baseline is used to compute optimizer savings. No battery action means
    no cycle cost. Excess PV is sold when feed-in is allowed, otherwise wasted.
    """
    total = 0.0
    for slot, pv, load in zip(inputs.slots, inputs.pv_kw, inputs.load_kw):
        net = load - pv  # positive = need to buy, negative = surplus
        if net >= 0:
            total += slot.duration_h * slot.price_buy * net
        else:
            export = -net if slot.feedin_allowed else 0.0
            export = min(export, inputs.p_grid_exp_max_kw)
            total -= slot.duration_h * slot.price_sell * export
    return total


def aggregate_savings(result: OptimizerResult) -> float:
    """Convenience accessor matching :pyattr:`OptimizerResult.savings`."""
    return result.savings


def slots_summary(slots: Sequence[TariffSlot]) -> dict:
    """Tiny diagnostic helper used by sensors / tests."""
    return {
        "count": len(slots),
        "duration_h_total": sum(s.duration_h for s in slots),
    }
