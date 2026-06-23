# EV-triggered on-demand re-plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-plan immediately when a relevant EV entity changes (plug-in, target, deadline, planned-start, mode), so charging engages within seconds instead of waiting up to one full coordinator cycle.

**Architecture:** A pure helper computes the list of EV entity ids to watch from an `EVConfig`; the HA layer (`async_setup_entry`) registers an `async_track_state_change_event` listener over that list and calls `coordinator.async_request_refresh()` on any change. No planner/optimizer logic changes. The helper is unit-tested in the plain (HA-less) test venv; the listener registration is thin glue verified manually.

**Tech Stack:** Python 3.11, Home Assistant `DataUpdateCoordinator` + `homeassistant.helpers.event.async_track_state_change_event`, pytest.

Spec: `docs/superpowers/specs/2026-06-23-ev-replan-on-change-design.md`

---

### Task 1: Pure helper `ev_replan_trigger_entities`

Compute the deduplicated, non-empty list of EV entity ids that should trigger an immediate re-plan. Lives next to `EVConfig` in `planner.py` (already import-safe without Home Assistant), so it is unit-testable in the plain venv.

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py` (insert after the `EVConfig` dataclass, which ends at line 137, before `class PlannerConfig` at line 140)
- Test: `tests/test_planner.py` (append new tests near the existing `test_planner_config_with_ev_config`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_planner.py`:

```python
def test_ev_replan_trigger_entities_full_set() -> None:
    from custom_components.pv_optimizer.planner import (
        EVConfig, ev_replan_trigger_entities,
    )
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
        target_pct_entity="number.pv_optimizer_ev_target_pct",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        planned_start_entity="datetime.pv_optimizer_ev_planned_start",
    )
    assert ev_replan_trigger_entities(ev_cfg) == [
        "sensor.ev_state",
        "number.pv_optimizer_ev_target_kwh",
        "number.pv_optimizer_ev_target_pct",
        "datetime.pv_optimizer_ev_deadline",
        "datetime.pv_optimizer_ev_planned_start",
        "select.pv_optimizer_ev_mode",
    ]


def test_ev_replan_trigger_entities_filters_empty() -> None:
    """The integration-created ids default to '' on EVConfig; drop them.

    The charging-power entity is deliberately NOT watched (its wattage
    fluctuates during normal charging and would trigger needless solves).
    """
    from custom_components.pv_optimizer.planner import (
        EVConfig, ev_replan_trigger_entities,
    )
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
    )
    result = ev_replan_trigger_entities(ev_cfg)
    assert result == ["sensor.ev_state"]
    assert "sensor.ev_power" not in result
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest "tests/test_planner.py::test_ev_replan_trigger_entities_full_set" "tests/test_planner.py::test_ev_replan_trigger_entities_filters_empty" -v`
Expected: FAIL with `ImportError: cannot import name 'ev_replan_trigger_entities'`.

- [ ] **Step 3: Write the minimal implementation**

In `custom_components/pv_optimizer/planner.py`, immediately after the `EVConfig` dataclass (after line 137) and before `@dataclass(frozen=True)`/`class PlannerConfig`:

```python
def ev_replan_trigger_entities(ev: EVConfig) -> list[str]:
    """Entity ids whose state changes should trigger an immediate re-plan.

    Watches the charger *connection state* and the user-facing EV inputs
    (target kWh/%, deadline, planned start, mode). The charging-*power*
    entity is intentionally excluded: its wattage fluctuates throughout a
    charging session and would trigger needless (CPU-bound) LP solves.

    Empty ids are filtered out so a never-configured optional input never
    subscribes to ``""``. Order is stable for deterministic tests.
    """
    candidates = [
        ev.charger_state_entity,
        ev.target_kwh_entity,
        ev.target_pct_entity,
        ev.deadline_entity,
        ev.planned_start_entity,
        ev.mode_entity,
    ]
    return [e for e in candidates if e]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest "tests/test_planner.py::test_ev_replan_trigger_entities_full_set" "tests/test_planner.py::test_ev_replan_trigger_entities_filters_empty" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(ev): add ev_replan_trigger_entities helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire the state-change listener in `async_setup_entry`

Register an HA state-change listener over the helper's entity list; on any change, request an immediate coordinator refresh. This is thin HA glue (the test venv has no Home Assistant, so it is verified manually, not unit-tested).

**Files:**
- Modify: `custom_components/pv_optimizer/__init__.py` (inside `async_setup_entry`, after `hass.data.setdefault(...)` at line 136 and after the existing `async_forward_entry_setups` at line 137; before the options-reload listener at line 142)

- [ ] **Step 1: Add the deferred imports**

In `custom_components/pv_optimizer/__init__.py`, at the top of `async_setup_entry`'s deferred-import block (the existing block starting at line 19 with `from .coordinator import ...`), add the HA event/core imports and the helper import. After the edit the block reads:

```python
    # Imports deferred so this module is import-safe without homeassistant.
    from homeassistant.core import callback
    from homeassistant.helpers.event import async_track_state_change_event
    from .coordinator import LoadForecasterOptions, PvOptimizerCoordinator
    from .models import BatteryParams, EVParams
    from .planner import EVConfig, PlannerConfig, ev_replan_trigger_entities
    from . import const as C
```

- [ ] **Step 2: Register the listener**

In `async_setup_entry`, insert this block immediately after the existing
`await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)` line
(currently line 137), and before the `entry.async_on_unload(entry.add_update_listener(...))` line:

```python
    # Re-plan immediately when a relevant EV entity changes (e.g. plugging in
    # the car, or editing the target/deadline/planned-start/mode) instead of
    # waiting up to a full coordinator cycle. async_request_refresh() is
    # debounced by HA, so the first change fires at once and rapid follow-up
    # bounces coalesce into a single solve. async_on_unload detaches the
    # listener on reload/unload (mirrors the options-reload listener below).
    if ev_cfg is not None:
        ev_trigger_entities = ev_replan_trigger_entities(ev_cfg)

        @callback
        def _ev_changed(event):
            hass.async_create_task(coord.async_request_refresh())

        if ev_trigger_entities:
            entry.async_on_unload(
                async_track_state_change_event(
                    hass, ev_trigger_entities, _ev_changed)
            )
```

- [ ] **Step 3: Byte-compile to catch syntax/indentation errors**

Run: `python -m py_compile custom_components/pv_optimizer/__init__.py`
Expected: no output (exit 0).

- [ ] **Step 4: Run the full suite (no regressions)**

Run: `pytest -q`
Expected: all tests pass. (No new unit test here — `__init__.py` requires Home Assistant, which is absent from the test venv by design.)

- [ ] **Step 5: Manual verification (record result in the commit/PR)**

In a running Home Assistant with the integration configured for EV:
1. Note the configured `update_seconds` (e.g. 180s).
2. With the car unplugged and an EV target + future deadline set, plug in the car.
3. Confirm a re-plan happens within ~1-2s (not after the full cycle): the EV
   max-current / charging state reacts almost immediately rather than waiting
   for the next scheduled update. Watch the `pv_optimizer` debug log for an
   off-cycle coordinator update timestamped right after the plug-in.
4. Change the EV mode select auto→off and confirm another immediate re-plan.

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_optimizer/__init__.py
git commit -m "feat(ev): re-plan on EV state/input changes via state listener

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Trigger entity set (charger_state, target_kwh, target_pct, deadline, planned_start, mode) → Task 1 helper + tests. ✓
- Watch state entity, not power entity → Task 1 excludes `charging_power_entity`; asserted in `test_ev_replan_trigger_entities_filters_empty`. ✓
- Listener wiring + `async_request_refresh` + `async_on_unload` + `ev_cfg is not None` guard → Task 2. ✓
- Debounce safety, unavailable/unknown harmless → relies on HA `Debouncer` and planner's existing optional reads; documented, no code needed. ✓
- Out of scope (button, non-EV inputs, planner logic changes) → none added. ✓
- Testing approach (pure helper unit-tested; glue manual since no HA in venv) → Task 1 tests + Task 2 Step 5. ✓

**Placeholder scan:** No TBD/TODO; all code blocks are complete. ✓

**Type/name consistency:** `ev_replan_trigger_entities(ev: EVConfig) -> list[str]` is defined in Task 1 and imported/called identically in Task 2. `ev_cfg` and `coord` are the exact local names already present in `async_setup_entry` (`__init__.py:51`, `:130`). ✓
