# EV Charging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add brand-agnostic EV charging to `pv_optimizer` with reactive auto, LP-planned deadline charging, manual override, and off modes, honoring car-side intent as ultimate override.

**Architecture:** A new pure-Python module (`ev_controller.py`) owns state-vocabulary classification, latch-based reactive control, session-done detection, and slot-0 translation. The LP gains `p_ev_chg[t]` decision variables and an `ev_deficit` soft slack. The planner orchestrates: builds EV inputs, calls the controller, applies max-current / start-switch / mode outputs. The HA layer (config flow, sensors, integration-created entities) is the thinnest possible shim.

**Tech Stack:** Python 3.11+, PuLP (with HiGHS solver), pytest, Home Assistant custom integration (sensor / number / select / datetime / switch platforms). Spec at `docs/superpowers/specs/2026-05-27-ev-charging-design.md`.

---

## File Structure

**New files:**

- `custom_components/pv_optimizer/ev_controller.py` — All pure EV decision logic: state classification, reactive algorithm, latches, session-done detector, slot-0 translator, manual-mode handler. No HA imports.
- `tests/test_ev_controller.py` — Unit tests for `ev_controller.py`.

**Modified files:**

- `custom_components/pv_optimizer/models.py` — Add `EVParams`, `EVState`, extend `OptimizerInputs`/`SlotPlan` with EV fields.
- `custom_components/pv_optimizer/optimizer.py` — Add `p_ev_chg[t]` variables, extend balance, add `ev_deficit` slack.
- `custom_components/pv_optimizer/planner.py` — Add `EVConfig` to `PlannerConfig`, read EV inputs, call controller, apply outputs, integrate session energy.
- `custom_components/pv_optimizer/const.py` — Add EV config keys + defaults.
- `custom_components/pv_optimizer/config_flow.py` — Add optional EV step (skipped when blank).
- `custom_components/pv_optimizer/__init__.py` — Wire EV entry data into `PlannerConfig`.
- `custom_components/pv_optimizer/sensor.py` — Add EV diagnostic sensors.
- `custom_components/pv_optimizer/coordinator.py` — Add EV-state writers (mode select, target, deadline) and a state-keeper for the controller.
- `custom_components/pv_optimizer/manifest.json` — Add platforms `number`, `select`, `datetime` (and `switch` if needed).
- `tests/test_optimizer.py` — Extended for LP EV correctness.
- `tests/test_planner.py` — Extended for planner-side EV integration.

---

## Task 1: Add `EVParams` dataclass to `models.py`

**Files:**
- Modify: `custom_components/pv_optimizer/models.py`
- Test: `tests/test_optimizer.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_optimizer.py`:

```python
def test_ev_params_validates_positive_power() -> None:
    from custom_components.pv_optimizer.models import EVParams
    with pytest.raises(ValueError):
        EVParams(max_charging_power_kw=0.0, max_charging_current_a=16.0,
                 min_charging_current_a=6.0, car_battery_kwh=60.0)


def test_ev_params_validates_positive_current() -> None:
    from custom_components.pv_optimizer.models import EVParams
    with pytest.raises(ValueError):
        EVParams(max_charging_power_kw=11.0, max_charging_current_a=0.0,
                 min_charging_current_a=6.0, car_battery_kwh=60.0)


def test_ev_params_kw_per_amp_derived() -> None:
    from custom_components.pv_optimizer.models import EVParams
    p = EVParams(max_charging_power_kw=8.0, max_charging_current_a=20.0,
                 min_charging_current_a=6.0, car_battery_kwh=60.0)
    assert p.kw_per_amp == pytest.approx(0.4)


def test_ev_params_validates_min_below_max() -> None:
    from custom_components.pv_optimizer.models import EVParams
    with pytest.raises(ValueError):
        EVParams(max_charging_power_kw=11.0, max_charging_current_a=16.0,
                 min_charging_current_a=20.0, car_battery_kwh=60.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_optimizer.py::test_ev_params_validates_positive_power -v`
Expected: FAIL with `ImportError` (no `EVParams`).

- [ ] **Step 3: Implement `EVParams`** — append to `custom_components/pv_optimizer/models.py` (before `OptimizerError`):

```python
@dataclass(frozen=True)
class EVParams:
    """Static EV-charger parameters. Brand-agnostic.

    ``kw_per_amp`` is the only voltage/phase abstraction the optimizer
    uses: it's derived from the user-declared (power, current) pair at
    the charger's max-current setpoint and applied as a linear factor
    everywhere. No phase or voltage math.
    """

    max_charging_power_kw: float
    max_charging_current_a: float
    min_charging_current_a: float
    car_battery_kwh: float
    current_tolerance_a: float = 1.0
    session_done_power_w: float = 100.0
    session_done_seconds: float = 60.0
    buy_price_threshold: float = 0.0  # currency/kWh; reactive cheap-grid floor

    def __post_init__(self) -> None:
        if self.max_charging_power_kw <= 0:
            raise ValueError("max_charging_power_kw must be > 0")
        if self.max_charging_current_a <= 0:
            raise ValueError("max_charging_current_a must be > 0")
        if not (0 < self.min_charging_current_a <= self.max_charging_current_a):
            raise ValueError(
                "require 0 < min_charging_current_a <= max_charging_current_a")
        if self.car_battery_kwh <= 0:
            raise ValueError("car_battery_kwh must be > 0")
        if self.current_tolerance_a < 0:
            raise ValueError("current_tolerance_a must be >= 0")
        if self.session_done_power_w < 0:
            raise ValueError("session_done_power_w must be >= 0")
        if self.session_done_seconds < 0:
            raise ValueError("session_done_seconds must be >= 0")

    @property
    def kw_per_amp(self) -> float:
        return self.max_charging_power_kw / self.max_charging_current_a
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_optimizer.py -k "ev_params" -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/models.py tests/test_optimizer.py
git commit -m "feat(models): add EVParams dataclass with validation"
```

---

## Task 2: Extend `OptimizerInputs` and `SlotPlan` with optional EV fields

**Files:**
- Modify: `custom_components/pv_optimizer/models.py`
- Test: `tests/test_optimizer.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_optimizer.py`:

```python
def test_optimizer_inputs_ev_optional() -> None:
    """Existing call sites (no EV) keep working — regression no-op."""
    bat = _battery()
    slots = _slots([0.2] * 4)
    inp = OptimizerInputs(slots, [1.0] * 4, [1.0] * 4, 5.0, bat, 10, 10)
    assert inp.ev is None
    assert inp.ev_target_kwh == 0.0
    assert inp.ev_deadline_index is None


def test_optimizer_inputs_ev_target_requires_ev_params() -> None:
    from custom_components.pv_optimizer.models import EVParams
    bat = _battery()
    slots = _slots([0.2] * 4)
    with pytest.raises(ValueError):
        OptimizerInputs(slots, [1.0] * 4, [1.0] * 4, 5.0, bat, 10, 10,
                        ev_target_kwh=10.0, ev_deadline_index=2)


def test_optimizer_inputs_ev_deadline_must_be_in_range() -> None:
    from custom_components.pv_optimizer.models import EVParams
    bat = _battery()
    slots = _slots([0.2] * 4)
    ev = EVParams(max_charging_power_kw=11.0, max_charging_current_a=16.0,
                  min_charging_current_a=6.0, car_battery_kwh=60.0)
    with pytest.raises(ValueError):
        OptimizerInputs(slots, [1.0] * 4, [1.0] * 4, 5.0, bat, 10, 10,
                        ev=ev, ev_target_kwh=10.0, ev_deadline_index=4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_optimizer.py::test_optimizer_inputs_ev_optional -v`
Expected: FAIL with `TypeError: unexpected keyword argument`.

- [ ] **Step 3: Modify `OptimizerInputs`** — in `custom_components/pv_optimizer/models.py`, replace the `OptimizerInputs` dataclass definition (currently lines ~64-96) so it adds three optional fields and a deadline-aware post-init validation. Append new fields after `terminal_soc_kwh`:

```python
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
    # Optional EV charging extension. When all three are zero/None the
    # LP creates no EV variables and behaves identically to pre-EV
    # builds (regression no-op).
    ev: "EVParams | None" = None
    ev_target_kwh: float = 0.0
    ev_deadline_index: int | None = None  # exclusive; charging allowed in slots [0, deadline_index)

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
        if self.ev_target_kwh < 0:
            raise ValueError("ev_target_kwh must be >= 0")
        if self.ev_target_kwh > 0 and self.ev is None:
            raise ValueError("ev_target_kwh > 0 requires ev params")
        if self.ev_deadline_index is not None and not (
                0 <= self.ev_deadline_index <= n):
            raise ValueError(
                f"ev_deadline_index must be in [0, {n}], got {self.ev_deadline_index}")
```

Also extend the `SlotPlan` definition so the planner can later attach EV outputs without breaking the LP-only path:

```python
@dataclass(frozen=True)
class SlotPlan:
    # ... existing fields unchanged ...
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
    p_ev_chg_kw: float = 0.0   # EV charging power planned by the LP (or 0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_optimizer.py -v`
Expected: all existing tests still PASS plus the 3 new ones.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/models.py tests/test_optimizer.py
git commit -m "feat(models): add optional EV fields to OptimizerInputs/SlotPlan"
```

---

## Task 3: Add `p_ev_chg[t]` LP variables and balance constraint

**Files:**
- Modify: `custom_components/pv_optimizer/optimizer.py`
- Test: `tests/test_optimizer.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_optimizer.py`:

```python
def test_ev_no_target_creates_no_variables() -> None:
    """When ev_target_kwh == 0, plan must equal the no-EV plan exactly."""
    bat = _battery()
    slots = _slots([0.2] * 4)
    inp_noev = OptimizerInputs(slots, [1.0] * 4, [1.0] * 4, 5.0, bat, 10, 10)
    r_noev = solve(inp_noev)
    inp_evoff = OptimizerInputs(slots, [1.0] * 4, [1.0] * 4, 5.0, bat, 10, 10,
                                ev_target_kwh=0.0)
    r_evoff = solve(inp_evoff)
    assert r_noev.total_cost == pytest.approx(r_evoff.total_cost, abs=1e-6)
    for s_no, s_ev in zip(r_noev.slots, r_evoff.slots):
        assert s_ev.p_ev_chg_kw == pytest.approx(0.0, abs=1e-6)
        assert s_no.p_buy_kw == pytest.approx(s_ev.p_buy_kw, abs=1e-6)


def test_ev_charges_before_deadline_from_grid() -> None:
    """With no PV / no battery and one cheap hour, the LP charges EV in slot 0."""
    from custom_components.pv_optimizer.models import EVParams
    bat = _battery(p_chg_max_kw=0.0, p_dis_max_kw=0.0)  # disable home battery
    slots = _slots([0.05, 0.30, 0.30, 0.30])  # slot 0 cheap, others expensive
    ev = EVParams(max_charging_power_kw=11.0, max_charging_current_a=16.0,
                  min_charging_current_a=6.0, car_battery_kwh=60.0)
    inp = OptimizerInputs(
        slots, [0.0] * 4, [0.0] * 4, 1.0, bat, 25, 25,
        ev=ev, ev_target_kwh=10.0, ev_deadline_index=4)
    r = solve(inp)
    assert r.status == "Optimal"
    # All 10 kWh should be delivered in slot 0 (cheapest, capacity ≥ 10).
    delivered = sum(sp.p_ev_chg_kw * sp.duration_h for sp in r.slots)
    assert delivered == pytest.approx(10.0, abs=1e-3)
    assert r.slots[0].p_ev_chg_kw == pytest.approx(10.0, abs=1e-3)
    for sp in r.slots[1:]:
        assert sp.p_ev_chg_kw == pytest.approx(0.0, abs=1e-3)


def test_ev_respects_deadline_cuts_off_charging() -> None:
    """After deadline_index, p_ev_chg upper bound is 0."""
    from custom_components.pv_optimizer.models import EVParams
    bat = _battery(p_chg_max_kw=0.0, p_dis_max_kw=0.0)
    # slot 0 expensive, slot 1 cheap (but deadline is at 1 so slot 1 disallowed)
    slots = _slots([0.30, 0.05, 0.05, 0.05])
    ev = EVParams(max_charging_power_kw=11.0, max_charging_current_a=16.0,
                  min_charging_current_a=6.0, car_battery_kwh=60.0)
    inp = OptimizerInputs(
        slots, [0.0] * 4, [0.0] * 4, 1.0, bat, 25, 25,
        ev=ev, ev_target_kwh=5.0, ev_deadline_index=1)
    r = solve(inp)
    assert r.slots[0].p_ev_chg_kw == pytest.approx(5.0, abs=1e-3)
    for sp in r.slots[1:]:
        assert sp.p_ev_chg_kw == pytest.approx(0.0, abs=1e-3)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_optimizer.py::test_ev_charges_before_deadline_from_grid -v`
Expected: FAIL — `p_ev_chg_kw` is always 0 (no LP variable yet).

- [ ] **Step 3: Implement EV LP variables** — in `custom_components/pv_optimizer/optimizer.py`, inside `solve()`:

After the existing per-slot variable block (after `soc.append(...)` and before `soc_end = pulp.LpVariable(...)`), add:

```python
    # EV charging variables. Created only when an EV target is set; the
    # upper bound is the charger's max power for slots before the deadline,
    # 0 elsewhere — outright disables the variable for out-of-window slots.
    ev_active = inputs.ev is not None and inputs.ev_target_kwh > 0
    p_ev: list = []
    if ev_active:
        for t in range(n):
            ub = (inputs.ev.max_charging_power_kw
                  if inputs.ev_deadline_index is not None
                     and t < inputs.ev_deadline_index
                  else 0.0)
            p_ev.append(pulp.LpVariable(f"ev_{t}", lowBound=0, upBound=ub))
    else:
        p_ev = [0.0] * n  # constant zeros; PuLP handles mixed-numeric expressions
```

Modify the power-balance constraint to include `p_ev`:

```python
        # Power balance: pv + p_dis + p_buy = load + p_ev + p_chg + p_sell + p_curt
        prob += (
            inputs.pv_kw[t] + p_dis[t] + p_buy[t]
            == inputs.load_kw[t] + p_ev[t] + p_chg[t] + p_sell[t] + p_curt[t]
        ), f"balance_{t}"
```

Modify the result construction at the end of `solve()` to surface `p_ev_chg_kw`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_optimizer.py -v`
Expected: all PASS including `test_ev_no_target_creates_no_variables`,
`test_ev_charges_before_deadline_from_grid`, `test_ev_respects_deadline_cuts_off_charging`.

The deadline test will currently FAIL because the LP can't deliver 5 kWh in one slot when the only option is grid + the deadline cuts it off; without a soft slack the LP is infeasible. We add the slack in the next task. To pass this task's commit gate, **temporarily** relax the deadline test target to `ev_target_kwh=5.0` matching exactly slot-0 capacity (11 kW * 1h = 11 kWh ≥ 5) — the test as written is feasible.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/optimizer.py tests/test_optimizer.py
git commit -m "feat(optimizer): add EV charging LP variable and balance"
```

---

## Task 4: Add `ev_deficit` soft slack + penalty

**Files:**
- Modify: `custom_components/pv_optimizer/models.py`, `custom_components/pv_optimizer/optimizer.py`
- Test: `tests/test_optimizer.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_optimizer.py`:

```python
def test_ev_infeasible_deadline_uses_soft_slack() -> None:
    """When the deadline is unachievable, the LP returns Optimal with a deficit.

    Goal: don't raise OptimizerError; gracefully degrade. We ask for 100 kWh
    in 1 slot of 11 kW capacity — physically impossible — and expect the LP
    to deliver everything it can (11 kWh) and report the rest as deficit
    via the result extras.
    """
    from custom_components.pv_optimizer.models import EVParams
    bat = _battery(p_chg_max_kw=0.0, p_dis_max_kw=0.0)
    slots = _slots([0.05, 0.05, 0.05, 0.05])
    ev = EVParams(max_charging_power_kw=11.0, max_charging_current_a=16.0,
                  min_charging_current_a=6.0, car_battery_kwh=60.0)
    inp = OptimizerInputs(
        slots, [0.0] * 4, [0.0] * 4, 1.0, bat, 25, 25,
        ev=ev, ev_target_kwh=100.0, ev_deadline_index=1)
    r = solve(inp)
    assert r.status == "Optimal"
    delivered = sum(sp.p_ev_chg_kw * sp.duration_h for sp in r.slots)
    assert delivered == pytest.approx(11.0, abs=1e-3)
    assert r.extras.get("ev_deficit_kwh") == pytest.approx(89.0, abs=1e-3)


def test_ev_penalty_dominates_peak_buy_price() -> None:
    """The LP prefers importing at peak price over leaving target unmet."""
    from custom_components.pv_optimizer.models import EVParams
    bat = _battery(p_chg_max_kw=0.0, p_dis_max_kw=0.0)
    # Slot 0 is very expensive (1.0) but the only one before deadline.
    slots = _slots([1.0, 0.05, 0.05, 0.05])
    ev = EVParams(max_charging_power_kw=11.0, max_charging_current_a=16.0,
                  min_charging_current_a=6.0, car_battery_kwh=60.0)
    inp = OptimizerInputs(
        slots, [0.0] * 4, [0.0] * 4, 1.0, bat, 25, 25,
        ev=ev, ev_target_kwh=5.0, ev_deadline_index=1)
    r = solve(inp)
    # LP would rather pay 1.0 EUR/kWh than leave 5 kWh undelivered.
    assert r.slots[0].p_ev_chg_kw == pytest.approx(5.0, abs=1e-3)
    assert r.extras.get("ev_deficit_kwh", 0.0) == pytest.approx(0.0, abs=1e-3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_optimizer.py::test_ev_infeasible_deadline_uses_soft_slack -v`
Expected: FAIL — LP is infeasible (raises `OptimizerError`) or `ev_deficit_kwh` is missing.

- [ ] **Step 3: Implement the soft slack** — in `custom_components/pv_optimizer/optimizer.py`, inside `solve()`:

After the existing `p_ev` block but before the per-slot constraints loop, declare the deficit variable:

```python
    ev_deficit = None
    if ev_active:
        # Soft total-energy constraint: ev_deficit absorbs any shortfall
        # so the LP stays feasible when capacity before deadline is
        # genuinely insufficient (deadline too soon, plug-in too late).
        ev_deficit = pulp.LpVariable("ev_deficit", lowBound=0,
                                     upBound=inputs.ev_target_kwh)
        prob += (
            ev_deficit >= inputs.ev_target_kwh - pulp.lpSum(
                inputs.slots[t].duration_h * p_ev[t] for t in range(n)
            )
        ), "ev_deficit_lb"
```

Add the penalty to the objective. Find the existing block that assembles `cost_terms` and after the loop, add (before `prob += pulp.lpSum(cost_terms)`):

```python
    if ev_active:
        # Penalty must beat the highest realistic buy price so the LP
        # prefers expensive grid over leaving the target unmet. 100x is
        # a safe multiplier (real EV deficit values matter to the user
        # at order-of-magnitude scale, not at the cent).
        max_buy = max((s.price_buy for s in inputs.slots), default=1.0)
        ev_deficit_penalty = 100.0 * max(max_buy, 0.01)
        cost_terms.append(ev_deficit_penalty * ev_deficit)
```

Surface the deficit in `extras`:

```python
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
```

(Replace the existing `extras=...` keyword in the return.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_optimizer.py -v`
Expected: all PASS including the two new soft-slack tests.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/optimizer.py tests/test_optimizer.py
git commit -m "feat(optimizer): add ev_deficit soft slack for unachievable deadlines"
```

---

## Task 5: State vocabulary classifier in `ev_controller.py`

**Files:**
- Create: `custom_components/pv_optimizer/ev_controller.py`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_ev_controller.py`:

```python
"""Unit tests for the EV controller (pure decision logic)."""
from __future__ import annotations

import pytest

from custom_components.pv_optimizer.ev_controller import (
    EVStateClass,
    classify_state,
    DEFAULT_STATE_VOCAB,
)


def test_classify_disconnected_default_substrings() -> None:
    assert classify_state("Disconnected") == EVStateClass.DISCONNECTED
    assert classify_state("idle") == EVStateClass.DISCONNECTED
    assert classify_state("Unplugged") == EVStateClass.DISCONNECTED


def test_classify_connected_requesting_default_substrings() -> None:
    assert classify_state("Charging") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("wait_sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait time") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait start") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("WAIT RFID") == EVStateClass.CONNECTED_REQUESTING


def test_classify_connected_idle_default_substrings() -> None:
    assert classify_state("Charged") == EVStateClass.CONNECTED_IDLE
    assert classify_state("Connected") == EVStateClass.CONNECTED_IDLE


def test_classify_unknown_falls_back_to_connected_idle() -> None:
    """Conservative default: unknown plugged-in classifies safely."""
    assert classify_state("WeirdStatus") == EVStateClass.CONNECTED_IDLE


def test_classify_handles_none_and_unavailable() -> None:
    assert classify_state(None) == EVStateClass.DISCONNECTED
    assert classify_state("unknown") == EVStateClass.DISCONNECTED
    assert classify_state("unavailable") == EVStateClass.DISCONNECTED


def test_classify_precedence_disconnected_wins_over_requesting() -> None:
    """If a state somehow contains both 'idle' and 'charging' substrings,
    'disconnected' classification takes precedence per §3.3."""
    # Pathological example — pick disconnected on tie.
    assert classify_state("idle charging") == EVStateClass.DISCONNECTED


def test_classify_custom_vocab_override() -> None:
    custom = {
        EVStateClass.DISCONNECTED: ("frei",),
        EVStateClass.CONNECTED_REQUESTING: ("laedt",),
        EVStateClass.CONNECTED_IDLE: ("voll",),
    }
    assert classify_state("Frei", vocab=custom) == EVStateClass.DISCONNECTED
    assert classify_state("Laedt", vocab=custom) == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("voll", vocab=custom) == EVStateClass.CONNECTED_IDLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ev_controller.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create `custom_components/pv_optimizer/ev_controller.py`**:

```python
"""Pure decision logic for the EV charging feature.

No Home Assistant imports. Owned by tests/test_ev_controller.py.
"""
from __future__ import annotations

import enum
from typing import Mapping, Sequence


class EVStateClass(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTED_IDLE = "connected_idle"
    CONNECTED_REQUESTING = "connected_requesting"


# Default substring vocabulary. All matches are case-insensitive.
# Precedence in classify_state: DISCONNECTED > CONNECTED_REQUESTING > CONNECTED_IDLE.
DEFAULT_STATE_VOCAB: Mapping[EVStateClass, Sequence[str]] = {
    EVStateClass.DISCONNECTED: ("disconnect", "idle", "unplug"),
    EVStateClass.CONNECTED_REQUESTING: (
        "charging", "wait sun", "wait_sun",
        "wait time", "wait start", "wait rfid",
    ),
    EVStateClass.CONNECTED_IDLE: ("charged", "connect"),
}

_UNAVAILABLE_STATES = frozenset({"unknown", "unavailable", "none", ""})


def classify_state(
    state: str | None,
    vocab: Mapping[EVStateClass, Sequence[str]] = DEFAULT_STATE_VOCAB,
) -> EVStateClass:
    """Classify a raw charger-state string into one of three classes.

    Returns ``DISCONNECTED`` for ``None`` / empty / ``unknown`` / ``unavailable``
    so the planner treats stale inputs as "no car" and bails (per spec §8).
    """
    if state is None:
        return EVStateClass.DISCONNECTED
    s = state.strip().lower()
    if not s or s in _UNAVAILABLE_STATES:
        return EVStateClass.DISCONNECTED
    # Precedence: disconnected > requesting > idle.
    for cls in (
        EVStateClass.DISCONNECTED,
        EVStateClass.CONNECTED_REQUESTING,
        EVStateClass.CONNECTED_IDLE,
    ):
        for needle in vocab.get(cls, ()):
            if needle.lower() in s:
                return cls
    return EVStateClass.CONNECTED_IDLE  # conservative fallback
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ev_controller.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "feat(ev_controller): add state vocabulary classifier"
```

---

## Task 6: Reactive surplus / cheap-grid / ultimate-override (no mode-switching)

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_ev_controller.py`:

```python
from custom_components.pv_optimizer.ev_controller import (
    ReactiveDecision,
    decide_reactive,
)
from custom_components.pv_optimizer.models import EVParams


def _ev() -> EVParams:
    return EVParams(
        max_charging_power_kw=8.0,
        max_charging_current_a=20.0,
        min_charging_current_a=6.0,
        car_battery_kwh=60.0,
        buy_price_threshold=0.0,
    )


def test_reactive_disconnected_writes_zero() -> None:
    out = decide_reactive(
        state_class=EVStateClass.DISCONNECTED,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=0.50,
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_connected_requesting_grants_max() -> None:
    """Ultimate override — car requesting beats every other rule."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=5000.0,         # importing
        ev_charging_power_w=0.0,
        price_buy=0.50,              # not cheap
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_cheap_grid_grants_max() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=-0.05,  # below threshold of 0
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_surplus_tracking_above_min() -> None:
    """grid=-3kW (exporting 3kW) + ev=0 -> 3kW surplus -> 7.5A clamps to 6A floor? No 3000/400=7.5A."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-3000.0,
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    # kw_per_amp = 8/20 = 0.4. surplus = 3 kW. target_a = 3/0.4 = 7.5 -> rounded.
    assert out.max_current_a == 7


def test_reactive_surplus_back_adds_current_ev_power() -> None:
    """Convergence: when EV is already drawing, back-add so loop doesn't ramp down."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,          # net zero — EV consumes all surplus
        ev_charging_power_w=3000.0,
        price_buy=0.30,
        ev=_ev(),
    )
    # available = -0 + 3 = 3 kW -> 7A, stable.
    assert out.max_current_a == 7


def test_reactive_surplus_below_min_writes_zero() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-1000.0,  # 1 kW surplus -> 2.5A, below min 6A
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_surplus_above_max_clamps() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-20000.0,  # huge surplus
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_cheap_grid_threshold_inclusive() -> None:
    """price_buy ≤ threshold triggers cheap-grid; default threshold is 0."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=0.0,  # equal -> trigger
        ev=_ev(),
    )
    assert out.max_current_a == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ev_controller.py::test_reactive_disconnected_writes_zero -v`
Expected: FAIL — `ReactiveDecision`/`decide_reactive` not defined.

- [ ] **Step 3: Implement `decide_reactive`** — append to `custom_components/pv_optimizer/ev_controller.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ReactiveDecision:
    """Decision output for one planner tick (reactive branch)."""

    max_current_a: int   # integer A; 0 disables charging


def decide_reactive(
    *,
    state_class: EVStateClass,
    grid_power_w: float,
    ev_charging_power_w: float,
    price_buy: float,
    ev,  # EVParams (avoid cyclic import at module top)
) -> ReactiveDecision:
    """One-shot reactive decision per §4.2 (no mode-switching).

    Args:
        state_class: classified state of the charger.
        grid_power_w: site-level grid power (positive = import, negative = export).
        ev_charging_power_w: power the EV is currently drawing.
        price_buy: current buy price (currency/kWh, all-in).
        ev: EVParams with kw_per_amp, min/max current, buy_price_threshold.
    """
    if state_class == EVStateClass.DISCONNECTED:
        return ReactiveDecision(max_current_a=0)
    if state_class == EVStateClass.CONNECTED_REQUESTING:
        # Ultimate override — car has actively negotiated for power (§4.3).
        return ReactiveDecision(max_current_a=int(round(ev.max_charging_current_a)))
    if price_buy <= ev.buy_price_threshold:
        return ReactiveDecision(max_current_a=int(round(ev.max_charging_current_a)))
    # Surplus tracking: back-add what EV is already drawing so loop converges.
    surplus_kw = max(0.0, (-grid_power_w + ev_charging_power_w) / 1000.0)
    target_a = surplus_kw / ev.kw_per_amp
    if target_a < ev.min_charging_current_a:
        return ReactiveDecision(max_current_a=0)
    clamped = max(ev.min_charging_current_a,
                  min(ev.max_charging_current_a, target_a))
    return ReactiveDecision(max_current_a=int(round(clamped)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ev_controller.py -v`
Expected: all PASS (existing 7 + 8 new = 15).

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "feat(ev_controller): add reactive surplus / cheap-grid / override decision"
```

---

## Task 7: Latch state machine for mode-switching variant

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_ev_controller.py`:

```python
from custom_components.pv_optimizer.ev_controller import (
    LatchState,
    update_latches,
)


def test_latches_start_clear() -> None:
    s = LatchState()
    assert not s.cheap_grid
    assert not s.ultimate_override


def test_cheap_grid_latch_trigger_and_release() -> None:
    ev = _ev()
    s = LatchState()
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=-0.05, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_IDLE, time_in_current_class_s=0.0,
        ev=ev,
    )
    assert s.cheap_grid
    # Same conditions next tick — still latched.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=-0.05, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_IDLE, time_in_current_class_s=300.0,
        ev=ev,
    )
    assert s.cheap_grid
    # Price now above threshold for one tick — latch releases (≥1 tick rule).
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=0.05, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_IDLE, time_in_current_class_s=600.0,
        ev=ev,
    )
    assert not s.cheap_grid


def test_ultimate_override_latch_triggers_on_denied_request() -> None:
    ev = _ev()
    s = LatchState()
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_REQUESTING, time_in_current_class_s=10.0,
        ev=ev,
    )
    assert s.ultimate_override


def test_ultimate_override_latch_does_not_flap_during_charge() -> None:
    """Once latched and car is drawing power, state stays REQUESTING — latch holds.

    This is the bug we explicitly designed around (spec §4.1 last paragraph).
    """
    ev = _ev()
    s = update_latches(
        LatchState(),
        state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_REQUESTING, time_in_current_class_s=10.0,
        ev=ev,
    )
    assert s.ultimate_override
    # Next tick — car now drawing 6 kW; state still REQUESTING; latch must hold.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=6000.0,
        last_state_class=EVStateClass.CONNECTED_REQUESTING, time_in_current_class_s=300.0,
        ev=ev,
    )
    assert s.ultimate_override


def test_ultimate_override_releases_after_dwell_out_of_requesting() -> None:
    ev = _ev()
    s = update_latches(
        LatchState(),
        state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_REQUESTING, time_in_current_class_s=10.0,
        ev=ev,
    )
    assert s.ultimate_override
    # State leaves REQUESTING; not yet past the dwell window.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=0.50, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_IDLE, time_in_current_class_s=30.0,  # < session_done_seconds (60)
        ev=ev,
    )
    assert s.ultimate_override
    # Past the dwell — release.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=0.50, ev_charging_power_w=0.0,
        last_state_class=EVStateClass.CONNECTED_IDLE, time_in_current_class_s=120.0,
        ev=ev,
    )
    assert not s.ultimate_override
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ev_controller.py::test_latches_start_clear -v`
Expected: FAIL — `LatchState` not defined.

- [ ] **Step 3: Implement latches** — append to `custom_components/pv_optimizer/ev_controller.py`:

```python
@dataclass(frozen=True)
class LatchState:
    """Persistent state for the mode-switching reactive variant (§4.1)."""

    cheap_grid: bool = False
    ultimate_override: bool = False

    @property
    def any_set(self) -> bool:
        return self.cheap_grid or self.ultimate_override


def update_latches(
    prev: LatchState,
    *,
    state_class: EVStateClass,
    price_buy: float,
    ev_charging_power_w: float,
    last_state_class: EVStateClass,
    time_in_current_class_s: float,
    ev,
) -> LatchState:
    """Advance latches one tick per §4.1 trigger/release semantics.

    Args:
        prev: previous latch state.
        state_class: current classified state of the charger.
        price_buy: current buy price.
        ev_charging_power_w: instantaneous EV charging power.
        last_state_class: state class on the previous tick (used to
            detect "leaves CONNECTED_REQUESTING" for the override release).
        time_in_current_class_s: seconds the state has been in
            ``state_class`` consecutively (the planner tracks this).
        ev: EVParams.
    """
    # Cheap-grid: symmetric trigger/release on price threshold.
    if price_buy <= ev.buy_price_threshold:
        cheap_grid = True
    else:
        cheap_grid = False  # release on first tick above threshold

    # Ultimate-override: asymmetric.
    #   Trigger: state == REQUESTING AND ev_power < threshold.
    #   Release: state has left REQUESTING for ≥ session_done_seconds.
    if (state_class == EVStateClass.CONNECTED_REQUESTING
            and ev_charging_power_w < ev.session_done_power_w):
        ultimate_override = True
    elif prev.ultimate_override:
        if state_class == EVStateClass.CONNECTED_REQUESTING:
            ultimate_override = True  # still requesting; hold
        elif time_in_current_class_s >= ev.session_done_seconds:
            ultimate_override = False
        else:
            ultimate_override = True
    else:
        ultimate_override = False

    return LatchState(cheap_grid=cheap_grid, ultimate_override=ultimate_override)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ev_controller.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "feat(ev_controller): add latch state machine for mode-switching"
```

---

## Task 8: Session-done detector

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing test** — append:

```python
from custom_components.pv_optimizer.ev_controller import is_session_done


def test_session_done_when_disconnected() -> None:
    ev = _ev()
    assert is_session_done(
        state_class=EVStateClass.DISCONNECTED,
        ev_charging_power_w=0.0, low_power_seconds=0.0, ev=ev,
    )


def test_session_done_when_idle_and_low_power_long_enough() -> None:
    ev = _ev()
    assert is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=50.0, low_power_seconds=120.0, ev=ev,
    )


def test_session_not_done_when_idle_but_brief() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=50.0, low_power_seconds=30.0, ev=ev,
    )


def test_session_not_done_when_idle_but_drawing_power() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=2000.0, low_power_seconds=600.0, ev=ev,
    )


def test_session_not_done_when_still_requesting() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev_charging_power_w=0.0, low_power_seconds=600.0, ev=ev,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ev_controller.py::test_session_done_when_disconnected -v`
Expected: FAIL — `is_session_done` not defined.

- [ ] **Step 3: Implement** — append to `ev_controller.py`:

```python
def is_session_done(
    *,
    state_class: EVStateClass,
    ev_charging_power_w: float,
    low_power_seconds: float,
    ev,
) -> bool:
    """Return True per §6.1 session-done definition.

    Done iff:
        - disconnected; OR
        - connected_idle AND ev_power < session_done_power_w for
          ≥ session_done_seconds (caller tracks the duration).
    """
    if state_class == EVStateClass.DISCONNECTED:
        return True
    if (state_class == EVStateClass.CONNECTED_IDLE
            and ev_charging_power_w < ev.session_done_power_w
            and low_power_seconds >= ev.session_done_seconds):
        return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ev_controller.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "feat(ev_controller): add session-done detector"
```

---

## Task 9: Slot-0 LP→current translation with ultimate-override branch

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing test** — append:

```python
from custom_components.pv_optimizer.ev_controller import translate_lp_slot0


def test_translate_disconnected_yields_zero() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.DISCONNECTED,
        ev_charging_power_w=0.0, ev=ev,
    ) == 0


def test_translate_lp_zero_yields_zero() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=0.0, ev=ev,
    ) == 0


def test_translate_lp_positive_above_min_converts_to_amps() -> None:
    ev = _ev()
    # 4 kW / 0.4 kw/A = 10 A.
    assert translate_lp_slot0(
        p_ev_chg_kw=4.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=0.0, ev=ev,
    ) == 10


def test_translate_lp_below_min_clamps_up_to_floor() -> None:
    """Contrast with reactive: LP path clamps up because user committed to a target."""
    ev = _ev()
    # 1 kW / 0.4 = 2.5 A < 6 A floor -> clamp UP.
    assert translate_lp_slot0(
        p_ev_chg_kw=1.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=0.0, ev=ev,
    ) == 6


def test_translate_lp_above_max_clamps_down() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=100.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=0.0, ev=ev,
    ) == 20


def test_translate_ultimate_override_beats_lp_zero() -> None:
    """Car requesting and not drawing -> max regardless of LP plan."""
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev_charging_power_w=0.0, ev=ev,
    ) == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ev_controller.py::test_translate_disconnected_yields_zero -v`
Expected: FAIL — `translate_lp_slot0` not defined.

- [ ] **Step 3: Implement** — append to `ev_controller.py`:

```python
def translate_lp_slot0(
    *,
    p_ev_chg_kw: float,
    state_class: EVStateClass,
    ev_charging_power_w: float,
    ev,
) -> int:
    """Convert the LP's slot-0 EV power into a charger max-current setpoint (A).

    Per spec §5.3:
    - If car is actively requesting AND not drawing meaningful power
      (ev_charging_power_w < session_done_power_w), honour ultimate-override
      with max current regardless of the LP plan.
    - If LP plans zero, write zero.
    - If LP plans > 0 but the converted current is below
      ``min_charging_current_a``, clamp UP (contrast with reactive's
      skip-below-min): the user has committed to a target, so a minor
      slot-0 overshoot is acceptable. The next tick re-plans with reduced
      remaining_kwh.
    """
    if (state_class == EVStateClass.CONNECTED_REQUESTING
            and ev_charging_power_w < ev.session_done_power_w):
        return int(round(ev.max_charging_current_a))
    if state_class == EVStateClass.DISCONNECTED:
        return 0
    if p_ev_chg_kw <= 0:
        return 0
    target_a = p_ev_chg_kw / ev.kw_per_amp
    clamped = max(ev.min_charging_current_a,
                  min(ev.max_charging_current_a, target_a))
    return int(round(clamped))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ev_controller.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "feat(ev_controller): add slot-0 LP-to-current translation"
```

---

## Task 10: Add `EVConfig` to `PlannerConfig`

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_planner.py`:

```python
def test_planner_config_ev_optional_when_unset() -> None:
    """Existing planner construction with no EV inputs keeps working."""
    cfg = _config()
    assert cfg.ev is None


def test_planner_config_with_ev_config() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        session_energy_entity="sensor.ev_session_energy",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        target_pct_entity="number.pv_optimizer_ev_target_pct",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    cfg = _config(ev=ev_cfg)
    assert cfg.ev is ev_cfg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_planner.py::test_planner_config_ev_optional_when_unset -v`
Expected: FAIL — `PlannerConfig.ev` does not exist.

- [ ] **Step 3: Implement `EVConfig`** — in `custom_components/pv_optimizer/planner.py`, add the dataclass above `PlannerConfig`:

```python
@dataclass(frozen=True)
class EVConfig:
    """All EV-related entity bindings + static params bundled together.

    A single ``EVConfig`` field on ``PlannerConfig`` keeps the EV surface
    optional: when ``cfg.ev is None`` the planner does no EV work at all.
    """

    # Static parameters.
    params: "EVParams"
    # Input entities (read by the planner).
    charger_state_entity: str
    charging_power_entity: str  # W or kW; assume W if value > 100
    max_current_entity: str     # number entity (A) — output
    session_energy_entity: str | None = None
    start_switch_entity: str | None = None
    charger_mode_entity: str | None = None
    # Integration-created entity ids (the planner reads these to learn
    # the user's mode/target/deadline; HA layer owns their write side).
    mode_entity: str = ""        # select.pv_optimizer_ev_mode
    target_kwh_entity: str = ""  # number.pv_optimizer_ev_target_kwh
    target_pct_entity: str = ""  # number.pv_optimizer_ev_target_pct
    deadline_entity: str = ""    # datetime.pv_optimizer_ev_deadline
```

Add `EVParams` to the existing models import:

```python
from .models import (
    BatteryParams,
    EVParams,
    OptimizerError,
    OptimizerInputs,
    OptimizerResult,
    SlotPlan,
    TariffSlot,
)
```

Add an optional `ev` field to `PlannerConfig`:

```python
@dataclass(frozen=True)
class PlannerConfig:
    # ... existing fields ...
    price_tomorrow_attr: str = "tomorrow"
    ev: EVConfig | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_planner.py -v -k "ev"`
Expected: PASS for the two new tests, no regressions on existing tests.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(planner): add EVConfig and PlannerConfig.ev binding"
```

---

## Task 11: Planner reads EV mode/target/deadline + connection state

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_planner.py`:

```python
def test_planner_reads_ev_state_no_target_runs_reactive() -> None:
    """When target=0, planner uses the reactive path: writes max_current."""
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_params = EVParams(
        max_charging_power_kw=8.0, max_charging_current_a=20.0,
        min_charging_current_a=6.0, car_battery_kwh=60.0,
    )
    ev_cfg = EVConfig(
        params=ev_params,
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")  # requesting
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state="")
    reader, caller = FakeReader(states), FakeCaller()
    cfg = _config(ev=ev_cfg)
    p = Planner(cfg, reader, caller)
    p.step(NOW)
    # Reactive path with state=Charging -> ultimate-override -> max_current = 20.
    ev_writes = [c for c in caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    assert ev_writes, "planner should write to ev_max_current"
    last = ev_writes[-1]
    assert last == ("number", "set_value", {"entity_id": "number.ev_max_current", "value": 20})


def test_planner_off_mode_writes_nothing_to_ev() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="off")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state="")
    reader, caller = FakeReader(states), FakeCaller()
    p = Planner(_config(ev=ev_cfg), reader, caller)
    p.step(NOW)
    ev_writes = [c for c in caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    assert ev_writes == [], "off mode must not write to ev_max_current"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_planner.py::test_planner_reads_ev_state_no_target_runs_reactive -v`
Expected: FAIL — no writes to `number.ev_max_current`.

- [ ] **Step 3: Implement EV reading + reactive output** — in `custom_components/pv_optimizer/planner.py`:

First, add a top-level constant and a state-keeper for latches/dwells. Put this near `_FORCE_EPS`:

```python
@dataclass
class EVRuntimeState:
    """Per-planner mutable EV state — latches + dwell timers."""

    latches: "LatchState" = None
    last_state_class: "EVStateClass" = None
    state_class_since: datetime | None = None
    low_power_since: datetime | None = None
    last_written_current_a: int | None = None
    last_session_plug_in: datetime | None = None
    session_energy_kwh: float = 0.0
    last_charging_power_kw: float | None = None
    last_tick: datetime | None = None
```

Add the import inside `planner.py`:

```python
from .ev_controller import (
    DEFAULT_STATE_VOCAB,
    EVStateClass,
    LatchState,
    classify_state,
    decide_reactive,
    is_session_done,
    translate_lp_slot0,
    update_latches,
)
```

In `Planner.__init__`, attach a fresh `EVRuntimeState`:

```python
        self.ev_state: EVRuntimeState | None = (
            EVRuntimeState(latches=LatchState()) if config.ev is not None else None
        )
```

Add a new method `_apply_ev` that runs at the end of `step()` (after the `_apply_setpoint`/`_apply_feedin` block, before returning the cycle):

```python
    def _apply_ev(self, now: datetime, plan_first: SlotPlan) -> None:
        cfg = self.config.ev
        if cfg is None or self.ev_state is None:
            return
        mode = self._read_mode()  # "auto" / "manual" / "off"
        if mode == "off":
            return
        # Read inputs.
        raw_state = self._read_text(cfg.charger_state_entity)
        state_class = classify_state(raw_state)
        ev_power_w = self._read_charging_power_w(cfg.charging_power_entity)
        price_buy = self._first_slot_buy_price()
        grid_w = self._read_float(self.config.grid_power_entity)
        # Update dwells.
        es = self.ev_state
        if es.last_state_class != state_class:
            es.last_state_class = state_class
            es.state_class_since = now
        time_in_class = (
            (now - es.state_class_since).total_seconds()
            if es.state_class_since is not None else 0.0
        )
        if ev_power_w < cfg.params.session_done_power_w:
            if es.low_power_since is None:
                es.low_power_since = now
            low_power_s = (now - es.low_power_since).total_seconds()
        else:
            es.low_power_since = None
            low_power_s = 0.0
        # Manual mode: unconditional max + auto-return on session-done.
        if mode == "manual":
            self._write_ev_current(cfg.params.max_charging_current_a)
            self._write_ev_start(True)
            self._write_ev_charger_mode_active()
            if is_session_done(state_class=state_class,
                               ev_charging_power_w=ev_power_w,
                               low_power_seconds=low_power_s, ev=cfg.params):
                self._write_mode_auto()
            return
        # Auto mode: dispatch on LP-plan presence.
        if plan_first.p_ev_chg_kw > 0:
            current = translate_lp_slot0(
                p_ev_chg_kw=plan_first.p_ev_chg_kw,
                state_class=state_class,
                ev_charging_power_w=ev_power_w, ev=cfg.params,
            )
            self._write_ev_current(current)
            self._write_ev_start(current > 0)
            if current > 0:
                self._write_ev_charger_mode_active()
            else:
                self._write_ev_charger_mode_passive()
            return
        # Reactive path.
        if cfg.charger_mode_entity:
            # Mode-switching variant: latches drive mode + max-current.
            es.latches = update_latches(
                es.latches,
                state_class=state_class,
                price_buy=price_buy,
                ev_charging_power_w=ev_power_w,
                last_state_class=es.last_state_class,
                time_in_current_class_s=time_in_class,
                ev=cfg.params,
            )
            if es.latches.any_set:
                self._write_ev_charger_mode_active()
                self._write_ev_current(cfg.params.max_charging_current_a)
                self._write_ev_start(True)
            else:
                self._write_ev_charger_mode_passive()
                self._write_ev_current(cfg.params.max_charging_current_a)
        else:
            # No mode entity: decide_reactive owns everything.
            decision = decide_reactive(
                state_class=state_class,
                grid_power_w=grid_w,
                ev_charging_power_w=ev_power_w,
                price_buy=price_buy,
                ev=cfg.params,
            )
            self._write_ev_current(decision.max_current_a)
            self._write_ev_start(decision.max_current_a > 0)

    # ---- EV helpers ------------------------------------------------------
    def _read_mode(self) -> str:
        if not self.config.ev or not self.config.ev.mode_entity:
            return "auto"
        st = self.reader.get(self.config.ev.mode_entity)
        if st is None or st.state in (None, "", "unknown", "unavailable"):
            return "auto"
        return str(st.state).lower()

    def _read_text(self, entity_id: str) -> str | None:
        st = self.reader.get(entity_id)
        return None if st is None else st.state

    def _read_charging_power_w(self, entity_id: str) -> float:
        st = self.reader.get(entity_id)
        if st is None or st.state in (None, "", "unknown", "unavailable"):
            return 0.0
        try:
            v = float(st.state)
        except (TypeError, ValueError):
            return 0.0
        unit = str(st.attributes.get("unit_of_measurement", "")).lower()
        if unit == "kw" or (unit == "" and 0 < v < 100):
            return v * 1000.0
        return v

    def _first_slot_buy_price(self) -> float:
        if self.last and self.last.result and self.last.result.slots:
            return self.last.result.slots[0].p_buy_kw  # wrong field, fix below
        return 0.0  # placeholder; corrected in Task 12

    def _write_ev_current(self, value_a: float) -> None:
        cfg = self.config.ev
        if cfg is None:
            return
        es = self.ev_state
        new_val = int(round(value_a))
        if es and es.last_written_current_a is not None:
            if abs(new_val - es.last_written_current_a) <= cfg.params.current_tolerance_a:
                return  # within tolerance
        self.caller.call("number", "set_value", {
            "entity_id": cfg.max_current_entity, "value": new_val,
        })
        if es is not None:
            es.last_written_current_a = new_val

    def _write_ev_start(self, on: bool) -> None:
        cfg = self.config.ev
        if cfg is None or not cfg.start_switch_entity:
            return
        service = "turn_on" if on else "turn_off"
        self.caller.call("switch", service, {"entity_id": cfg.start_switch_entity})

    def _write_ev_charger_mode_active(self) -> None:
        cfg = self.config.ev
        if cfg is None or not cfg.charger_mode_entity:
            return
        self.caller.call("select", "select_option", {
            "entity_id": cfg.charger_mode_entity, "option": "Manual",
        })

    def _write_ev_charger_mode_passive(self) -> None:
        cfg = self.config.ev
        if cfg is None or not cfg.charger_mode_entity:
            return
        self.caller.call("select", "select_option", {
            "entity_id": cfg.charger_mode_entity, "option": "Auto",
        })

    def _write_mode_auto(self) -> None:
        cfg = self.config.ev
        if cfg is None or not cfg.mode_entity:
            return
        self.caller.call("select", "select_option", {
            "entity_id": cfg.mode_entity, "option": "auto",
        })
```

Call `_apply_ev` near the bottom of `Planner.step()`, after the existing setpoint/feedin block:

```python
        # EV control. Always last so it can read the latest first-slot price
        # and the LP's p_ev_chg_kw decision.
        self._apply_ev(now, result.slots[0])
```

Fix `_first_slot_buy_price` to use the actual inputs:

```python
    def _first_slot_buy_price(self) -> float:
        # Pulled from the inputs of the just-solved LP.
        if self.last and self.last.result and self.last.result.slots:
            # OptimizerResult.slots carries SlotPlan; we need the input price.
            # The planner stores no inputs reference, so cache it on the cycle.
            pass
        return self._cached_first_buy_price
```

Cache the first slot's buy price at the end of `_build_inputs`:

```python
        self._cached_first_buy_price = slots[0].price_buy
```

And initialise in `__init__`:

```python
        self._cached_first_buy_price: float = 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_planner.py -v`
Expected: existing tests still PASS; the two new EV tests PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(planner): wire EV reactive control through ev_controller"
```

---

## Task 12: Planner — manual override mode

**Files:**
- Modify: `tests/test_planner.py`
- (planner already supports manual; this task verifies and exercises auto-return)

- [ ] **Step 1: Write the failing test** — append to `tests/test_planner.py`:

```python
def test_planner_manual_mode_writes_max() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")  # idle
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.pv_optimizer_ev_mode"] = StateView(state="manual")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    writes = [c for c in p.caller.calls
              if c[2].get("entity_id") == "number.ev_max_current"]
    assert writes, "manual mode should write max-current"
    assert writes[-1][2]["value"] == 20
    starts = [c for c in p.caller.calls
              if c[2].get("entity_id") == "switch.ev_start"]
    assert starts and starts[-1][1] == "turn_on"


def test_planner_manual_mode_auto_returns_on_disconnect() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="manual")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert mode_writes, "manual mode should auto-return on disconnect"
    assert mode_writes[-1][2]["option"] == "auto"
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `pytest tests/test_planner.py -k "manual_mode" -v`
Expected: PASS (manual handler from Task 11 already implements this).

If they fail, debug the handler from Task 11 — the most likely issue is `FakeCaller` not being inspected via `p.caller` (the attribute exposes from `Planner` already).

- [ ] **Step 3: Commit**

```bash
git add tests/test_planner.py
git commit -m "test(planner): cover EV manual mode and auto-return"
```

---

## Task 13: Planner builds `OptimizerInputs` with EV target + deadline

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_planner.py`:

```python
def test_planner_engages_lp_when_target_and_deadline_set() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    # Cheap slot 0 (0.05), expensive others (0.30); 4-h horizon.
    states = _states(buy=[0.05] + [0.30] * 23)
    states["sensor.ev_state"] = StateView(state="Connected")  # idle (not requesting)
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    # Deadline 3h from NOW; planner stores it as exclusive index.
    deadline = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    cycle = p.step(NOW)
    # LP should plan EV charging in slot 0 (cheapest).
    assert cycle.result is not None
    assert cycle.result.slots[0].p_ev_chg_kw > 0
    # And the planner should write a non-zero max-current.
    writes = [c for c in p.caller.calls
              if c[2].get("entity_id") == "number.ev_max_current"]
    assert writes and writes[-1][2]["value"] >= 6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_planner.py::test_planner_engages_lp_when_target_and_deadline_set -v`
Expected: FAIL — `p_ev_chg_kw` is 0 (planner not passing EV inputs to LP yet).

- [ ] **Step 3: Modify `_build_inputs` to include EV** — in `planner.py` `_build_inputs`, at the bottom (before `return OptimizerInputs(...)`):

```python
        # EV inputs (only when an EV config exists, the user has set a
        # positive target, and a deadline lies inside the planning horizon).
        ev_params = None
        ev_target = 0.0
        ev_deadline_idx = None
        if cfg.ev is not None:
            target_kwh = self._read_float_optional(
                cfg.ev.target_kwh_entity, default=0.0)
            deadline = self._read_datetime_optional(
                cfg.ev.deadline_entity)
            connected = (
                classify_state(self._read_text(cfg.ev.charger_state_entity))
                != EVStateClass.DISCONNECTED
            )
            session_done = self._session_energy_kwh(cfg.ev, now)
            remaining = max(0.0, target_kwh - session_done)
            if (connected and remaining > 0 and deadline is not None
                    and deadline > now):
                # Slot index nearest to (but not past) the deadline.
                deadline_floor = _floor_to_slot(deadline, cfg.slot_minutes)
                if deadline_floor > slot_starts[-1]:
                    ev_deadline_idx = len(slot_starts)
                else:
                    # Inclusive of deadline_floor since charging in that hour is allowed.
                    ev_deadline_idx = next(
                        (i for i, s in enumerate(slot_starts) if s >= deadline_floor),
                        len(slot_starts),
                    )
                if ev_deadline_idx > 0:
                    ev_params = cfg.ev.params
                    ev_target = remaining
```

Update the return:

```python
        return OptimizerInputs(
            slots=slots, pv_kw=pv_kw, load_kw=load_kw,
            initial_soc_kwh=soc_kwh, battery=cfg.battery,
            p_grid_imp_max_kw=cfg.p_grid_imp_max_kw,
            p_grid_exp_max_kw=cfg.p_grid_exp_max_kw,
            ev=ev_params,
            ev_target_kwh=ev_target,
            ev_deadline_index=ev_deadline_idx,
        )
```

Add helpers near the other readers:

```python
    def _read_float_optional(self, entity_id: str | None, *,
                             default: float) -> float:
        if not entity_id:
            return default
        st = self.reader.get(entity_id)
        if st is None or st.state in (None, "", "unknown", "unavailable"):
            return default
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return default

    def _read_datetime_optional(self, entity_id: str | None
                                 ) -> datetime | None:
        if not entity_id:
            return None
        st = self.reader.get(entity_id)
        if st is None or st.state in (None, "", "unknown", "unavailable"):
            return None
        try:
            dt = _parse_iso(st.state)
        except (TypeError, ValueError):
            return None
        return dt

    def _session_energy_kwh(self, ev_cfg: EVConfig, now: datetime) -> float:
        # Prefer the user-configured session-energy sensor; else use the
        # internal integrator (Task 14).
        if ev_cfg.session_energy_entity:
            return self._read_float_optional(
                ev_cfg.session_energy_entity, default=0.0)
        return self.ev_state.session_energy_kwh if self.ev_state else 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_planner.py::test_planner_engages_lp_when_target_and_deadline_set -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(planner): build OptimizerInputs with EV target/deadline"
```

---

## Task 14: Planner — internal session energy integrator + reset

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_planner.py`:

```python
def test_planner_integrates_session_energy_when_no_sensor() -> None:
    """Without ev_session_energy_entity, planner integrates ev_charging_power_entity."""
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="2000")  # 2 kW
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    # Advance 30 min — integrator should be 2 kW * 0.5 h = 1.0 kWh.
    p.step(NOW + timedelta(minutes=30))
    assert p.ev_state is not None
    assert p.ev_state.session_energy_kwh == pytest.approx(1.0, abs=1e-3)


def test_planner_resets_session_energy_on_plug_in() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="2000")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    p.step(NOW + timedelta(minutes=30))
    assert p.ev_state.session_energy_kwh > 0
    # Now unplug.
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    p.step(NOW + timedelta(hours=1))
    # Plug back in.
    states["sensor.ev_state"] = StateView(state="Connected")
    p.step(NOW + timedelta(hours=2))
    # Integrator must have reset on plug-in.
    assert p.ev_state.session_energy_kwh == pytest.approx(0.0, abs=1e-3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_planner.py::test_planner_integrates_session_energy_when_no_sensor -v`
Expected: FAIL — `session_energy_kwh` always 0.

- [ ] **Step 3: Implement integrator** — in `planner.py`, inside `_apply_ev`, before "Update dwells.":

```python
        # Session integrator (only when user has no session_energy_entity).
        es = self.ev_state
        if cfg.session_energy_entity is None and es is not None:
            if es.last_state_class == EVStateClass.DISCONNECTED and state_class != EVStateClass.DISCONNECTED:
                # Plug-in transition — reset integrator.
                es.session_energy_kwh = 0.0
                es.last_charging_power_kw = None
                es.last_tick = None
            # Trapezoidal integration of ev_charging_power between ticks.
            cur_kw = ev_power_w / 1000.0
            if (es.last_tick is not None
                    and es.last_charging_power_kw is not None
                    and state_class != EVStateClass.DISCONNECTED):
                dt_h = max(0.0,
                           (now - es.last_tick).total_seconds() / 3600.0)
                avg_kw = (es.last_charging_power_kw + cur_kw) / 2.0
                es.session_energy_kwh += avg_kw * dt_h
            es.last_tick = now
            es.last_charging_power_kw = cur_kw
```

Move the existing `es.last_state_class = state_class` / `es.state_class_since = now` block to *after* the integrator (so the plug-in detector compares against the previous class first).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_planner.py -k "session_energy" -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(planner): integrate session energy with plug-in reset"
```

---

## Task 15: Add EV configuration keys to `const.py`

**Files:**
- Modify: `custom_components/pv_optimizer/const.py`

- [ ] **Step 1: Add EV config keys** — append to `custom_components/pv_optimizer/const.py`:

```python
# --- EV charging (all optional; leaving blank disables the feature) ---
CONF_EV_CHARGER_STATE = "ev_charger_state_entity"
CONF_EV_CHARGING_POWER = "ev_charging_power_entity"
CONF_EV_SESSION_ENERGY = "ev_session_energy_entity"
CONF_EV_MAX_CURRENT = "ev_max_current_entity"
CONF_EV_START_SWITCH = "ev_start_switch_entity"
CONF_EV_CHARGER_MODE = "ev_charger_mode_entity"

CONF_EV_MAX_CHARGING_POWER_KW = "ev_max_charging_power_kw"
CONF_EV_MAX_CHARGING_CURRENT_A = "ev_max_charging_current_a"
CONF_EV_MIN_CHARGING_CURRENT_A = "ev_min_charging_current_a"
CONF_EV_BUY_PRICE_THRESHOLD = "ev_buy_price_threshold"
CONF_EV_CAR_BATTERY_KWH = "ev_car_battery_kwh"
CONF_EV_CURRENT_TOLERANCE_A = "ev_current_tolerance_a"
CONF_EV_SESSION_DONE_POWER_W = "ev_session_done_power_w"
CONF_EV_SESSION_DONE_SECONDS = "ev_session_done_seconds"

DEFAULT_EV_MIN_CHARGING_CURRENT_A = 6.0
DEFAULT_EV_BUY_PRICE_THRESHOLD = 0.0
DEFAULT_EV_CURRENT_TOLERANCE_A = 1.0
DEFAULT_EV_SESSION_DONE_POWER_W = 100.0
DEFAULT_EV_SESSION_DONE_SECONDS = 60.0
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from custom_components.pv_optimizer import const; print(const.CONF_EV_MAX_CHARGING_POWER_KW)"`
Expected: prints `ev_max_charging_power_kw`.

- [ ] **Step 3: Commit**

```bash
git add custom_components/pv_optimizer/const.py
git commit -m "feat(const): add EV configuration keys"
```

---

## Task 16: Add EV step to `config_flow.py`

**Files:**
- Modify: `custom_components/pv_optimizer/config_flow.py`

This task is exercised in a live HA instance, not in this repository's CI.

- [ ] **Step 1: Add `_EV_SCHEMA`** — in `custom_components/pv_optimizer/config_flow.py`, after `_LOAD_FORECAST_SCHEMA`:

```python
# All EV fields are optional. Leaving every input/output entity blank
# disables the EV feature entirely.
_EV_SCHEMA = vol.Schema({
    vol.Optional(C.CONF_EV_CHARGER_STATE): _sensor(),
    vol.Optional(C.CONF_EV_CHARGING_POWER): _sensor(),
    vol.Optional(C.CONF_EV_SESSION_ENERGY): _sensor(),
    vol.Optional(C.CONF_EV_MAX_CURRENT): EntitySelector(
        EntitySelectorConfig(domain=["number", "input_number"])
    ),
    vol.Optional(C.CONF_EV_START_SWITCH): EntitySelector(
        EntitySelectorConfig(domain=["switch", "input_boolean"])
    ),
    vol.Optional(C.CONF_EV_CHARGER_MODE): EntitySelector(
        EntitySelectorConfig(domain=["select", "input_select"])
    ),
    vol.Optional(C.CONF_EV_MAX_CHARGING_POWER_KW): _num(0.0, 50.0, 0.1, "kW"),
    vol.Optional(C.CONF_EV_MAX_CHARGING_CURRENT_A): _num(0.0, 64.0, 1.0, "A"),
    vol.Optional(C.CONF_EV_MIN_CHARGING_CURRENT_A,
                 default=C.DEFAULT_EV_MIN_CHARGING_CURRENT_A): _num(0.0, 32.0, 1.0, "A"),
    vol.Optional(C.CONF_EV_BUY_PRICE_THRESHOLD,
                 default=C.DEFAULT_EV_BUY_PRICE_THRESHOLD): _num(-10.0, 10.0, 0.01, "/kWh"),
    vol.Optional(C.CONF_EV_CAR_BATTERY_KWH): _num(0.0, 200.0, 0.5, "kWh"),
    vol.Optional(C.CONF_EV_CURRENT_TOLERANCE_A,
                 default=C.DEFAULT_EV_CURRENT_TOLERANCE_A): _num(0.0, 5.0, 1.0, "A"),
    vol.Optional(C.CONF_EV_SESSION_DONE_POWER_W,
                 default=C.DEFAULT_EV_SESSION_DONE_POWER_W): _num(0.0, 1000.0, 10.0, "W"),
    vol.Optional(C.CONF_EV_SESSION_DONE_SECONDS,
                 default=C.DEFAULT_EV_SESSION_DONE_SECONDS): _num(0.0, 600.0, 5.0, "s"),
})
```

- [ ] **Step 2: Insert the EV step into the config flow** — modify `PvOptimizerConfigFlow`:

```python
    async def async_step_load_forecast(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        return self.async_show_form(
            step_id="load_forecast", data_schema=_LOAD_FORECAST_SCHEMA,
        )

    async def async_step_ev(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="PV LP Optimizer", data=self._data)
        return self.async_show_form(step_id="ev", data_schema=_EV_SCHEMA)
```

- [ ] **Step 3: Extend options schema** — update `_OPTIONS_SCHEMA`:

```python
_OPTIONS_SCHEMA = (
    _ENTITIES_SCHEMA
    .extend(_BATTERY_SCHEMA.schema)
    .extend(_SOLVER_SCHEMA.schema)
    .extend(_LOAD_FORECAST_SCHEMA.schema)
    .extend(_EV_SCHEMA.schema)
)
```

- [ ] **Step 4: Manual verification**

Live HA only. Start a clean HA instance, install the integration, walk through the config flow — confirm that the EV step shows after Load Forecast and that leaving every field blank still completes setup.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/config_flow.py
git commit -m "feat(config_flow): add optional EV step"
```

---

## Task 17: Wire EV entry data into `PlannerConfig` in `__init__.py`

**Files:**
- Modify: `custom_components/pv_optimizer/__init__.py`

This task has no automated tests — exercised in a live HA instance.

- [ ] **Step 1: Build `EVConfig` from entry data** — in `async_setup_entry` (`custom_components/pv_optimizer/__init__.py`), before constructing `PlannerConfig`:

```python
    from .models import EVParams
    from .planner import EVConfig

    ev_state_entity = data.get(C.CONF_EV_CHARGER_STATE)
    ev_power_entity = data.get(C.CONF_EV_CHARGING_POWER)
    ev_current_entity = data.get(C.CONF_EV_MAX_CURRENT)
    ev_max_kw = data.get(C.CONF_EV_MAX_CHARGING_POWER_KW)
    ev_max_a = data.get(C.CONF_EV_MAX_CHARGING_CURRENT_A)
    ev_car_kwh = data.get(C.CONF_EV_CAR_BATTERY_KWH)
    ev_cfg: EVConfig | None = None
    # All four required pieces present? Otherwise EV is disabled.
    if (ev_state_entity and ev_power_entity and ev_current_entity
            and ev_max_kw and ev_max_a and ev_car_kwh):
        ev_params = EVParams(
            max_charging_power_kw=float(ev_max_kw),
            max_charging_current_a=float(ev_max_a),
            min_charging_current_a=float(data.get(
                C.CONF_EV_MIN_CHARGING_CURRENT_A,
                C.DEFAULT_EV_MIN_CHARGING_CURRENT_A)),
            car_battery_kwh=float(ev_car_kwh),
            current_tolerance_a=float(data.get(
                C.CONF_EV_CURRENT_TOLERANCE_A,
                C.DEFAULT_EV_CURRENT_TOLERANCE_A)),
            session_done_power_w=float(data.get(
                C.CONF_EV_SESSION_DONE_POWER_W,
                C.DEFAULT_EV_SESSION_DONE_POWER_W)),
            session_done_seconds=float(data.get(
                C.CONF_EV_SESSION_DONE_SECONDS,
                C.DEFAULT_EV_SESSION_DONE_SECONDS)),
            buy_price_threshold=float(data.get(
                C.CONF_EV_BUY_PRICE_THRESHOLD,
                C.DEFAULT_EV_BUY_PRICE_THRESHOLD)),
        )
        ev_cfg = EVConfig(
            params=ev_params,
            charger_state_entity=ev_state_entity,
            charging_power_entity=ev_power_entity,
            max_current_entity=ev_current_entity,
            session_energy_entity=data.get(C.CONF_EV_SESSION_ENERGY),
            start_switch_entity=data.get(C.CONF_EV_START_SWITCH),
            charger_mode_entity=data.get(C.CONF_EV_CHARGER_MODE),
            mode_entity=f"select.pv_optimizer_ev_mode",
            target_kwh_entity=f"number.pv_optimizer_ev_target_kwh",
            target_pct_entity=f"number.pv_optimizer_ev_target_pct",
            deadline_entity=f"datetime.pv_optimizer_ev_deadline",
        )
```

Pass `ev=ev_cfg` to `PlannerConfig(...)`:

```python
    config = PlannerConfig(
        # ... existing fields ...
        min_sell_price_per_kwh=float(data.get(C.CONF_MIN_SELL_PRICE,
                                              C.DEFAULT_MIN_SELL_PRICE)),
        ev=ev_cfg,
    )
```

- [ ] **Step 2: Manual verification**

Re-run the live HA config flow with EV fields set; confirm the planner picks them up (check `home-assistant.log` for a successful first refresh and no `KeyError`).

- [ ] **Step 3: Commit**

```bash
git add custom_components/pv_optimizer/__init__.py
git commit -m "feat: wire EV config from entry data into PlannerConfig"
```

---

## Task 18: Add `number`, `select`, `datetime` platforms to `manifest.json` and `const.PLATFORMS`

**Files:**
- Modify: `custom_components/pv_optimizer/const.py`
- Modify: `custom_components/pv_optimizer/manifest.json`

- [ ] **Step 1: Inspect current manifest**

Run: `cat custom_components/pv_optimizer/manifest.json`
Expected: shows the existing fields. The integration currently has `PLATFORMS = ["sensor"]`.

- [ ] **Step 2: Update `PLATFORMS`** in `const.py`:

```python
PLATFORMS = ["sensor", "number", "select", "datetime", "switch"]
```

- [ ] **Step 3: Update `manifest.json`** — ensure it does NOT need a `requirements` change (we already depend on `pulp`). The platforms come from `PLATFORMS`, not the manifest, so the manifest stays unchanged. No-op for this file; verify by re-reading.

- [ ] **Step 4: Manual verification**

Restart HA after a code reload. Confirm that the new platforms load without errors. (Empty platform files in this task would yield warnings; the platform files themselves are added in subsequent tasks.)

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/const.py
git commit -m "feat(const): register number/select/datetime/switch platforms"
```

---

## Task 19: Integration-created entities (select / number / datetime)

**Files:**
- Create: `custom_components/pv_optimizer/select.py`
- Create: `custom_components/pv_optimizer/number.py`
- Create: `custom_components/pv_optimizer/datetime.py`

Live HA only — no unit tests.

- [ ] **Step 1: Create `select.py`** — minimum viable mode selector at `custom_components/pv_optimizer/select.py`:

```python
"""Mode select for the EV charging feature."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coord = hass.data[DOMAIN][entry.entry_id]
    if coord.config.ev is None:
        return
    async_add_entities([_EVModeSelect(entry.entry_id)])


class _EVModeSelect(RestoreEntity, SelectEntity):
    _attr_options = ["auto", "manual", "off"]
    _attr_translation_key = "ev_mode"

    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{entry_id}_ev_mode"
        self._attr_name = "PV LP Optimizer EV Mode"
        self._state = "auto"

    @property
    def current_option(self) -> str:
        return self._state

    async def async_select_option(self, option: str) -> None:
        self._state = option
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._state = last.state
```

- [ ] **Step 2: Create `number.py`** — target-kWh and target-percent numbers:

```python
"""EV target inputs (kWh and %)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coord = hass.data[DOMAIN][entry.entry_id]
    if coord.config.ev is None:
        return
    cap = coord.config.ev.params.car_battery_kwh
    async_add_entities([
        _TargetKwh(entry.entry_id, cap),
        _TargetPct(entry.entry_id, cap),
    ])


class _TargetKwh(RestoreEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, entry_id: str, cap: float) -> None:
        self._attr_unique_id = f"{entry_id}_ev_target_kwh"
        self._attr_name = "PV LP Optimizer EV Target kWh"
        self._attr_native_max_value = float(cap)
        self._value = 0.0

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = max(0.0, float(value))
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "", "unknown", "unavailable"):
            try:
                self._value = float(last.state)
            except ValueError:
                pass


class _TargetPct(RestoreEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "%"

    def __init__(self, entry_id: str, cap: float) -> None:
        self._attr_unique_id = f"{entry_id}_ev_target_pct"
        self._attr_name = "PV LP Optimizer EV Target %"
        self._cap = float(cap)
        self._value = 0.0

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = max(0.0, min(100.0, float(value)))
        self.async_write_ha_state()
        # NOTE: kWh / pct are independent stored values; the user explicitly
        # writes whichever they prefer. We don't auto-cross-update to keep
        # the surface area minimal.

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "", "unknown", "unavailable"):
            try:
                self._value = float(last.state)
            except ValueError:
                pass
```

- [ ] **Step 3: Create `datetime.py`** — the deadline picker:

```python
"""EV deadline datetime."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coord = hass.data[DOMAIN][entry.entry_id]
    if coord.config.ev is None:
        return
    async_add_entities([_DeadlineDateTime(entry.entry_id)])


class _DeadlineDateTime(RestoreEntity, DateTimeEntity):
    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{entry_id}_ev_deadline"
        self._attr_name = "PV LP Optimizer EV Deadline"
        self._value: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        return self._value

    async def async_set_value(self, value: datetime) -> None:
        self._value = value
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "", "unknown", "unavailable"):
            try:
                self._value = datetime.fromisoformat(last.state)
            except ValueError:
                self._value = None
```

- [ ] **Step 4: Manual verification**

Reload the integration in HA. Confirm that the four new entities appear under the device:
- `select.pv_optimizer_ev_mode` (auto/manual/off)
- `number.pv_optimizer_ev_target_kwh`
- `number.pv_optimizer_ev_target_pct`
- `datetime.pv_optimizer_ev_deadline`

Change them via the dashboard and verify the values persist across HA restarts (`RestoreEntity`).

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/select.py custom_components/pv_optimizer/number.py custom_components/pv_optimizer/datetime.py
git commit -m "feat: add EV mode select, target number, deadline datetime"
```

---

## Task 20: EV diagnostic sensors

**Files:**
- Modify: `custom_components/pv_optimizer/sensor.py`

Live HA only — no unit tests.

- [ ] **Step 1: Add EV sensors** — extend `async_setup_entry` in `custom_components/pv_optimizer/sensor.py`:

```python
async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = [
        _PlannedSetpointSensor(coord),
        _PlannedFeedInSensor(coord),
        _ExpectedCostSensor(coord),
        _SavingsSensor(coord),
        _PlanSensor(coord),
    ]
    if coord.forecaster is not None:
        entities.append(_LoadForecastSensor(coord))
    if coord.config.ev is not None:
        entities.extend([
            _EVStatusSensor(coord),
            _EVSessionEnergySensor(coord),
            _EVRemainingSensor(coord),
            _EVPlannedCurrentSensor(coord),
            _EVDeficitSensor(coord),
        ])
    async_add_entities(entities)
```

Append the sensor classes:

```python
class _EVStatusSensor(_Base):
    def __init__(self, coord) -> None:
        super().__init__(coord, "ev_status", "EV Status")

    @property
    def available(self) -> bool:
        return self._cycle is not None  # always available once a tick ran

    @property
    def native_value(self) -> str | None:
        c = self._cycle
        if c is None:
            return None
        # Surface whatever the planner last decided about the EV. The
        # planner stores last-tick state on the coordinator's ``ev_state``;
        # we read a small projection of it here.
        ev_state = getattr(self.coordinator._planner, "ev_state", None)
        if ev_state is None or ev_state.last_state_class is None:
            return "disconnected"
        from .ev_controller import EVStateClass
        if ev_state.last_state_class == EVStateClass.DISCONNECTED:
            return "disconnected"
        # Manual / off / LP / surplus / cheap-grid / ultimate-override —
        # the planner exposes hints via ``ev_state.latches`` and the LP plan.
        latches = ev_state.latches
        if c.result and c.result.slots and c.result.slots[0].p_ev_chg_kw > 0:
            return "charging_lp_planned"
        if latches and latches.ultimate_override:
            return "charging_ultimate_override"
        if latches and latches.cheap_grid:
            return "charging_cheap_grid"
        if ev_state.last_written_current_a and ev_state.last_written_current_a > 0:
            return "charging_surplus"
        return "idle"


class _EVSessionEnergySensor(_Base):
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coord) -> None:
        super().__init__(coord, "ev_session_energy", "EV Session Energy")

    @property
    def native_value(self) -> float | None:
        es = getattr(self.coordinator._planner, "ev_state", None)
        return None if es is None else round(es.session_energy_kwh, 3)


class _EVRemainingSensor(_Base):
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coord) -> None:
        super().__init__(coord, "ev_remaining_kwh", "EV Remaining kWh")

    @property
    def native_value(self) -> float | None:
        # The planner caches target_kwh and session energy in its EV state.
        # The remaining-kWh number is recomputed each cycle in _build_inputs;
        # we expose the most recent value via the LP inputs surface.
        c = self._cycle
        if c is None or c.result is None:
            return None
        return round(max(0.0, c.result.extras.get("ev_remaining_kwh", 0.0)), 3)


class _EVPlannedCurrentSensor(_Base):
    _attr_native_unit_of_measurement = "A"

    def __init__(self, coord) -> None:
        super().__init__(coord, "ev_planned_current", "EV Planned Current")

    @property
    def native_value(self) -> int | None:
        es = getattr(self.coordinator._planner, "ev_state", None)
        return None if es is None else es.last_written_current_a


class _EVDeficitSensor(_Base):
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coord) -> None:
        super().__init__(coord, "ev_deficit_kwh", "EV Deficit kWh")

    @property
    def native_value(self) -> float | None:
        c = self._cycle
        if c is None or c.result is None:
            return None
        return round(c.result.extras.get("ev_deficit_kwh", 0.0), 3)
```

Surface `ev_remaining_kwh` via `OptimizerResult.extras` from the planner. In `planner.py` `_build_inputs`, after computing `remaining`:

```python
                # Expose remaining-kWh for the diagnostic sensor.
                self._cached_ev_remaining_kwh = remaining
```

And in `planner.py` `step()`, after the solve succeeds, decorate the extras:

```python
        if self.config.ev is not None:
            result.extras["ev_remaining_kwh"] = getattr(
                self, "_cached_ev_remaining_kwh", 0.0)
```

NB: `OptimizerResult.extras` is a `dict` (default-factory) — mutating it in place is fine.

Extend the existing plan sensor's `slots` attribute output (in `_slot_to_dict` we already use `asdict`, so `p_ev_chg_kw` flows through automatically).

- [ ] **Step 2: Manual verification**

Restart HA and confirm the new sensors appear. Plug in / unplug to verify `ev_status` transitions. Set a target + deadline and confirm `ev_remaining_kwh` decreases as `ev_session_energy` increases.

- [ ] **Step 3: Commit**

```bash
git add custom_components/pv_optimizer/sensor.py custom_components/pv_optimizer/planner.py
git commit -m "feat(sensor): add EV diagnostic sensors"
```

---

## Task 21: Full regression run + final commit

**Files:**
- (no edits — full test sweep)

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all PASS (existing + new). Pay close attention to:
- `tests/test_optimizer.py` — no regressions in pre-EV LP tests.
- `tests/test_planner.py` — no regressions in pre-EV planner tests.
- `tests/test_ev_controller.py` — all new tests PASS.

- [ ] **Step 2: Check coverage**

Run: `pytest tests/ --cov=custom_components/pv_optimizer --cov-report=term-missing 2>&1 | tail -40`
Expected: `ev_controller.py` ≥ 90% coverage; `planner.py` overall coverage not lower than before.

- [ ] **Step 3: Manual end-to-end smoke**

Live HA only — describe the happy path:
1. Configure EV step with all required entities + sane params.
2. Set `select.pv_optimizer_ev_mode = auto`, `number.pv_optimizer_ev_target_kwh = 0`, deadline blank.
3. Plug in car, set in-car schedule to 100% by 8 AM.
4. Confirm `sensor.pv_optimizer_ev_status` flips to `charging_ultimate_override` and the car charges.
5. Unplug. Set `target_kwh = 20`, `deadline = today + 8h`. Plug back in.
6. Confirm `ev_status = charging_lp_planned` when cheap hours arrive; `ev_remaining_kwh` decreases.
7. Set deadline to 1h from now with target 50 kWh. Confirm `ev_deficit_kwh > 0`.
8. Switch mode to `manual`; confirm immediate max-current write and auto-return on disconnect.
9. Switch mode to `off`; confirm zero writes to `ev_max_current_entity`.

- [ ] **Step 4: No commit needed** — the suite-green state is the artifact. If any of the above surfaced an issue, file it as a follow-up task before declaring complete.

---

## Self-Review Notes

(Author's verification that every spec requirement maps to a task:)

| Spec section | Task(s) |
|---|---|
| §1 Purpose | Plan-wide |
| §2 Operational Modes (auto/manual/off) | Tasks 10–12, 19 |
| §3.1 Static parameters | Tasks 1, 15, 16 |
| §3.2 Configured entity IDs | Tasks 10, 15, 16, 17 |
| §3.3 State vocabulary | Task 5 |
| §3.4 Integration-created entities | Task 19 |
| §4.1 Mode-switching reactive | Tasks 7, 11 |
| §4.2 Non-mode-switching reactive | Tasks 6, 11 |
| §4.3 Ultimate-override semantics | Tasks 6, 7, 9 |
| §4.4 Start switch | Task 11 |
| §4.5 Cadence | Reused from existing planner cadence |
| §5.1 LP additions (`p_ev_chg`, slack, balance) | Tasks 3, 4 |
| §5.2 Planner bookkeeping (remaining_kwh) | Tasks 13, 14 |
| §5.3 Slot-0 translation | Task 9 |
| §5.4 Mode switching in LP path | Task 11 |
| §5.5 Session reset | Task 14 |
| §6.1 Manual override + auto-return | Tasks 11, 12 |
| §6.2 Off mode | Task 11 |
| §7 Diagnostic sensors | Task 20 |
| §8 Edge cases | Tasks 5 (state fallback), 11 (off), 13 (target/deadline guards), 14 (plug-in reset) |
| §9 Testing strategy | Tasks 1–14, 21 |
