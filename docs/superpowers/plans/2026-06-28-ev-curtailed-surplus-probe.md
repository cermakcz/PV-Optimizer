# EV charging from curtailed solar surplus — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the home battery is full and grid export is disabled (bad sell price), have the planner actively drive the EVCS to absorb otherwise-curtailed PV surplus that the charger's own export-following "Auto" mode cannot see.

**Architecture:** A new opt-in reactive control layer. Two pure functions in `ev_controller.py` decide *whether* to take over (`should_probe_surplus`) and *how much current* to command (`decide_surplus_probe`, a stateful zero-import regulator). The planner wires them into the two no-LP-charge paths (planned-start gate + reactive branch). Overshoot is detected primarily via battery discharge (a full battery feeds the gap before the grid does), so the probe requires a new `battery_power_entity` and stays disabled (falling back to today's passive handback) when that entity is absent.

**Tech Stack:** Python 3.14, Home Assistant custom component, `pytest`. Pure logic lives in `ev_controller.py` (HA-free, tested by `tests/test_ev_controller.py`); planner wiring tested in `tests/test_planner.py` with `FakeReader`/`FakeCaller`.

**Spec:** `docs/superpowers/specs/2026-06-23-ev-curtailed-surplus-probe-design.md`

**Branch:** `ocermak/ev_curtailed_surplus_probe` (already checked out; the spec commit is on it).

---

## File Structure

- `custom_components/pv_optimizer/const.py` — add `CONF_BATTERY_POWER`.
- `custom_components/pv_optimizer/config_flow.py` — add an optional sensor selector for it.
- `custom_components/pv_optimizer/__init__.py` — read it into `PlannerConfig`.
- `custom_components/pv_optimizer/planner.py` — new `battery_power_entity` field on `PlannerConfig`; new probe fields on `EVRuntimeState`; `_read_float_or_none` helper; cache slot-0 PV/load; `force` param on `_write_ev_current`; `_run_surplus_probe` helper; wiring into the gate + reactive branches.
- `custom_components/pv_optimizer/ev_controller.py` — probe constants, `should_probe_surplus`, `SurplusProbeDecision`, `decide_surplus_probe`.
- `tests/test_ev_controller.py` — pure-logic tests.
- `tests/test_planner.py` — wiring tests.

---

## Task 1: Config plumbing for `battery_power_entity`

**Files:**
- Modify: `custom_components/pv_optimizer/const.py`
- Modify: `custom_components/pv_optimizer/config_flow.py`
- Modify: `custom_components/pv_optimizer/planner.py` (`PlannerConfig`)
- Modify: `custom_components/pv_optimizer/__init__.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_planner.py`:

```python
def test_planner_config_accepts_battery_power_entity() -> None:
    cfg = _config(battery_power_entity="sensor.batt_w")
    assert cfg.battery_power_entity == "sensor.batt_w"


def test_planner_config_battery_power_entity_defaults_none() -> None:
    assert _config().battery_power_entity is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_planner.py::test_planner_config_accepts_battery_power_entity -q`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'battery_power_entity'`.

- [ ] **Step 3: Add the `PlannerConfig` field**

In `custom_components/pv_optimizer/planner.py`, in the `PlannerConfig` dataclass, add next to the other optional input entities (after `force_pv_export_entity`):

```python
    force_pv_export_entity: str | None = None
    battery_power_entity: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k battery_power_entity -q`
Expected: PASS (both tests).

- [ ] **Step 5: Wire the config key (const + config_flow + __init__)**

In `custom_components/pv_optimizer/const.py`, next to `CONF_FORCE_PV_EXPORT`:

```python
CONF_FORCE_PV_EXPORT = "force_pv_export_entity"
CONF_BATTERY_POWER = "battery_power_entity"
```

In `custom_components/pv_optimizer/config_flow.py`, in the schema dict next to the `CONF_FORCE_PV_EXPORT` entry:

```python
    vol.Optional(C.CONF_BATTERY_POWER): _sensor(),
```

In `custom_components/pv_optimizer/__init__.py`, in the `PlannerConfig(...)` construction next to `force_pv_export_entity=...`:

```python
        force_pv_export_entity=data.get(C.CONF_FORCE_PV_EXPORT),
        battery_power_entity=data.get(C.CONF_BATTERY_POWER),
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all green; nothing else touched).

- [ ] **Step 7: Commit**

```bash
git add custom_components/pv_optimizer/const.py custom_components/pv_optimizer/config_flow.py custom_components/pv_optimizer/planner.py custom_components/pv_optimizer/__init__.py tests/test_planner.py
git commit -m "feat(ev): add optional battery_power_entity config"
```

---

## Task 2: Planner input plumbing — `_read_float_or_none` + cache slot-0 PV/load

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_planner.py`:

```python
def test_read_float_or_none_reads_and_handles_bad() -> None:
    states = _states()
    states["sensor.batt_w"] = StateView(state="-1500")
    states["sensor.bad"] = StateView(state="unavailable")
    p = Planner(_config(), FakeReader(states), FakeCaller())
    assert p._read_float_or_none("sensor.batt_w") == -1500.0
    assert p._read_float_or_none("sensor.bad") is None
    assert p._read_float_or_none("sensor.missing") is None
    assert p._read_float_or_none(None) is None


def test_planner_caches_first_slot_pv_and_load() -> None:
    # pv forecast 3 kW at slot 0, load ~1 kW -> cached surplus ~2 kW.
    pv_forecast = {
        (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): (3000.0 if h == 0 else 0.0)
        for h in range(4)
    }
    states = _states(load_w=1000.0, pv_forecast=pv_forecast)
    p = Planner(_config(), FakeReader(states), FakeCaller())
    p.step(NOW)
    assert p._cached_first_pv_kw == pytest.approx(3.0, abs=1e-6)
    assert p._cached_first_load_kw == pytest.approx(1.0, abs=0.2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k "read_float_or_none or caches_first_slot" -q`
Expected: FAIL — `AttributeError: 'Planner' object has no attribute '_read_float_or_none'`.

- [ ] **Step 3: Add the reader helper**

In `custom_components/pv_optimizer/planner.py`, next to `_read_float_optional` (around line 593):

```python
    def _read_float_or_none(self, entity_id: str | None) -> float | None:
        """Like ``_read_float_optional`` but returns ``None`` (not a default)
        when the entity is unset, missing, or non-numeric — so callers can
        fail-safe on a genuinely-absent reading."""
        if not entity_id:
            return None
        st = self.reader.get(entity_id)
        if st is None or st.state in _BAD_STATES:
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None
```

- [ ] **Step 4: Add the cache fields and populate them**

In `__init__` (around line 244-245), next to the other caches:

```python
        self._cached_first_buy_price: float = 0.0
        self._cached_ev_remaining_kwh: float = 0.0
        self._cached_first_pv_kw: float = 0.0
        self._cached_first_load_kw: float = 0.0
```

In `_build_inputs`, immediately after the existing `self._cached_first_buy_price = slots[0].price_buy` line (around line 396):

```python
        self._cached_first_buy_price = slots[0].price_buy
        self._cached_first_pv_kw = pv_kw[0] if pv_kw else 0.0
        self._cached_first_load_kw = load_kw[0] if load_kw else 0.0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k "read_float_or_none or caches_first_slot" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(planner): add _read_float_or_none and cache slot-0 pv/load"
```

---

## Task 3: Pure `should_probe_surplus` arm predicate

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ev_controller.py`:

```python
from custom_components.pv_optimizer.ev_controller import (
    EVStateClass,
    should_probe_surplus,
    SOC_FULL_EPS_KWH,
    SOC_DISARM_EPS_KWH,
)


def _arm_kwargs(**over):
    base = dict(
        currently_armed=False,
        state_class=EVStateClass.CONNECTED_IDLE,
        p_ev_chg_kw=0.0,
        p_sell_kw=0.0,
        soc_kwh=9.0,
        soc_max_kwh=9.0,
        forecast_surplus_kw=2.0,
        battery_power_available=True,
        grid_available=True,
    )
    base.update(over)
    return base


def test_should_probe_arms_in_curtailment_corner() -> None:
    assert should_probe_surplus(**_arm_kwargs()) is True


def test_should_probe_blocks_without_battery_power() -> None:
    assert should_probe_surplus(**_arm_kwargs(battery_power_available=False)) is False


def test_should_probe_blocks_without_grid() -> None:
    assert should_probe_surplus(**_arm_kwargs(grid_available=False)) is False


def test_should_probe_blocks_when_disconnected() -> None:
    assert should_probe_surplus(
        **_arm_kwargs(state_class=EVStateClass.DISCONNECTED)) is False


def test_should_probe_blocks_when_lp_charges() -> None:
    assert should_probe_surplus(**_arm_kwargs(p_ev_chg_kw=2.0)) is False


def test_should_probe_blocks_when_exporting() -> None:
    assert should_probe_surplus(**_arm_kwargs(p_sell_kw=1.0)) is False


def test_should_probe_blocks_when_battery_not_full() -> None:
    # 9.0 - 0.2 (SOC_FULL_EPS) = 8.8 is the arm floor; 8.5 is below it.
    assert should_probe_surplus(**_arm_kwargs(soc_kwh=8.5)) is False


def test_should_probe_blocks_without_forecast_surplus() -> None:
    assert should_probe_surplus(**_arm_kwargs(forecast_surplus_kw=0.1)) is False


def test_should_probe_disarm_uses_wider_soc_margin() -> None:
    # soc 8.6: below arm floor (8.8) but above disarm floor (9.0-0.5=8.5).
    assert should_probe_surplus(**_arm_kwargs(soc_kwh=8.6, currently_armed=False)) is False
    assert should_probe_surplus(**_arm_kwargs(soc_kwh=8.6, currently_armed=True)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k should_probe -q`
Expected: FAIL — `ImportError: cannot import name 'should_probe_surplus'`.

- [ ] **Step 3: Implement the constants and predicate**

In `custom_components/pv_optimizer/ev_controller.py`, after the `classify_state` function (before `ReactiveDecision`), add:

```python
# --- Curtailed-surplus probe (see specs/2026-06-23-ev-curtailed-surplus-probe) ---
# All tunable; initial values to validate empirically.
SOC_FULL_EPS_KWH = 0.2        # how close to soc_max counts as "full" (arm)
SOC_DISARM_EPS_KWH = 0.5      # wider margin to stay armed (avoid self-disarm)
PROBE_FORECAST_MARGIN_KW = 0.5  # forecast surplus must exceed this to arm
PROBE_DISCHARGE_CEILING_W = 300.0  # step down above this battery discharge
PROBE_IMPORT_CEILING_W = 500.0     # step down above this grid import
PROBE_UP_INTERVAL_CYCLES = 3       # min cycles between speculative up-steps


def should_probe_surplus(
    *,
    currently_armed: bool,
    state_class: EVStateClass,
    p_ev_chg_kw: float,
    p_sell_kw: float,
    soc_kwh: float,
    soc_max_kwh: float,
    forecast_surplus_kw: float,
    battery_power_available: bool,
    grid_available: bool,
    eps: float = 1e-6,
) -> bool:
    """True iff the planner should take over surplus charging from the EVCS.

    Arms only in the curtailment corner: battery full, no LP-planned EV charge,
    not exporting (so the EVCS's export-follower would be blind), forecast says
    surplus exists, car connected, and both signal sources are available.
    Uses a wider SoC margin while already armed so the probe's own brief
    overshoot-dip can't disarm it.
    """
    if not (battery_power_available and grid_available):
        return False
    if state_class == EVStateClass.DISCONNECTED:
        return False
    if p_ev_chg_kw > eps or p_sell_kw > eps:
        return False
    soc_eps = SOC_DISARM_EPS_KWH if currently_armed else SOC_FULL_EPS_KWH
    if soc_kwh < soc_max_kwh - soc_eps:
        return False
    if forecast_surplus_kw <= PROBE_FORECAST_MARGIN_KW:
        return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k should_probe -q`
Expected: PASS (all 9).

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "feat(ev): add should_probe_surplus arm predicate"
```

---

## Task 4: Pure `decide_surplus_probe` regulator

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ev_controller.py`:

```python
from custom_components.pv_optimizer.models import EVParams
from custom_components.pv_optimizer.ev_controller import (
    decide_surplus_probe,
    PROBE_UP_INTERVAL_CYCLES,
)

# 7.2 kW / 32 A => 0.225 kW/A; min 6 A, max 16 A.
_PROBE_EV = EVParams(
    max_charging_power_kw=7.2, max_charging_current_a=32.0,
    min_charging_current_a=6.0, car_battery_kwh=60.0,
)


def _probe(**over):
    base = dict(
        battery_discharge_w=0.0,
        grid_import_w=0.0,
        forecast_surplus_kw=10.0,
        current_a=0,
        cycles_since_up=0,
        ev=_PROBE_EV,
    )
    base.update(over)
    return decide_surplus_probe(**base)


def test_probe_kicks_to_min_when_not_charging() -> None:
    d = _probe(current_a=0)
    assert d.current_a == 6
    assert d.cycles_since_up == 0


def test_probe_steps_down_on_battery_discharge() -> None:
    d = _probe(current_a=10, battery_discharge_w=500.0)
    assert d.current_a == 9
    assert d.cycles_since_up == 0


def test_probe_steps_down_on_grid_import() -> None:
    d = _probe(current_a=10, grid_import_w=800.0)
    assert d.current_a == 9


def test_probe_below_min_goes_to_zero() -> None:
    d = _probe(current_a=6, battery_discharge_w=500.0)
    assert d.current_a == 0


def test_probe_holds_inside_deadband() -> None:
    # No overshoot, interval not yet reached -> hold, count up.
    d = _probe(current_a=10, cycles_since_up=0)
    assert d.current_a == 10
    assert d.cycles_since_up == 1


def test_probe_steps_up_after_interval() -> None:
    d = _probe(current_a=10, cycles_since_up=PROBE_UP_INTERVAL_CYCLES - 1)
    assert d.current_a == 11
    assert d.cycles_since_up == 0


def test_probe_up_gated_by_forecast_headroom() -> None:
    # current 10 A -> 11 A needs 11*0.225 = 2.475 kW. Forecast surplus 2.0 kW
    # is below that, so no up-step even though the interval elapsed.
    d = _probe(current_a=10, cycles_since_up=PROBE_UP_INTERVAL_CYCLES - 1,
               forecast_surplus_kw=2.0)
    assert d.current_a == 10
    assert d.cycles_since_up == PROBE_UP_INTERVAL_CYCLES


def test_probe_does_not_exceed_max() -> None:
    d = _probe(current_a=32, cycles_since_up=PROBE_UP_INTERVAL_CYCLES - 1)
    assert d.current_a == 32
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k probe -q`
Expected: FAIL — `ImportError: cannot import name 'decide_surplus_probe'`.

- [ ] **Step 3: Implement the regulator**

In `custom_components/pv_optimizer/ev_controller.py`, after `should_probe_surplus`:

```python
@dataclass(frozen=True)
class SurplusProbeDecision:
    """One regulator step: next commanded current and updated up-counter."""

    current_a: int           # integer A; 0 disables charging
    cycles_since_up: int


def decide_surplus_probe(
    *,
    battery_discharge_w: float,
    grid_import_w: float,
    forecast_surplus_kw: float,
    current_a: int,
    cycles_since_up: int,
    ev,  # EVParams
) -> SurplusProbeDecision:
    """Zero-import regulator step (see spec §"Control law").

    Down (responsive): on battery discharge or grid import past the ceilings,
    step down one amp immediately (below min -> 0). Up (speculative, lazy): at
    most one amp every PROBE_UP_INTERVAL_CYCLES, only while not overshooting,
    below max, and the next amp still fits the forecast surplus headroom.
    Otherwise hold and advance the up-counter.
    """
    min_a = int(round(ev.min_charging_current_a))
    max_a = int(round(ev.max_charging_current_a))

    overshoot = (battery_discharge_w > PROBE_DISCHARGE_CEILING_W
                 or grid_import_w > PROBE_IMPORT_CEILING_W)
    if overshoot:
        new_a = current_a - 1
        if new_a < min_a:
            return SurplusProbeDecision(current_a=0, cycles_since_up=0)
        return SurplusProbeDecision(current_a=new_a, cycles_since_up=0)

    if current_a < min_a:
        # Not charging yet — kick to min as the first probe.
        return SurplusProbeDecision(current_a=min_a, cycles_since_up=0)

    can_step_up = (
        current_a < max_a
        and cycles_since_up + 1 >= PROBE_UP_INTERVAL_CYCLES
        and (current_a + 1) * ev.kw_per_amp <= forecast_surplus_kw
    )
    if can_step_up:
        return SurplusProbeDecision(current_a=current_a + 1, cycles_since_up=0)
    return SurplusProbeDecision(current_a=current_a,
                                cycles_since_up=cycles_since_up + 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k probe -q`
Expected: PASS (all 8).

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "feat(ev): add decide_surplus_probe regulator"
```

---

## Task 5: `EVRuntimeState` probe fields + `_write_ev_current` force param

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_planner.py`:

```python
def test_write_ev_current_force_bypasses_tolerance() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=7.2, max_charging_current_a=32.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            current_tolerance_a=1.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
    )
    p = Planner(_config(ev=ev_cfg), FakeReader(_states()), FakeCaller())
    p.ev_state.last_written_current_a = 10
    # 1 A delta is within tolerance -> suppressed without force.
    p._write_ev_current(11)
    assert not [c for c in p.caller.calls
                if c[2].get("entity_id") == "number.ev_max_current"]
    # force=True writes anyway.
    p._write_ev_current(11, force=True)
    writes = [c for c in p.caller.calls
              if c[2].get("entity_id") == "number.ev_max_current"]
    assert writes and writes[-1][2]["value"] == 11
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_planner.py::test_write_ev_current_force_bypasses_tolerance -q`
Expected: FAIL — `TypeError: _write_ev_current() got an unexpected keyword argument 'force'`.

- [ ] **Step 3: Add probe fields to `EVRuntimeState`**

In `custom_components/pv_optimizer/planner.py`, in the `EVRuntimeState` dataclass, after `last_tick`:

```python
    last_tick: datetime | None = None
    # Curtailed-surplus probe state.
    probe_armed: bool = False
    probe_current_a: int = 0
    probe_cycles_since_up: int = 0
```

- [ ] **Step 4: Add the `force` param to `_write_ev_current`**

Replace the body of `_write_ev_current` (around lines 890-903) with:

```python
    def _write_ev_current(self, value_a: float, *, force: bool = False) -> None:
        cfg = self.config.ev
        if cfg is None:
            return
        es = self.ev_state
        new_val = int(round(value_a))
        if (not force and es and es.last_written_current_a is not None
                and abs(new_val - es.last_written_current_a)
                <= cfg.params.current_tolerance_a):
            return  # within tolerance
        self.caller.call("number", "set_value", {
            "entity_id": cfg.max_current_entity, "value": new_val,
        })
        if es is not None:
            es.last_written_current_a = new_val
```

- [ ] **Step 5: Run tests to verify pass + no regressions**

Run: `.venv/bin/python -m pytest tests/test_planner.py -q`
Expected: PASS (the new test plus all existing planner tests).

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(planner): probe runtime state + forceable current write"
```

---

## Task 6: `_run_surplus_probe` helper + wire into the reactive branch

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_planner.py`. The helper used across these tests:

```python
def _probe_ev_cfg():
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    return EVConfig(
        params=EVParams(
            max_charging_power_kw=7.2, max_charging_current_a=32.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            current_tolerance_a=1.0, buy_price_threshold=0.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        planned_start_entity="datetime.pv_optimizer_ev_planned_start",
    )


def _probe_states():
    # Battery full (soc_max is 9 kWh of 10 kWh capacity => 90 %), sunny
    # forecast at slot 0, sell price below 0 so the LP never exports, no EV
    # target so the LP plans no charge -> reactive branch.
    pv_forecast = {
        (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): (4000.0 if h == 0 else 0.0)
        for h in range(4)
    }
    states = _states(soc_pct=90.0, load_w=1000.0, sell=[-0.01] * 24,
                     pv_forecast=pv_forecast)
    states["sensor.ev_state"] = StateView(state="Connected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Manual")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    states["sensor.batt_w"] = StateView(state="0")  # battery idle
    states["sensor.grid_w"] = StateView(state="0")
    return states


def test_probe_arms_and_drives_manual_in_reactive_branch() -> None:
    states = _probe_states()
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    current_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    starts = [c for c in p.caller.calls if c[2].get("entity_id") == "switch.ev_start"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Manual"
    assert current_writes and current_writes[-1][2]["value"] == 6  # kicked to min
    assert starts and starts[-1][1] == "turn_on"
    assert p.ev_state.probe_armed is True
    assert p.ev_state.probe_current_a == 6


def test_probe_disabled_without_battery_power_entity() -> None:
    states = _probe_states()
    p = Planner(_config(ev=_probe_ev_cfg()),  # no battery_power_entity
                FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    # Falls back to the reactive passive handback (EVCS "Auto").
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"
    assert p.ev_state.probe_armed is False


def test_probe_disarms_back_to_auto_when_exporting() -> None:
    states = _probe_states()
    states["sensor.sell"] = StateView(state="0.30", attributes={"today": [0.30] * 24})
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True       # pretend we were probing
    p.ev_state.probe_current_a = 12
    p.step(NOW)
    assert p.ev_state.probe_armed is False
    assert p.ev_state.probe_current_a == 0
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k "probe_arms or probe_disabled or probe_disarms" -q`
Expected: FAIL — `test_probe_arms...` asserts "Manual" but the reactive branch still writes "Auto" (probe not wired yet).

- [ ] **Step 3: Add imports**

In `custom_components/pv_optimizer/planner.py`, find the existing import from `ev_controller` (it imports `classify_state`, `decide_reactive`, `EVStateClass`, `is_session_done`, `translate_lp_slot0`) and add the two new names:

```python
from .ev_controller import (
    classify_state, decide_reactive, EVStateClass, is_session_done,
    translate_lp_slot0, should_probe_surplus, decide_surplus_probe,
)
```

(Match the existing import statement's exact form; just add `should_probe_surplus` and `decide_surplus_probe`.)

- [ ] **Step 4: Add the `_run_surplus_probe` helper**

In `custom_components/pv_optimizer/planner.py`, add to the `Planner` class in the "EV helpers" region (near `_write_mode_auto`, around line 950):

```python
    def _run_surplus_probe(self, plan_first) -> bool:
        """If conditions warrant, take over surplus charging from the EVCS and
        return True (writes Manual + regulated current + start). Otherwise reset
        any probe state and return False so the caller's passive handback runs.
        """
        cfg = self.config.ev
        es = self.ev_state
        if cfg is None or es is None:
            return False
        batt = self._read_float_or_none(self.config.battery_power_entity)
        grid = self._read_float_or_none(self.config.grid_power_entity)
        soc_pct = self._read_float_or_none(self.config.battery_soc_entity)
        state_class = classify_state(self._read_text(cfg.charger_state_entity))
        soc_kwh = (soc_pct / 100.0 * self.config.battery.capacity_kwh
                   if soc_pct is not None else 0.0)
        forecast_surplus = self._cached_first_pv_kw - self._cached_first_load_kw
        armed = should_probe_surplus(
            currently_armed=es.probe_armed,
            state_class=state_class,
            p_ev_chg_kw=plan_first.p_ev_chg_kw,
            p_sell_kw=plan_first.p_sell_kw,
            soc_kwh=soc_kwh,
            soc_max_kwh=self.config.battery.soc_max_kwh,
            forecast_surplus_kw=forecast_surplus,
            battery_power_available=batt is not None and soc_pct is not None,
            grid_available=grid is not None,
        )
        if not armed:
            if es.probe_armed:
                es.probe_armed = False
                es.probe_current_a = 0
                es.probe_cycles_since_up = 0
            return False
        decision = decide_surplus_probe(
            battery_discharge_w=max(0.0, -batt),
            grid_import_w=max(0.0, grid),
            forecast_surplus_kw=forecast_surplus,
            current_a=es.probe_current_a,
            cycles_since_up=es.probe_cycles_since_up,
            ev=cfg.params,
        )
        # Mode first so any active/passive transition cache invalidation lands
        # before the (forced) current write.
        self._write_ev_charger_mode_active()
        self._write_ev_current(decision.current_a, force=True)
        self._write_ev_start(decision.current_a > 0)
        es.probe_armed = True
        es.probe_current_a = decision.current_a
        es.probe_cycles_since_up = decision.cycles_since_up
        return True
```

- [ ] **Step 5: Wire it into the reactive branch**

In `_apply_ev`, find the reactive-path section (around line 835):

```python
        # Reactive path.
        es.cheap_grid_active = price_buy <= cfg.params.buy_price_threshold
```

Insert the probe check immediately before it:

```python
        # Reactive path.
        if self._run_surplus_probe(plan_first):
            return
        es.cheap_grid_active = price_buy <= cfg.params.buy_price_threshold
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k "probe_arms or probe_disabled or probe_disarms" -q`
Expected: PASS (all 3).

- [ ] **Step 7: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(ev): planner-driven surplus probe in reactive branch"
```

---

## Task 7: Wire the probe into the planned-start gate

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_planner.py`:

```python
def test_probe_arms_in_planned_start_gate() -> None:
    states = _probe_states()
    # Future planned start + a target so the LP reserves a later window;
    # slot 0 stays at p_ev_chg=0, exercising the gate.
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    deadline = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    planned_start = (NOW + timedelta(hours=2)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(state=planned_start)
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    current_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Manual"   # probe took over
    assert current_writes and current_writes[-1][2]["value"] == 6
    assert p.ev_state.probe_armed is True


def test_planned_start_gate_without_battery_power_still_passive() -> None:
    # Regression: existing gate behavior (passive handback) is preserved when
    # the probe is not configured.
    states = _probe_states()
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    deadline = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    planned_start = (NOW + timedelta(hours=2)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(state=planned_start)
    p = Planner(_config(ev=_probe_ev_cfg()),  # no battery_power_entity
                FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"   # passive handback
    assert p.ev_state.probe_armed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_planner.py::test_probe_arms_in_planned_start_gate -q`
Expected: FAIL — gate still writes "Auto" (passive handback), so the "Manual" assert fails.

- [ ] **Step 3: Wire the probe into the gate**

In `_apply_ev`, find the planned-start gate (around lines 737-746, as left by the prior change):

```python
        if mode == "auto":
            planned_start = self._read_datetime_optional(
                cfg.planned_start_entity)
            if planned_start is not None and planned_start > now:
                self._write_ev_charger_mode_passive()
                self._write_ev_current(cfg.params.max_charging_current_a)
                self._write_ev_start(True)
                return
```

Replace the inner block so the probe is tried first, falling back to the passive handback:

```python
        if mode == "auto":
            planned_start = self._read_datetime_optional(
                cfg.planned_start_entity)
            if planned_start is not None and planned_start > now:
                if not self._run_surplus_probe(plan_first):
                    self._write_ev_charger_mode_passive()
                    self._write_ev_current(cfg.params.max_charging_current_a)
                    self._write_ev_start(True)
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k "planned_start_gate or probe_arms_in_planned" -q`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (the two pre-existing gate tests still pass — they configure no `battery_power_entity`, so the probe stays disabled and the passive handback runs unchanged).

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(ev): planner-driven surplus probe in planned-start gate"
```

---

## Task 8: Regression sweep + manifest of behavior

**Files:**
- Test: `tests/` (whole suite)

- [ ] **Step 1: Run the entire suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, output pristine (no warnings/errors).

- [ ] **Step 2: Confirm the two prior gate tests assert the right (unchanged) behavior**

These pre-existing tests must still pass because they don't configure `battery_power_entity`:
- `test_planner_planned_start_pre_schedules_window_in_auto`
- `test_planner_planned_start_hands_connected_car_to_passive_surplus`

Run: `.venv/bin/python -m pytest tests/test_planner.py -k "planned_start_pre_schedules or hands_connected_car_to_passive" -q`
Expected: PASS.

- [ ] **Step 3: Lint/type check if the project has one**

Run: `.venv/bin/python -m pytest -q` is the gate; if the repo has a `ruff`/`mypy` config, run it (e.g. `.venv/bin/ruff check custom_components`). Otherwise skip.

- [ ] **Step 4: Final commit if anything changed**

```bash
git add -A
git commit -m "test(ev): regression sweep for surplus probe" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- Asymmetric split (EVCS follows export) → arm predicate blocks on `p_sell > 0` (Task 3). ✓
- Probe arms only in curtailment corner → `should_probe_surplus` (Task 3). ✓
- Watch battery discharge (primary) + grid import (secondary) → `decide_surplus_probe` overshoot (Task 4); planner reads `battery_power_entity`, normalises `max(0, -batt)` (Task 6). ✓
- Probe requires `battery_power_entity`; fail-safe to passive otherwise → Tasks 1, 6, 7 + regression Task 8. ✓
- SoC hysteresis (arm vs disarm) → `currently_armed` branch (Task 3). ✓
- Below-min → 0 → Task 4. ✓
- Anti-oscillation (deadband + lazy/rate-limited up + forecast headroom) → Task 4. ✓
- Disarm back to "Auto", reset state, no stickiness → Task 6 helper + reactive/gate fall-through. ✓
- Wire both no-LP paths through one helper → Tasks 6, 7. ✓
- Constants per spec (`PROBE_UP_INTERVAL_CYCLES=3`, `PROBE_IMPORT_CEILING_W=500`, `PROBE_DISCHARGE_CEILING_W=300`, `SOC_FULL_EPS_KWH=0.2`, `SOC_DISARM_EPS_KWH=0.5`, `PROBE_FORECAST_MARGIN_KW=0.5`) → Task 3. ✓
- Cadence unchanged (existing cycle) → no scheduler change. ✓

**Type consistency:** `should_probe_surplus` / `decide_surplus_probe` / `SurplusProbeDecision` signatures and `EVRuntimeState` field names (`probe_armed`, `probe_current_a`, `probe_cycles_since_up`) are used identically across Tasks 3–7. `_write_ev_current(value, *, force=False)` and `_run_surplus_probe(plan_first)` match their call sites.

**Placeholder scan:** none — every step shows concrete code/commands.

**Note on the forecast-headroom gate:** it assumes the slot-0 load is EV-excluded, which holds in auto mode because the load forecaster already subtracts EV history (`feat(ev): planner gates EV-history subtraction on auto mode`). It only suppresses speculative up-steps; the battery-discharge down-step is the real safety, so an inaccurate headroom estimate cannot cause grid import or battery drain.
