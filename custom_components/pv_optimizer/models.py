"""Pure data models used by the optimizer.

These dataclasses contain no Home Assistant dependencies so the optimizer
remains fully unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence


@dataclass(frozen=True)
class BatteryParams:
    """Static battery parameters."""

    capacity_kwh: float
    soc_min_kwh: float
    soc_max_kwh: float
    p_chg_max_kw: float
    p_dis_max_kw: float
    eta_chg: float = 0.95
    eta_dis: float = 0.95
    cycle_cost_per_kwh: float = 0.05    # currency/kWh delivered (discharge)
    # Soft "health" floor: per-slot penalty rate applied to (soc_health -
    # soc[t])+ so the LP avoids long dwells at low SoC without a hard
    # constraint. Disabled when ``low_soc_penalty_per_kwh_h == 0`` or
    # ``soc_health_kwh <= soc_min_kwh`` — both defaults make this a no-op.
    soc_health_kwh: float = 0.0
    low_soc_penalty_per_kwh_h: float = 0.0   # currency/(kWh*h) below floor

    def __post_init__(self) -> None:
        if self.capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be > 0")
        if not (0.0 <= self.soc_min_kwh <= self.soc_max_kwh <= self.capacity_kwh):
            raise ValueError("require 0 <= soc_min <= soc_max <= capacity")
        if self.p_chg_max_kw < 0 or self.p_dis_max_kw < 0:
            raise ValueError("power limits must be >= 0")
        if not (0.0 < self.eta_chg <= 1.0) or not (0.0 < self.eta_dis <= 1.0):
            raise ValueError("efficiencies must be in (0, 1]")
        if self.cycle_cost_per_kwh < 0:
            raise ValueError("cycle_cost must be >= 0")
        if self.soc_health_kwh < 0 or self.soc_health_kwh > self.soc_max_kwh:
            raise ValueError("soc_health_kwh must be in [0, soc_max_kwh]")
        if self.low_soc_penalty_per_kwh_h < 0:
            raise ValueError("low_soc_penalty_per_kwh_h must be >= 0")


@dataclass(frozen=True)
class TariffSlot:
    """One discrete planning slot."""

    start: datetime
    duration_h: float
    price_buy: float       # currency/kWh, all-in (spot + surcharges + tax)
    price_sell: float      # currency/kWh, all-in
    feedin_allowed: bool = True

    def __post_init__(self) -> None:
        if self.duration_h <= 0:
            raise ValueError("duration_h must be > 0")


@dataclass(frozen=True)
class OptimizerInputs:
    """Bundle of everything the LP needs."""

    slots: Sequence[TariffSlot]
    pv_kw: Sequence[float]                # average PV power per slot (kW)
    load_kw: Sequence[float]              # average load per slot (kW)
    initial_soc_kwh: float
    battery: BatteryParams
    p_grid_imp_max_kw: float
    p_grid_exp_max_kw: float
    terminal_soc_kwh: float | None = None  # default: initial_soc_kwh

    def __post_init__(self) -> None:
        n = len(self.slots)
        if n == 0:
            raise ValueError("at least one slot required")
        if len(self.pv_kw) != n or len(self.load_kw) != n:
            raise ValueError("pv_kw and load_kw must match slots length")
        if any(v < 0 for v in self.pv_kw) or any(v < 0 for v in self.load_kw):
            raise ValueError("pv_kw and load_kw must be >= 0")
        if not (self.battery.soc_min_kwh
                <= self.initial_soc_kwh
                <= self.battery.soc_max_kwh):
            raise ValueError("initial_soc out of [soc_min, soc_max]")
        if self.p_grid_imp_max_kw < 0 or self.p_grid_exp_max_kw < 0:
            raise ValueError("grid power limits must be >= 0")
        if self.terminal_soc_kwh is not None and not (
            self.battery.soc_min_kwh
            <= self.terminal_soc_kwh
            <= self.battery.soc_max_kwh
        ):
            raise ValueError("terminal_soc out of [soc_min, soc_max]")


@dataclass(frozen=True)
class SlotPlan:
    """Optimal decisions for a single slot (kW for power, kWh for SoC).

    ``soc_physical_kwh`` is a planner-layer projection of the *actual* SoC
    the inverter is expected to reach at slot start, simulating passive
    self-consumption (PV→battery→export) for slots where the LP issues no
    forced setpoint. Left ``None`` by the optimizer; populated by the
    planner when wrapping the LP result.

    ``setpoint_w`` is the grid set-point the planner *would* write for
    this slot under the §8.1 active-vs-passive rules (positive = import,
    negative = export; ``0`` = passive / hand control to the EMS). Same
    lifecycle as ``soc_physical_kwh``: ``None`` from the optimizer,
    populated by the planner. Useful for chart overlays so the dashboard
    doesn't have to re-implement the predicate logic.
    """

    index: int
    start: datetime
    duration_h: float
    p_buy_kw: float
    p_sell_kw: float
    p_chg_kw: float
    p_dis_kw: float
    soc_start_kwh: float
    soc_physical_kwh: float | None = None
    setpoint_w: float | None = None


@dataclass(frozen=True)
class OptimizerResult:
    """Result of solving the LP."""

    slots: Sequence[SlotPlan]
    total_cost: float               # in user-configured currency; negative = net profit
    passive_cost: float             # cost of doing nothing (battery idle)
    status: str                     # "Optimal", ...
    solve_time_s: float
    extras: dict = field(default_factory=dict)

    @property
    def savings(self) -> float:
        return self.passive_cost - self.total_cost


class OptimizerError(RuntimeError):
    """Raised when the LP is infeasible or the solver fails."""
