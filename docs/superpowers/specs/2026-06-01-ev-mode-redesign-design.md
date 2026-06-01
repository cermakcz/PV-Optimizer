# EV mode redesign — remove ultimate-override, rename manual → car

Status: approved 2026-06-01.
Supersedes the EV-mode portions of `2026-05-27-ev-charging-design.md`
(§2 mode table, §4.3 ultimate-override latch, §5.3 ultimate-override
branch in slot-0 translation, §6 manual override, §7 diagnostic state
strings). That document is amended in-place as part of implementation.

## 1. Problem

Today's `auto` mode pegs to `max_charging_current_a` the moment a freshly
plugged-in car enters a `CONNECTED_REQUESTING` state with low power draw
— the "ultimate-override" latch in `update_latches`, mirrored by
short-circuits in `decide_reactive` and `translate_lp_slot0`. This
defeats two legitimate intents:

1. **LP-planned charging.** A user who has set `target_kwh + deadline`
   wants the optimizer to schedule charging slot-by-slot across the
   horizon. The override forces max current regardless of the plan.
2. **Surplus-only charging.** A user with no target who wants to charge
   only from PV surplus gets max-grid-import instead, because the
   override fires before the surplus / passive-mode handoff to the
   EVCS's own solar logic.

The override exists to handle a third intent — "I plugged in, give it
power now, no plan needed" — which deserves its own explicit user
choice, not an implicit override that overrides everything else.

## 2. Mode surface

Three modes on `select.pv_optimizer_ev_mode`:

| Mode  | Behavior                                                                                                                                                                                                                                                          |
|-------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `auto` | LP plan if `target_kwh > 0 AND deadline > now`; otherwise reactive surplus + cheap-grid latch via the EVCS (passive charger-mode write). Does **not** react to the car's REQUESTING+low-power signal.                                                              |
| `car`  | Writes ACTIVE charger mode + `max_charging_current_a` + start switch ON every tick. **Sticky** — stays until the user explicitly switches it back. Auto-returns to `auto` on session-done **only** when `switch.pv_optimizer_ev_car_auto_return` is ON.            |
| `off`  | No EV writes. (Unchanged.)                                                                                                                                                                                                                                        |

The `manual` option is gone. `_EVModeSelect.async_added_to_hass`
silently migrates a restored `"manual"` to `"car"` before the
options-membership check, so existing installs survive the rename
without user action.

## 3. New entity: car-mode auto-return switch

`switch.pv_optimizer_ev_car_auto_return`, default OFF, `RestoreEntity`
across restarts.

- OFF (default): `car` mode is sticky. No auto-return. The
  `manual_session_charging_seen` bookkeeping is skipped.
- ON: `car` mode preserves today's auto-return behavior — return to
  `auto` on physical disconnect, or on `CONNECTED_IDLE` with
  `ev_power < session_done_power_w` for ≥ `session_done_seconds`
  *provided* the car has drawn ≥ `session_done_power_w` at some point
  during the session (the `manual_session_charging_seen` guard against
  EVCS-side gating false-positives).

Implementation requires:

- A new `switch.py` platform module mirroring `select.py`'s shape.
- `"switch"` added to `PLATFORMS` in `const.py`.
- `EVConfig.car_auto_return_entity: str = "switch.pv_optimizer_ev_car_auto_return"`.
- A `_read_bool_optional(cfg.car_auto_return_entity, default=False)`
  call inside the `car` branch of `_apply_ev`.

## 4. Removals in `ev_controller.py`

- Delete `LatchState.ultimate_override`. Since `cheap_grid` is then the
  only field, collapse the dataclass entirely: callers track a single
  `cheap_grid_active: bool` directly.
- Delete `update_latches`. Replace each call site with the one-liner
  `cheap_grid_active = price_buy <= ev.buy_price_threshold`. The
  ultimate-override branch (the asymmetric trigger/release block at
  `ev_controller.py:166-177`) goes away with it.
- Delete the `state_class == CONNECTED_REQUESTING AND
  ev_charging_power_w < ev.session_done_power_w` short-circuit in
  `decide_reactive` (`ev_controller.py:105-107`). Function flows
  straight to the cheap-grid + surplus + skip-below-min path.
- Delete the same short-circuit in `translate_lp_slot0`
  (`ev_controller.py:225-227`). When the LP plans `0`, the planner
  writes `0` even if the car is asking — the user's plan takes
  precedence over the car's request.

`is_session_done` and `ReactiveDecision` stay as-is.

## 5. Changes in `planner.py`

- `EVRuntimeState.latches: LatchState` → `cheap_grid_active: bool = False`.
- `_apply_ev`:
  - Read mode as before (`"auto"` / `"car"` / `"off"`), matching `"car"`
    where today's code matches `"manual"`.
  - **`off` branch:** unchanged.
  - **`car` branch:** read
    `car_auto_return = self._read_bool_optional(cfg.car_auto_return_entity, default=False)`.
    - If `car_auto_return` is True: keep today's `manual_session_charging_seen`
      tracking and the `disconnected_done` / `idle_done` auto-return block
      (call `self._write_mode_auto()` and return on done). Maintenance of
      `manual_session_charging_seen` (set on power ≥ threshold, reset on
      `mode != "car"` or disconnect) lives inside this branch.
    - If `car_auto_return` is False: skip the auto-return entirely and
      skip the `manual_session_charging_seen` bookkeeping. Just write
      active mode + max current + start ON, every tick.
  - **`auto` branch with LP plan (`plan_first.p_ev_chg_kw > 0`):** call
    `translate_lp_slot0` (now without the override short-circuit); write
    active or passive mode based on whether the returned current is `> 0`;
    write the current; write start. Behavior matches today's LP path
    minus the override.
  - **`auto` branch reactive (no LP plan), with `charger_mode_entity`:**
    `cheap_grid_active = price_buy <= cfg.params.buy_price_threshold`.
    If True → active mode + max current + start ON. If False → passive
    mode + max current (EVCS owns the surplus decision). The
    ultimate-override branch is gone; the car's REQUESTING signal no
    longer changes what we write.
  - **`auto` branch reactive (no LP plan), without `charger_mode_entity`:**
    call `decide_reactive` (now without the override short-circuit);
    write current; write start.
- `EVConfig.car_auto_return_entity: str = "switch.pv_optimizer_ev_car_auto_return"`.
- `EVRuntimeState.manual_session_charging_seen` is renamed to
  `car_session_charging_seen` for hygiene and stays — it's still
  used by the auto-return-ON path. Reset semantics: reset on
  `mode != "car"`, on `state_class == DISCONNECTED`, or when the
  `car_auto_return` switch flips OFF mid-session.

## 6. Sensor diagnostic state (`sensor.py`)

`_EVStateSensor.native_value` returns one of the following, evaluated
in this precedence order (first match wins). The mode-based checks
come *before* the activity-based ones so a stale `last_written_current_a`
from a previous session doesn't bleed through after switching to `off`
or `car`:

1. `disconnected` — `state_class == DISCONNECTED`.
2. `off` — mode is `off`. (New explicit case.)
3. `car_mode` — mode is `car`, regardless of LP plan or power flow.
   (New explicit case. In `car` mode the planner always writes max
   current, so without this check `charging_surplus` would mis-fire.)
4. `charging_lp_planned` — LP plan slot 0 has `p_ev_chg_kw > 0`.
5. `charging_cheap_grid` — auto + reactive + `cheap_grid_active`.
6. `charging_surplus` — auto + reactive + last written current > 0.
   In the *with-`charger_mode_entity`* reactive path this fires
   whenever the planner has delegated to the EVCS (we always write
   max current passively in that branch), so the label means
   "delegated; EVCS decides" rather than literally "surplus drove
   it". Acceptable wart: the actual flow is still surplus-bounded
   by the EVCS's own logic. In the *without-`charger_mode_entity`*
   path the label remains literal (`decide_reactive` computed a
   nonzero current from grid-power surplus).
7. `idle` — connected, none of the above.

Removed: `charging_ultimate_override`. Added: `off`, `car_mode`.

## 7. Tests

Delete:

- `test_ev_controller.py::test_ultimate_override_latch_triggers_on_denied_request`
- `test_ev_controller.py::test_ultimate_override_latch_does_not_flap_during_charge`
- `test_ev_controller.py::test_ultimate_override_releases_after_dwell_out_of_requesting`
- `test_ev_controller.py::test_translate_ultimate_override_beats_lp_zero`
- The `assert not s.ultimate_override` line in
  `test_ev_controller.py:222` (and any other latch-shape assertions
  that referenced the field).

Rename: `"manual"` → `"car"` across `test_planner.py` (and the
`test_passive_cost_matches_manual` name in `test_optimizer.py` is
unrelated — leave it).

Add:

- Auto + reactive + REQUESTING + `ev_power < session_done_power_w`
  produces a *passive* charger-mode write and does **not** peg current
  to max (the regression the redesign fixes).
- Auto + LP plan + REQUESTING + low power writes the LP-derived current
  (e.g. 0 when LP plans 0; mid-range when LP plans mid-range), not max.
- Car mode + switch OFF: writes active+max+start every tick across
  several ticks spanning a session, never returns to auto on
  disconnect or idle dwell.
- Car mode + switch ON: matches today's auto-return — disconnect during
  car mode returns to auto; idle+low-power dwell after charging has
  been observed returns to auto; idle+low-power dwell without prior
  charging does NOT return (the `manual_session_charging_seen` guard).
- Car mode + switch flips OFF mid-session: bookkeeping resets cleanly,
  no spurious auto-return on the next tick.
- `_EVModeSelect`: a restored `"manual"` state lands as `"car"`.

## 8. Spec amendments

After implementation, update `docs/superpowers/specs/2026-05-27-ev-charging-design.md`:

- §2 mode table: rows for `auto` / `car` / `off`, with the behavior
  text above. Mention the sticky default and the auto-return switch.
- §3.3 / §3.4 if any reference to the `"manual"` option string.
- §4.1: drop `ultimate_override_latch` from the latch list; the
  remaining cheap-grid logic is no longer a "latch" — it's a per-tick
  predicate. Remove or fold §4.1 accordingly.
- §4.3 ultimate-override semantics: delete the section.
- §5.3 slot-0 translation: remove the ultimate-override branch from
  the pseudocode; the function reduces to LP-translate + DISCONNECTED
  short-circuit + zero short-circuit.
- §6 "Manual Override and Off Modes" → rename "Car Mode and Off Modes".
  Document the sticky default and the auto-return switch.
- §7 diagnostic state strings: align with §6 of this doc.
- §8 edge cases: the "manual override while car already full" entry
  becomes the `car` + auto-return-OFF case — clarify it just keeps
  writing every tick (no exit).

## 9. Out of scope

- The `cheap_grid` predicate's threshold semantics, hysteresis, and
  default value — unchanged.
- LP solver behavior — unchanged.
- The `planned_start` future-schedule gate in `_apply_ev` — unchanged.
- Vendor `charger_mode_option_*` string config — unchanged.
- Sensor entities other than `_EVStateSensor` — unchanged.
- Reclassifying `waiting_for_sun` into `CONNECTED_IDLE` — discussed
  earlier as a possible alternative; explicitly NOT done here, since
  the new `auto` behavior already gives the EVCS's solar gating room
  to run via the passive charger-mode write.
