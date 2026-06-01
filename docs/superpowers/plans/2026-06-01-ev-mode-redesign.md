# EV mode redesign — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the `manual` EV mode to `car`, remove the ultimate-override
latch entirely from `auto` mode, and add an opt-in `car`-mode auto-return
switch — so `auto` becomes pure LP-plan-or-surplus and `car` is the
explicit "give the car max amps" choice.

**Architecture:** Strip the `ultimate_override` short-circuits out of
`decide_reactive`, `translate_lp_slot0`, and `update_latches`. With
only `cheap_grid` left, collapse `LatchState` / `update_latches` to a
single inline `cheap_grid_active: bool`. Add a new `switch` platform
hosting `switch.pv_optimizer_ev_car_auto_return`; planner reads it
inside the `car` branch and only auto-returns when on. `select.py`
gains a `manual → car` migration in `async_added_to_hass`.

**Tech Stack:** Python 3.14, Home Assistant custom-component conventions
(SelectEntity / SwitchEntity / RestoreEntity), pytest, no new deps.

**Spec:** `docs/superpowers/specs/2026-06-01-ev-mode-redesign-design.md`.

**Files**

- Modify: `custom_components/pv_optimizer/ev_controller.py` — remove override branches, delete `LatchState` + `update_latches`.
- Modify: `custom_components/pv_optimizer/planner.py` — rename mode string, rename runtime field, inline `cheap_grid_active`, gate auto-return on the switch, add `EVConfig.car_auto_return_entity`, add `EVRuntimeState.last_mode`.
- Modify: `custom_components/pv_optimizer/select.py` — options list `manual` → `car`, migrate stored `manual` on restore.
- Modify: `custom_components/pv_optimizer/sensor.py` — drop `charging_ultimate_override`, add `off` and `car_mode` precedence, read `cheap_grid_active`.
- Modify: `custom_components/pv_optimizer/const.py` — `PLATFORMS += ["switch"]`.
- Create: `custom_components/pv_optimizer/switch.py` — new platform module with one `RestoreEntity` switch.
- Modify: `tests/test_ev_controller.py` — delete ultimate-override and `LatchState` tests; add the inverted behavior tests.
- Modify: `tests/test_planner.py` — rename `manual` → `car`, retarget auto-return tests to require the switch ON, add sticky-when-OFF tests.
- Modify: `docs/superpowers/specs/2026-05-27-ev-charging-design.md` — amend per §8 of the redesign spec.

**Conventions used throughout**

- `pytest -xvs tests/path/test.py::test_name` to run one test verbosely, failing fast.
- `pytest tests/` to run the full suite.
- Commit format: matches recent history — `fix(ev): …`, `feat(ev): …`, `refactor(ev): …`. Co-author trailer omitted (recent commits don't use it).
- Single-quoted strings in Python where the file's surrounding style uses them; double quotes elsewhere. Match what's nearby.

---

## Task 1: Remove the REQUESTING short-circuit from `decide_reactive`

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py:103-107`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Replace the existing "ultimate override" test with the new inverted behavior**

In `tests/test_ev_controller.py`, find this test (around line 126):

```python
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
```

Replace it with:

```python
def test_reactive_requesting_no_surplus_no_cheap_grid_writes_zero() -> None:
    """After the redesign, the car's REQUESTING signal no longer pegs to
    max. With no surplus and no cheap-grid trigger, we write 0 even
    though the car is asking — auto mode honours the LP / surplus
    constraint rather than the car's request."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=5000.0,         # importing — no surplus
        ev_charging_power_w=0.0,
        price_buy=0.50,              # not cheap
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_requesting_with_surplus_tracks_surplus() -> None:
    """REQUESTING no longer short-circuits — surplus math still runs."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=-3000.0,        # 3 kW surplus
        ev_charging_power_w=0.0,
        price_buy=0.50,
        ev=_ev(),
    )
    # kw_per_amp = 0.4 -> 3 kW / 0.4 = 7.5 -> truncate to 7.
    assert out.max_current_a == 7


def test_reactive_requesting_with_cheap_grid_grants_max() -> None:
    """Cheap-grid still wins for REQUESTING too — it's price-driven, not request-driven."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=5000.0,
        ev_charging_power_w=0.0,
        price_buy=-0.05,             # below threshold
        ev=_ev(),
    )
    assert out.max_current_a == 20
```

- [ ] **Step 2: Run the new tests to verify they fail**

```
pytest -xvs tests/test_ev_controller.py::test_reactive_requesting_no_surplus_no_cheap_grid_writes_zero
```

Expected: FAIL — current code returns `20` because of the override short-circuit.

- [ ] **Step 3: Delete the REQUESTING short-circuit in `decide_reactive`**

In `custom_components/pv_optimizer/ev_controller.py`, delete lines 105-107:

```python
    if state_class == EVStateClass.CONNECTED_REQUESTING:
        # Ultimate override — car has actively negotiated for power (§4.3).
        return ReactiveDecision(max_current_a=int(round(ev.max_charging_current_a)))
```

The function should now read (post-edit, around line 103-120):

```python
    if state_class == EVStateClass.DISCONNECTED:
        return ReactiveDecision(max_current_a=0)
    if price_buy <= ev.buy_price_threshold:
        return ReactiveDecision(max_current_a=int(round(ev.max_charging_current_a)))
    # Surplus tracking: back-add what EV is already drawing so loop converges.
    surplus_kw = max(0.0, (-grid_power_w + ev_charging_power_w) / 1000.0)
    target_a = surplus_kw / ev.kw_per_amp
    if target_a < ev.min_charging_current_a:
        return ReactiveDecision(max_current_a=0)
    clamped = max(ev.min_charging_current_a,
                  min(ev.max_charging_current_a, target_a))
    # Truncate (not round) so we never overshoot available PV surplus.
    # E.g. with 7.5 A of headroom, rounding up to 8 A would pull the last
    # 0.5 A from the grid; truncating to 7 A keeps us on the export side.
    return ReactiveDecision(max_current_a=int(clamped))
```

- [ ] **Step 4: Run the affected tests to verify they pass**

```
pytest -xvs tests/test_ev_controller.py::test_reactive_requesting_no_surplus_no_cheap_grid_writes_zero tests/test_ev_controller.py::test_reactive_requesting_with_surplus_tracks_surplus tests/test_ev_controller.py::test_reactive_requesting_with_cheap_grid_grants_max
```

Expected: 3 passed.

Also run the full ev_controller test file to confirm no regression:

```
pytest -xvs tests/test_ev_controller.py
```

Expected: all green (the `LatchState`/`update_latches` ultimate-override tests still pass — those are touched in Task 5 / Task 6).

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "refactor(ev): drop REQUESTING short-circuit from decide_reactive

Auto mode now lets surplus / cheap-grid drive the current even when
the car is in CONNECTED_REQUESTING; the car's request signal no
longer forces max-current. Part of the auto/car/off mode redesign."
```

---

## Task 2: Remove the REQUESTING short-circuit from `translate_lp_slot0`

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py:225-227`
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Delete the override test and add the inverted-behavior test**

In `tests/test_ev_controller.py`, delete this test (around line 419):

```python
def test_translate_ultimate_override_beats_lp_zero() -> None:
    """Car requesting and not drawing -> max regardless of LP plan."""
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev_charging_power_w=0.0, ev=ev,
    ) == 20
```

In its place add:

```python
def test_translate_lp_zero_yields_zero_even_when_requesting() -> None:
    """After the redesign, REQUESTING + low power no longer overrides the LP.
    If the LP plans 0, we write 0 — the user's plan beats the car's signal."""
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev_charging_power_w=0.0, ev=ev,
    ) == 0


def test_translate_lp_positive_when_requesting_low_power_uses_lp_value() -> None:
    """LP-derived current is written even when the car is REQUESTING with
    low power — no max-current override."""
    ev = _ev()
    # 4 kW / 0.4 kw/A = 10 A.
    assert translate_lp_slot0(
        p_ev_chg_kw=4.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev_charging_power_w=0.0, ev=ev,
    ) == 10
```

- [ ] **Step 2: Run the new tests to verify they fail**

```
pytest -xvs tests/test_ev_controller.py::test_translate_lp_zero_yields_zero_even_when_requesting tests/test_ev_controller.py::test_translate_lp_positive_when_requesting_low_power_uses_lp_value
```

Expected: FAIL — current code returns `20` for both because of the override.

- [ ] **Step 3: Delete the REQUESTING short-circuit in `translate_lp_slot0`**

In `custom_components/pv_optimizer/ev_controller.py`, delete lines 225-227 (and the matching paragraph in the docstring above them — the bullet starting "If car is actively requesting AND not drawing meaningful power"):

```python
    if (state_class == EVStateClass.CONNECTED_REQUESTING
            and ev_charging_power_w < ev.session_done_power_w):
        return int(round(ev.max_charging_current_a))
```

Update the docstring to remove the now-obsolete first bullet so it reads:

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
    - If disconnected, write 0.
    - If LP plans zero, write zero.
    - If LP plans > 0 but the converted current is below
      ``min_charging_current_a``, clamp UP (contrast with reactive's
      skip-below-min): the user has committed to a target, so a minor
      slot-0 overshoot is acceptable. The next tick re-plans with reduced
      remaining_kwh.
    """
```

The `ev_charging_power_w` argument is now unused. Leave it in the signature for now — it's removed in a later task only if no caller still passes it. (Spoiler: planner passes it; remove the unused param at the same time we touch the planner, Task 5.)

Actually, simpler: drop the unused parameter right now. Update the planner call site at `planner.py:783-787` from:

```python
            current = translate_lp_slot0(
                p_ev_chg_kw=plan_first.p_ev_chg_kw,
                state_class=state_class,
                ev_charging_power_w=ev_power_w, ev=cfg.params,
            )
```

to:

```python
            current = translate_lp_slot0(
                p_ev_chg_kw=plan_first.p_ev_chg_kw,
                state_class=state_class,
                ev=cfg.params,
            )
```

And drop `ev_charging_power_w` from the signature in `ev_controller.py`:

```python
def translate_lp_slot0(
    *,
    p_ev_chg_kw: float,
    state_class: EVStateClass,
    ev,
) -> int:
```

Update the existing `translate_*` tests in `tests/test_ev_controller.py` that pass `ev_charging_power_w=...` — drop that kwarg from each call. The affected tests (search the file): `test_translate_disconnected_yields_zero`, `test_translate_lp_zero_yields_zero`, `test_translate_lp_positive_above_min_converts_to_amps`, `test_translate_lp_below_min_clamps_up_to_floor`, `test_translate_lp_above_max_clamps_down`, plus the two new ones added in Step 1 — drop `ev_charging_power_w=0.0` from each.

- [ ] **Step 4: Run the translate tests to verify they pass**

```
pytest -xvs tests/test_ev_controller.py -k translate
```

Expected: all `translate_*` tests pass.

- [ ] **Step 5: Run the planner tests to confirm the call-site change didn't break anything**

```
pytest -xvs tests/test_planner.py
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py custom_components/pv_optimizer/planner.py tests/test_ev_controller.py
git commit -m "refactor(ev): drop REQUESTING short-circuit from translate_lp_slot0

LP plan takes precedence over the car's request signal: if LP plans
0 for this slot, we write 0 even when the car is asking. Removes
the now-unused ev_charging_power_w argument."
```

---

## Task 3: Add the `car_auto_return` switch platform

**Files:**
- Create: `custom_components/pv_optimizer/switch.py`
- Modify: `custom_components/pv_optimizer/const.py:5`
- Modify: `custom_components/pv_optimizer/planner.py` — add `EVConfig.car_auto_return_entity` field

- [ ] **Step 1: Add the field to `EVConfig`**

In `custom_components/pv_optimizer/planner.py`, find the `EVConfig` dataclass (around line 107) and add this field just after `planned_start_entity`:

```python
    # Opt-in switch: when ON, 'car' mode auto-returns to 'auto' on
    # session-done (disconnect, or idle+low-power dwell after the car
    # has actually drawn power at least once). When OFF (default), 'car'
    # mode is sticky — stays until the user switches it back.
    car_auto_return_entity: str = ""
```

The default is empty string, not the entity id — that way external callers (tests, alternative wiring) opt in by setting it. The integration's own __init__.py supplies the canonical id below.

- [ ] **Step 2: Wire the canonical id in the integration's EVConfig construction**

In `custom_components/pv_optimizer/__init__.py`, find the `EVConfig(...)` block (around line 80-95) and add this line alongside the other fixed entity ids:

```python
            car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
```

(Put it after `planned_start_entity="datetime.pv_optimizer_ev_planned_start",`.)

- [ ] **Step 3: Add "switch" to PLATFORMS**

In `custom_components/pv_optimizer/const.py`, change line 5 from:

```python
PLATFORMS = ["sensor", "number", "select", "datetime"]
```

to:

```python
PLATFORMS = ["sensor", "number", "select", "datetime", "switch"]
```

- [ ] **Step 4: Create the switch platform module**

Create `custom_components/pv_optimizer/switch.py` (mirror `select.py`'s shape):

```python
"""Switch entities for the EV charging feature."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([_EVCarAutoReturnSwitch(entry.entry_id)])


class _EVCarAutoReturnSwitch(RestoreEntity, SwitchEntity):
    """Opt-in: when ON, 'car' mode auto-returns to 'auto' on session-done."""

    _attr_translation_key = "ev_car_auto_return"

    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{entry_id}_ev_car_auto_return"
        self._attr_name = "PV LP Optimizer EV Car Auto-Return"
        # Pin the entity_id so the planner's hard-coded read
        # (switch.pv_optimizer_ev_car_auto_return) lines up regardless
        # of the slug HA would derive from the friendly name.
        self.entity_id = "switch.pv_optimizer_ev_car_auto_return"
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._state = False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in ("on", "true", "1"):
            self._state = True
```

- [ ] **Step 5: Smoke test — import and class shape**

Add to `tests/test_ev_controller.py` (or a new file `tests/test_switch.py` if you prefer; the project keeps unit-level smoke tests inline so inline is fine):

```python
def test_car_auto_return_switch_class_shape() -> None:
    """Smoke test: switch platform imports cleanly and the entity defaults off."""
    from custom_components.pv_optimizer.switch import _EVCarAutoReturnSwitch
    s = _EVCarAutoReturnSwitch("entry-id-1")
    assert s.entity_id == "switch.pv_optimizer_ev_car_auto_return"
    assert s._attr_unique_id == "entry-id-1_ev_car_auto_return"
    assert s.is_on is False
```

- [ ] **Step 6: Run the smoke test**

```
pytest -xvs tests/test_ev_controller.py::test_car_auto_return_switch_class_shape
```

Expected: pass.

- [ ] **Step 7: Run the full test suite to make sure nothing else regressed**

```
pytest tests/
```

Expected: all green. The switch entity exists but nothing reads it yet; behavior is unchanged.

- [ ] **Step 8: Commit**

```bash
git add custom_components/pv_optimizer/switch.py custom_components/pv_optimizer/const.py custom_components/pv_optimizer/planner.py custom_components/pv_optimizer/__init__.py tests/test_ev_controller.py
git commit -m "feat(ev): add car_auto_return switch scaffolding

New switch entity switch.pv_optimizer_ev_car_auto_return (default off),
plus EVConfig.car_auto_return_entity field. Nothing reads the switch
yet — the planner is wired in the next commit."
```

---

## Task 4: Rename `manual` → `car` in the planner; gate auto-return on the switch

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py` — `EVRuntimeState`, `_apply_ev`, supporting helpers
- Modify: `tests/test_planner.py` — rename and add tests

- [ ] **Step 1: Rename existing tests and add new switch-aware tests**

In `tests/test_planner.py`, do a literal find-replace of `"manual"` → `"car"` in every `StateView(state="manual")` (six locations: lines ~1160, 1201, 1378, 1414, 1448, 1488). Also rename the test function names: `test_planner_manual_mode_*` → `test_planner_car_mode_*`. Update the in-function docstrings / comments where they say "manual" to "car" — purely cosmetic but keeps the file honest.

The four affected tests are: `test_planner_manual_mode_writes_max`, `test_planner_manual_mode_writes_start_and_charger_mode_every_tick`, `test_planner_planned_start_does_not_gate_manual_mode`, `test_planner_manual_mode_holds_through_low_soc_pause`, `test_planner_manual_mode_auto_returns_on_disconnect`, `test_planner_manual_mode_auto_exits_after_real_session`.

For the three **auto-return-aware** tests (`..._auto_returns_on_disconnect`, `..._auto_exits_after_real_session`, `..._holds_through_low_soc_pause`), the auto-return path is now gated on the switch. Two edits per test:

1. In the `EVConfig(...)` fixture, add the line:

```python
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
```

…alongside the other entity-id kwargs. Without this, `_read_bool_optional` short-circuits on an empty string and the auto-return code path is never reached.

2. In the `states[...]` block, add:

```python
states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="on")
```

…immediately after the `states["select.pv_optimizer_ev_mode"] = StateView(state="car")` line. These tests now assert the auto-return behavior **when the switch is ON**, which is what their docstrings already imply.

The other car-mode tests (`..._writes_max`, `..._writes_start_and_charger_mode_every_tick`, `..._planned_start_does_not_gate_car_mode`) don't exercise the auto-return path at all — leave them with `car_auto_return_entity` unset (default `""`), so the planner skips the switch read entirely.

Add two NEW tests at the end of the car-mode test block (after `test_planner_car_mode_auto_exits_after_real_session`):

```python
def test_planner_car_mode_does_not_auto_return_on_disconnect_when_switch_off() -> None:
    """Default (switch OFF): 'car' mode is sticky. No auto-return."""
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
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="off")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert not mode_writes, "car mode must stay sticky when auto-return switch is OFF"


def test_planner_car_mode_does_not_auto_exit_after_session_when_switch_off() -> None:
    """Default (switch OFF): a finished session does NOT bounce us back to auto."""
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
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="6000")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="off")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    states["sensor.ev_state"] = StateView(state="Charged")
    states["sensor.ev_power"] = StateView(state="0")
    p.step(NOW + timedelta(seconds=30))
    p.step(NOW + timedelta(seconds=120))
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert not mode_writes, "car mode is sticky — finished session must not bounce back to auto"
```

- [ ] **Step 2: Run the renamed + new tests — they should fail**

```
pytest -xvs tests/test_planner.py -k car_mode
```

Expected: the renamed `..._writes_max` and `..._writes_start_and_charger_mode_every_tick` likely fail because the planner code still matches `"manual"`. The new switch-OFF tests fail because today's code always auto-returns regardless of switch.

- [ ] **Step 3: Update the planner's mode matching and gate the auto-return on the switch**

In `custom_components/pv_optimizer/planner.py`, find the `_apply_ev` method (around line 692). Make these edits:

a) Rename the field on `EVRuntimeState` (around line 70):

```python
    # Tracks whether the car has drawn meaningful power (>= session_done_power_w)
    # at any point during the current 'car' mode session (auto-return path only).
    # Without this, the auto-return would fire on the IDLE+low-power dwell
    # whenever the EVCS is gating (e.g. low_soc, waiting_for_*) — defeating
    # the point of car mode. Reset whenever we leave car or the car disconnects.
    car_session_charging_seen: bool = False
```

b) In `_apply_ev`, find the block that resets the flag (around line 749-753):

```python
        # Reset manual-session "charging seen" flag whenever we leave manual
        # mode or the car goes away. Both transitions end the session
        # whether or not it ever actually drew power.
        if mode != "manual" or state_class == EVStateClass.DISCONNECTED:
            es.manual_session_charging_seen = False
```

Replace with:

```python
        # Reset car-mode "charging seen" flag whenever we leave car mode
        # or the car goes away. Both transitions end the session whether
        # or not it ever actually drew power.
        if mode != "car" or state_class == EVStateClass.DISCONNECTED:
            es.car_session_charging_seen = False
```

c) Replace the entire `if mode == "manual": ... return` block (around line 759-780) with the switch-gated `car` branch:

```python
        # Car mode: planner stays out of the car's way — write max current,
        # active charger mode, and start switch every tick. Auto-return to
        # 'auto' is opt-in via switch.pv_optimizer_ev_car_auto_return; off
        # by default, in which case car mode is sticky.
        if mode == "car":
            car_auto_return = self._read_bool_optional(
                cfg.car_auto_return_entity, default=False)
            if car_auto_return:
                if ev_power_w >= cfg.params.session_done_power_w:
                    es.car_session_charging_seen = True
                disconnected_done = state_class == EVStateClass.DISCONNECTED
                idle_done = (
                    es.car_session_charging_seen
                    and is_session_done(state_class=state_class,
                                        ev_charging_power_w=ev_power_w,
                                        low_power_seconds=low_power_s,
                                        ev=cfg.params)
                )
                if disconnected_done or idle_done:
                    self._write_mode_auto()
                    return
            # Mode first so any mode-transition cache invalidation lands
            # before the current write — otherwise the current would be
            # written, then immediately have its cache cleared, and re-fire
            # unnecessarily on the next tick.
            self._write_ev_charger_mode_active()
            self._write_ev_current(cfg.params.max_charging_current_a)
            self._write_ev_start(True)
            return
```

Note: when `car_auto_return` is False, we skip both the bookkeeping and the auto-return — the flag stays at whatever it was, but since we already reset it on `mode != "car"` above (and on disconnect), drift is bounded.

- [ ] **Step 4: Run the planner tests to verify they pass**

```
pytest -xvs tests/test_planner.py -k car_mode
```

Expected: all car-mode tests pass — the four renamed ones plus the two new switch-OFF ones.

- [ ] **Step 5: Run the full suite to confirm no regression**

```
pytest tests/
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "feat(ev): rename manual mode to car, gate auto-return on opt-in switch

'car' replaces 'manual' in the planner's mode matching, and the
auto-return-to-auto behavior is now gated on switch.pv_optimizer_ev_
car_auto_return (default off → sticky). Also renames
manual_session_charging_seen → car_session_charging_seen."
```

---

## Task 5: Inline `cheap_grid_active`; drop `LatchState` usage from planner + sensor

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py` — `EVRuntimeState.latches` → `cheap_grid_active`; inline computation in `_apply_ev`; remove `update_latches` import
- Modify: `custom_components/pv_optimizer/sensor.py:212-218` — read `cheap_grid_active`, drop the override case
- Modify: `tests/test_planner.py` — any test asserting `ev_state.latches.*` shape

- [ ] **Step 1: Update planner imports**

In `custom_components/pv_optimizer/planner.py`, near the top (lines 18-27), drop `LatchState` and `update_latches` from the import block:

```python
from .ev_controller import (
    DEFAULT_STATE_VOCAB,
    EVStateClass,
    classify_state,
    decide_reactive,
    is_session_done,
    translate_lp_slot0,
)
```

- [ ] **Step 2: Replace `latches: LatchState` with `cheap_grid_active: bool`**

In `custom_components/pv_optimizer/planner.py`, find the `EVRuntimeState` dataclass (around line 49) and replace this line:

```python
    latches: "LatchState" = None
```

with:

```python
    cheap_grid_active: bool = False
```

In the `Planner.__init__` (around line 213-215), replace:

```python
        self.ev_state: EVRuntimeState | None = (
            EVRuntimeState(latches=LatchState()) if config.ev is not None else None
        )
```

with:

```python
        self.ev_state: EVRuntimeState | None = (
            EVRuntimeState() if config.ev is not None else None
        )
```

- [ ] **Step 3: Inline `cheap_grid_active` in `_apply_ev`'s reactive path**

In `custom_components/pv_optimizer/planner.py`, find the reactive-path block in `_apply_ev` (around line 797-813). Replace:

```python
        # Reactive path.
        if cfg.charger_mode_entity:
            # Mode-switching variant: latches drive mode + max-current.
            es.latches = update_latches(
                es.latches,
                state_class=state_class,
                price_buy=price_buy,
                ev_charging_power_w=ev_power_w,
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
```

with:

```python
        # Reactive path.
        es.cheap_grid_active = price_buy <= cfg.params.buy_price_threshold
        if cfg.charger_mode_entity:
            # Mode-switching variant: cheap-grid drives mode + max-current.
            # When off, we hand back to the EVCS (passive + max so its own
            # surplus/solar logic decides).
            if es.cheap_grid_active:
                self._write_ev_charger_mode_active()
                self._write_ev_current(cfg.params.max_charging_current_a)
                self._write_ev_start(True)
            else:
                self._write_ev_charger_mode_passive()
                self._write_ev_current(cfg.params.max_charging_current_a)
```

Note: the `else` branch using `decide_reactive` is unchanged — leave it alone.

Also drop the now-unused `time_in_class` computation if nothing else in `_apply_ev` reads it. Skim the function — if `time_in_class` isn't referenced after the latch removal, delete the `time_in_class = (now - es.state_class_since)...` line and the dwell-tracking block immediately above it (around lines 735-741: `if es.last_state_class != state_class: es.last_state_class = state_class; es.state_class_since = now`). **Wait** — `es.last_state_class` is also read by the sensor (`sensor.py:207`) and by the session-energy integrator block (around line 717: `if (es.last_state_class == EVStateClass.DISCONNECTED and state_class != EVStateClass.DISCONNECTED)`). Keep the `last_state_class` tracking. Only drop the `state_class_since` field and the `time_in_class` local if nothing reads them. **After the latch removal, nothing reads `time_in_class`** — the only consumer was `update_latches`. Drop the local, drop the `state_class_since` field from `EVRuntimeState`, and drop the assignment in the dwell-update block. Keep `last_state_class`.

Concretely, the dwell-update block (around lines 735-741) becomes:

```python
        # Update dwells.
        if es.last_state_class != state_class:
            es.last_state_class = state_class
        if ev_power_w < cfg.params.session_done_power_w:
            if es.low_power_since is None:
                es.low_power_since = now
            low_power_s = (now - es.low_power_since).total_seconds()
        else:
            es.low_power_since = None
            low_power_s = 0.0
```

And the `EVRuntimeState` field declaration loses `state_class_since`:

```python
@dataclass
class EVRuntimeState:
    """Per-planner mutable EV state."""

    cheap_grid_active: bool = False
    last_state_class: "EVStateClass" = None
    low_power_since: datetime | None = None
    last_written_current_a: int | None = None
    last_written_charger_mode: str | None = None  # "active" or "passive"
    car_session_charging_seen: bool = False
    last_session_plug_in: datetime | None = None
    session_energy_kwh: float = 0.0
    last_charging_power_kw: float | None = None
    last_tick: datetime | None = None
```

(Drop the long comment about `last_written_charger_mode` only if you want to thin it; leaving it is fine.)

- [ ] **Step 4: Update sensor to read `cheap_grid_active` and drop the override state**

In `custom_components/pv_optimizer/sensor.py`, find `_EVStatusSensor.native_value` (around line 201-221) and replace the body's latch/state-derivation chunk (lines 212-219) — from `latches = ev_state.latches` through the `return "charging_surplus"` line. Replace with:

```python
        if c.result and c.result.slots and c.result.slots[0].p_ev_chg_kw > 0:
            return "charging_lp_planned"
        if getattr(ev_state, "cheap_grid_active", False):
            return "charging_cheap_grid"
        if ev_state.last_written_current_a and ev_state.last_written_current_a > 0:
            return "charging_surplus"
        return "idle"
```

The `charging_ultimate_override` case is gone. (`off` and `car_mode` cases come in Task 8.)

- [ ] **Step 5: Update any planner tests that assert `ev_state.latches.*` shape**

Search:

```
grep -n "latches\|LatchState\|cheap_grid\.\|ultimate_override\." tests/test_planner.py
```

If any matches turn up referencing `ev_state.latches`, rewrite them to use `ev_state.cheap_grid_active`. If they reference `LatchState`, drop those assertions. (Most likely there are zero — the `LatchState` shape tests live in `test_ev_controller.py` and are handled in Task 6.)

- [ ] **Step 6: Run the full suite**

```
pytest tests/
```

Expected: green. The reactive cheap-grid behavior is preserved; the field rename is transparent to anyone not poking the internals.

- [ ] **Step 7: Commit**

```bash
git add custom_components/pv_optimizer/planner.py custom_components/pv_optimizer/sensor.py tests/test_planner.py
git commit -m "refactor(ev): inline cheap_grid_active; drop LatchState plumbing in planner

EVRuntimeState now carries cheap_grid_active: bool directly. The
reactive path computes it inline (one comparison) instead of going
through update_latches/LatchState. Sensor reads the new field and
no longer reports charging_ultimate_override. LatchState and
update_latches are deleted in the next commit."
```

---

## Task 6: Delete `LatchState` and `update_latches` from `ev_controller.py`

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py` — delete `LatchState` dataclass and `update_latches` function
- Modify: `tests/test_ev_controller.py` — delete `LatchState`/`update_latches` tests

- [ ] **Step 1: Verify nothing imports `LatchState` or `update_latches`**

```
grep -rn "LatchState\|update_latches" custom_components tests | grep -v __pycache__
```

Expected matches are only inside `ev_controller.py` itself and `tests/test_ev_controller.py`. If anything else turns up, fix that first.

- [ ] **Step 2: Delete the classes from `ev_controller.py`**

In `custom_components/pv_optimizer/ev_controller.py`, delete the entire `LatchState` dataclass (around lines 123-132) and the entire `update_latches` function (around lines 135-179). The remaining structure: `EVStateClass`, `DEFAULT_STATE_VOCAB`, `classify_state`, `ReactiveDecision`, `decide_reactive`, `is_session_done`, `translate_lp_slot0`.

- [ ] **Step 3: Delete the LatchState/update_latches tests**

In `tests/test_ev_controller.py`:

- Delete the imports `LatchState, update_latches` (around line 213-216).
- Delete the section header comment "Task 7: LatchState / update_latches" if present.
- Delete `test_latches_start_clear` (around line 219).
- Delete `test_cheap_grid_latch_trigger_and_release` (around line 225) — this tested `update_latches`'s behavior; the inline cheap-grid logic in the planner is covered by the existing reactive cheap-grid test in `test_planner.py`. If you want a unit-level test that `price_buy <= threshold → cheap_grid_active = True`, that's a one-liner in the planner — not worth a dedicated test.
- Delete `test_ultimate_override_latch_triggers_on_denied_request` (around line 253).
- Delete `test_ultimate_override_latch_does_not_flap_during_charge` (around line 265).
- Delete `test_ultimate_override_releases_after_dwell_out_of_requesting` (around line 289).

- [ ] **Step 4: Run the test suite**

```
pytest tests/
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "refactor(ev): remove LatchState and update_latches

With ultimate_override gone and cheap_grid inlined into the planner,
the LatchState dataclass and update_latches function have no callers.
Delete them and their dedicated tests."
```

---

## Task 7: Rename select options and add `manual → car` migration

**Files:**
- Modify: `custom_components/pv_optimizer/select.py` — options list + migration in `async_added_to_hass`
- Test: `tests/test_ev_controller.py` — pure helper for the migration logic

- [ ] **Step 1: Add a pure migration helper + test**

In `custom_components/pv_optimizer/select.py`, add this module-level helper just below the imports (before `async_setup_entry`):

```python
def _migrate_legacy_mode(stored: str | None) -> str | None:
    """Map a restored mode value through legacy aliases.

    Returns the stored string unchanged unless it's a legacy name; in
    that case returns the current canonical name. Pure so callers can
    test it without booting Home Assistant.
    """
    if stored == "manual":
        return "car"
    return stored
```

Add to `tests/test_ev_controller.py` (end of file is fine):

```python
def test_select_migrates_manual_to_car() -> None:
    from custom_components.pv_optimizer.select import _migrate_legacy_mode
    assert _migrate_legacy_mode("manual") == "car"
    assert _migrate_legacy_mode("auto") == "auto"
    assert _migrate_legacy_mode("car") == "car"
    assert _migrate_legacy_mode("off") == "off"
    assert _migrate_legacy_mode(None) is None
```

- [ ] **Step 2: Run the migration test**

```
pytest -xvs tests/test_ev_controller.py::test_select_migrates_manual_to_car
```

Expected: pass (the helper is in place).

- [ ] **Step 3: Update the options list and call the helper in `async_added_to_hass`**

In `custom_components/pv_optimizer/select.py`, change `_attr_options` (line 22) from:

```python
    _attr_options = ["auto", "manual", "off"]
```

to:

```python
    _attr_options = ["auto", "car", "off"]
```

Update `async_added_to_hass` (around lines 42-46) from:

```python
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._state = last.state
```

to:

```python
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        migrated = _migrate_legacy_mode(last.state if last else None)
        if migrated in self._attr_options:
            self._state = migrated
```

- [ ] **Step 4: Run the suite**

```
pytest tests/
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/select.py tests/test_ev_controller.py
git commit -m "feat(ev): rename mode select option manual to car (with migration)

select.pv_optimizer_ev_mode now offers auto/car/off. A restored
'manual' state migrates silently to 'car' so existing installs
survive the rename. Migration logic extracted as a pure helper
for testability."
```

---

## Task 8: Sensor — add explicit `off` and `car_mode` precedence

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py` — add `EVRuntimeState.last_mode`, set it in `_apply_ev`
- Modify: `custom_components/pv_optimizer/sensor.py` — new precedence
- Modify: `tests/test_planner.py` — coverage for the new states

- [ ] **Step 1: Add `last_mode` to `EVRuntimeState`**

In `custom_components/pv_optimizer/planner.py`, add to `EVRuntimeState` (alongside the other tracking fields):

```python
    last_mode: str | None = None
```

In `_apply_ev`, immediately after `mode = self._read_mode()` (around line 696), set:

```python
        if self.ev_state is not None:
            self.ev_state.last_mode = mode
```

- [ ] **Step 2: Update the sensor with new precedence**

In `custom_components/pv_optimizer/sensor.py`, find `_EVStatusSensor.native_value` (around line 201) and replace the body with:

```python
    @property
    def native_value(self) -> str | None:
        c = self._cycle
        if c is None:
            return None
        ev_state = getattr(self.coordinator._planner, "ev_state", None)
        if ev_state is None or ev_state.last_state_class is None:
            return "disconnected"
        from .ev_controller import EVStateClass
        if ev_state.last_state_class == EVStateClass.DISCONNECTED:
            return "disconnected"
        # Mode-based checks come BEFORE activity-based ones so a stale
        # last_written_current_a from a previous session doesn't bleed
        # through after switching to 'off' or 'car'.
        if ev_state.last_mode == "off":
            return "off"
        if ev_state.last_mode == "car":
            return "car_mode"
        if c.result and c.result.slots and c.result.slots[0].p_ev_chg_kw > 0:
            return "charging_lp_planned"
        if getattr(ev_state, "cheap_grid_active", False):
            return "charging_cheap_grid"
        if ev_state.last_written_current_a and ev_state.last_written_current_a > 0:
            return "charging_surplus"
        return "idle"
```

- [ ] **Step 3: Confirm the sensor coverage via the existing planner test surface**

The integration's sensor doesn't have unit tests today (search: `grep -n "_EVStatusSensor\|EVStateSensor" tests/`). The simplest verification is end-to-end via the existing planner tests, which exercise the modes that set `last_mode`. Run the full suite:

```
pytest tests/
```

Expected: green. The `off`-mode planner test already exists (search `state="off"` in `test_planner.py`) and now also sets `ev_state.last_mode = "off"` indirectly via `_apply_ev`. No new test needed; behavior is observable through the test that already covers the `off`-mode branch by checking that no entities are written.

- [ ] **Step 4: Commit**

```bash
git add custom_components/pv_optimizer/planner.py custom_components/pv_optimizer/sensor.py
git commit -m "feat(ev): sensor reports off and car_mode explicitly

EVRuntimeState carries last_mode; sensor checks it before falling
through to activity-based states so a stale last_written_current_a
doesn't bleed through after switching to off/car."
```

---

## Task 9: Amend the original EV charging spec

**Files:**
- Modify: `docs/superpowers/specs/2026-05-27-ev-charging-design.md`

- [ ] **Step 1: Apply the §8 amendments from the redesign spec**

Open `docs/superpowers/specs/2026-06-01-ev-mode-redesign-design.md` §8 and apply each bullet to `docs/superpowers/specs/2026-05-27-ev-charging-design.md`. Concretely:

a) **§2 mode table** (around line 22-32): replace the existing table with three rows (`auto`, `car`, `off`) matching §2 of the redesign spec. Mention the auto-return switch and that `car` is sticky by default.

b) **§3.3 / §3.4** (state vocab + integration-created entities, around lines 70-105): change any reference to the option `"manual"` to `"car"`. Add a row for `switch.pv_optimizer_ev_car_auto_return` (default off; opt-in auto-return for car mode).

c) **§4.1** (latch list, around lines 109-141): drop the `ultimate_override_latch` row from the latch table. Note that cheap-grid is no longer a "latch" in the strict sense (no trigger/release asymmetry remains) — it's a per-tick predicate. Rewrite the §4.1 prose to reflect "cheap-grid predicate" instead of "two latches".

d) **§4.3 ultimate-override semantics**: delete the entire section.

e) **§5.3 slot-0 translation** (around line 264): remove the `Ultimate-override` pseudocode bullet at the top. Update the surrounding text accordingly. The remaining bullets: disconnect → 0; LP zero → 0; LP positive → translate (clamp up below min).

f) **§5.4 mode switching**: skim for any mention of override — remove.

g) **§6 "Manual Override and Off Modes"**: rename header to "Car Mode and Off Modes". Rewrite §6.1 as "Car mode (`mode = car`)" — sticky by default, opt-in auto-return via switch. Keep §6.2 (off mode) substantively unchanged; just confirm the wording still applies.

h) **§7 diagnostic sensors**: align the list with §6 of the redesign spec (state strings: `disconnected`, `off`, `car_mode`, `charging_lp_planned`, `charging_cheap_grid`, `charging_surplus`, `idle`; remove `charging_ultimate_override` and `manual_override`).

i) **§8 edge cases**: the "Manual override while car already full" bullet becomes "Car mode + auto-return OFF while car already full" — the planner just keeps writing every tick, no exit.

j) Add a short note at the very top of the file:

```markdown
> **Note (2026-06-01):** EV-mode portions of this spec were superseded by
> `2026-06-01-ev-mode-redesign-design.md`. The amendments are folded in
> below; see the redesign spec for the rationale.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-05-27-ev-charging-design.md
git commit -m "docs(ev): amend original EV spec for the auto/car/off redesign

Fold the §8 amendments from 2026-06-01-ev-mode-redesign-design.md
into the original EV charging spec so the canonical reference stays
correct."
```

---

## Final verification

- [ ] **Run the whole suite one more time**

```
pytest tests/
```

Expected: all green.

- [ ] **Skim git log to confirm the commit story is coherent**

```
git log --oneline ev_charging ^main
```

Expected: nine new commits (Tasks 1-9), each self-contained, in the order above.

- [ ] **Read the diff and make sure nothing snuck in**

```
git diff main...ev_charging -- custom_components tests docs
```

Expected: the changes match this plan. No stray edits to unrelated files; no `.venv/` or `__pycache__/` noise.
