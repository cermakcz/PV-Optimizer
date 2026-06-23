# EV-triggered on-demand re-plan — design

Date: 2026-06-23
Status: Approved (pending spec review)

## Problem

The optimizer runs on a single fixed clock: `PvOptimizerCoordinator` is a
standard HA `DataUpdateCoordinator` with a fixed `update_interval`
(`coordinator.py:237`), and every cycle calls `self._planner.step(...)`. There
is no separate fast reactive loop — both the LP plan and the reactive charging
decision are only recomputed once per cycle.

Consequently, when the user plugs in the car, nothing happens until the next
scheduled cycle. With a 3-minute round time that is up to a 3-minute wait
before charging starts. The same lag applies to changing any EV input
(target, deadline, planned start, mode).

## Goal

Re-plan immediately when a relevant EV entity changes state, so plugging in
(or changing an EV setting) engages charging within seconds instead of waiting
for the next cycle. No change to planner/optimizer logic — only the trigger
cadence.

## Approach

Register a Home Assistant state-change listener on the relevant EV entities and
have it request an immediate coordinator refresh.

This was chosen over a manual "Re-plan now" button: a button does not actually
remove the wait (the user would still have to open the app and tap it on
plug-in). Auto-reaction fixes the real annoyance. The button was explicitly
declined.

### Trigger entities

Listener fires on a state change of any of these (all sourced from `EVConfig`):

- `charger_state_entity` — the plug-in/charging connection state (primary)
- `target_kwh_entity` — `number.pv_optimizer_ev_target_kwh`
- `target_pct_entity` — `number.pv_optimizer_ev_target_pct`
- `deadline_entity` — `datetime.pv_optimizer_ev_deadline`
- `planned_start_entity` — `datetime.pv_optimizer_ev_planned_start`
- `mode_entity` — `select.pv_optimizer_ev_mode`

The **state** entity is watched deliberately, not the charging-power entity, so
normal charging-wattage fluctuations never trigger a (CPU-bound) LP solve.

### Wiring

In `__init__.py` `async_setup_entry`, after the coordinator is created and only
when `ev_cfg is not None`:

```python
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.core import callback

ev_trigger_entities = [
    ev_cfg.charger_state_entity,
    ev_cfg.target_kwh_entity,
    ev_cfg.target_pct_entity,
    ev_cfg.deadline_entity,
    ev_cfg.planned_start_entity,
    ev_cfg.mode_entity,
]

@callback
def _ev_changed(event):
    hass.async_create_task(coord.async_request_refresh())

entry.async_on_unload(
    async_track_state_change_event(hass, ev_trigger_entities, _ev_changed)
)
```

All six entities are guaranteed non-empty whenever `ev_cfg is not None`
(`charger_state_entity` is required for `ev_cfg` to be built, and the other five
are hard-coded entity ids), so no empty-string filtering is required. A
defensive `if e` filter may still be applied for robustness.

### Why this is safe

- `DataUpdateCoordinator.async_request_refresh()` is already debounced by HA
  (`Debouncer` with `immediate=True` plus a cooldown). The first change — the
  plug-in — fires an immediate re-plan; rapid follow-up bounces within the
  cooldown coalesce into one solve. Instant response, no CPU hammering.
- `entry.async_on_unload(...)` mirrors the existing update-listener pattern at
  `__init__.py:142`, so the listener detaches on reload/unload — no
  double-registration.
- A change to `unavailable`/`unknown` still triggers a refresh, but the planner
  already does optional reads with defaults and handles missing values
  gracefully, so an extra re-plan is harmless. No new failure modes.

## Data flow

Plug in → `charger_state_entity` transitions disconnected→connected →
`_ev_changed` → `coord.async_request_refresh()` → `_planner.step()` re-runs with
`connected=True` → reactive/LP path engages → charging starts within a second or
two instead of up to one full cycle.

## Out of scope

- Manual "Re-plan now" button (declined).
- Reacting to non-EV inputs (prices, force-export switch, etc.) — the existing
  periodic cycle continues to cover those.
- Any change to planner, optimizer, or reactive decision logic.

## Testing

- The wiring lives in HA-only code (`__init__.py`), so an integration-style test
  asserts the listener is registered for exactly the expected entity set and
  that firing a state-change event schedules a coordinator refresh.
- Planner behavior is unchanged, so existing planner/optimizer tests remain the
  coverage for charging decisions.
