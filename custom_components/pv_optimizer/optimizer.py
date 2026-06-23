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
        _LOGGER.info("PV LP Optimizer using LP solver: %s", name)
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
    sell_ub_t: list[float] = []
    for t in range(n):
        sell_ub = inputs.p_grid_exp_max_kw if inputs.slots[t].feedin_allowed else 0.0
        sell_ub_t.append(sell_ub)
        p_sell.append(pulp.LpVariable(f"sell_{t}", lowBound=0, upBound=sell_ub))
        p_chg.append(pulp.LpVariable(f"chg_{t}", lowBound=0, upBound=bat.p_chg_max_kw))
        p_dis.append(pulp.LpVariable(f"dis_{t}", lowBound=0, upBound=bat.p_dis_max_kw))
        # PV curtailment: free to dump excess PV when battery full and no feed-in.
        p_curt.append(pulp.LpVariable(f"curt_{t}", lowBound=0, upBound=inputs.pv_kw[t]))
        soc.append(pulp.LpVariable(f"soc_{t}", lowBound=bat.soc_min_kwh, upBound=bat.soc_max_kwh))
    soc_end = pulp.LpVariable("soc_end", lowBound=bat.soc_min_kwh, upBound=bat.soc_max_kwh)

    # EV charging variables. Created only when an EV target is set; the
    # upper bound is the charger's max power for slots inside the
    # ``[start_index, deadline_index)`` window, 0 elsewhere — outright
    # disables the variable for out-of-window slots. ``start_index`` lets
    # the planner pre-schedule a future-start charging block (user sets a
    # planned arrival time and the LP reserves slots from that time on).
    ev_active = inputs.ev is not None and inputs.ev_target_kwh > 0
    p_ev: list = []
    if ev_active:
        for t in range(n):
            ub = (inputs.ev.max_charging_power_kw
                  if (inputs.ev_deadline_index is not None
                      and inputs.ev_start_index <= t < inputs.ev_deadline_index)
                  else 0.0)
            p_ev.append(pulp.LpVariable(f"ev_{t}", lowBound=0, upBound=ub))
    else:
        p_ev = [0.0] * n  # constant zeros; PuLP handles mixed-numeric expressions

    # Grid import/export mutual exclusion. A single net-metered connection
    # exchanges one net power with the grid, so importing and exporting in
    # the same slot is unphysical. Without forbidding it the LP fabricates a
    # round-trip whenever ``price_sell >= price_buy`` (e.g. an evening price
    # spike): import N kW and re-export it at the feed-in cap to bank the
    # spread, pinning ``p_sell`` to the export limit and corrupting the
    # derived inverter set-point. One binary per slot (``export_on``) gates
    # the two legs via big-M; the big-Ms are the variables' own upper
    # bounds, so the constraints add no slack beyond what already bounded
    # them. Slots with ``sell_ub == 0`` already pin ``p_sell == 0``, so the
    # solver is free to pick ``export_on = 0`` and import normally.
    export_on = [pulp.LpVariable(f"export_on_{t}", cat="Binary") for t in range(n)]

    # Initial SoC
    prob += soc[0] == inputs.initial_soc_kwh, "soc_init"

    # Per-slot constraints
    for t in range(n):
        # Power balance: pv + p_dis + p_buy = load + p_ev + p_chg + p_sell + p_curt
        prob += (
            inputs.pv_kw[t] + p_dis[t] + p_buy[t]
            == inputs.load_kw[t] + p_ev[t] + p_chg[t] + p_sell[t] + p_curt[t]
        ), f"balance_{t}"
        # Mutual exclusion: export only when export_on, import only otherwise.
        prob += p_sell[t] <= sell_ub_t[t] * export_on[t], f"export_gate_{t}"
        prob += (
            p_buy[t] <= inputs.p_grid_imp_max_kw * (1 - export_on[t])
        ), f"import_gate_{t}"
        # SoC dynamics
        next_soc = soc[t + 1] if t + 1 < n else soc_end
        prob += (
            next_soc == soc[t] + dt[t] * (bat.eta_chg * p_chg[t] - p_dis[t] / bat.eta_dis)
        ), f"soc_{t}"

    # Terminal SoC (at end of horizon)
    prob += soc_end >= terminal, "soc_terminal"

    # Soft EV energy constraint: ev_deficit absorbs any shortfall so the LP
    # stays feasible when capacity before deadline is genuinely insufficient
    # (deadline too soon, plug-in too late).
    ev_deficit = None
    if ev_active:
        ev_deficit = pulp.LpVariable("ev_deficit", lowBound=0,
                                     upBound=inputs.ev_target_kwh)
        prob += (
            ev_deficit >= inputs.ev_target_kwh - pulp.lpSum(
                inputs.slots[t].duration_h * p_ev[t] for t in range(n)
            )
        ), "ev_deficit_lb"

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
    # ``cycle_cost_per_kwh`` is amortised on the *discharge* leg only (LCOS
    # convention: cost per kWh delivered out of the battery). The matching
    # charge-side wear is implicit in the input number, which the README
    # recipe derives as ``battery_price / (cycles · usable_kWh · η_rt)``.
    # ``eps_cycle`` stays on both legs so it still discourages co-charging
    # and co-discharging in the same slot when round-trip efficiency is 1.
    eps_curt = 1e-4
    eps_cycle = 1e-5
    # Among equal-cost EV schedules (the battery can shuttle energy between
    # in-window slots, so the slot choice is otherwise degenerate) prefer
    # charging earlier. A per-slot-index tilt this small never overrides a
    # real price difference, but it makes the plan deterministic across
    # solvers and biases the charge toward the soonest slot — a safer hedge
    # against forecast error and tightening deadlines.
    eps_ev_early = 1e-6
    cost_terms = []
    for t in range(n):
        s = inputs.slots[t]
        cost_terms.append(
            dt[t] * (
                s.price_buy * p_buy[t]
                - s.price_sell * p_sell[t]
                + bat.cycle_cost_per_kwh * p_dis[t]
                + eps_curt * p_curt[t]
                + eps_cycle * (p_chg[t] + p_dis[t])
            )
        )
        if ev_active:
            cost_terms.append(eps_ev_early * t * p_ev[t])
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
    if ev_active:
        # Penalty must beat the highest realistic buy price so the LP
        # prefers expensive grid over leaving the target unmet. 100x is
        # a safe multiplier (real EV deficit values matter to the user
        # at order-of-magnitude scale, not at the cent).
        max_buy = max((s.price_buy for s in inputs.slots), default=1.0)
        ev_deficit_penalty = 100.0 * max(max_buy, 0.01)
        cost_terms.append(ev_deficit_penalty * ev_deficit)
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
            p_ev_chg_kw=(float(p_ev[t].value() or 0.0) if ev_active else 0.0),
        )
        for t in range(n)
    ]
    total = float(pulp.value(prob.objective))

    extras: dict = {"soc_end_kwh": float(soc_end.value() or 0.0)}
    if ev_active and ev_deficit is not None:
        extras["ev_deficit_kwh"] = float(ev_deficit.value() or 0.0)
    return OptimizerResult(
        slots=plan,
        total_cost=total,
        passive_cost=passive_cost(inputs),
        status=status_name,
        solve_time_s=solve_time,
        extras=extras,
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
