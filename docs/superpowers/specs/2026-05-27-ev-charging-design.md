# EV Charging Support — Design

> **Note (2026-06-01):** EV-mode portions of this spec were superseded by
> `2026-06-01-ev-mode-redesign-design.md`. The amendments are folded in
> below; see the redesign spec for the rationale.

## 1. Purpose

Extend `pv_optimizer` to control an EV charger as part of the household
energy plan. The integration must be brand-agnostic (control via configured
HA entities, no charger-specific code paths) and must coexist with the
existing home-battery + grid optimization without compromising it.

Two charging regimes are supported:

- **LP-planned** — when the user has set both a charge target (kWh or %) and
  a deadline and the car is connected, EV charging is a decision variable in
  the existing LP and is co-optimized with home battery + PV + grid.
- **Reactive** — otherwise, a per-tick controller decides whether/how much
  to charge based on PV surplus and a configurable cheap-grid price floor.

A `car` mode (sticky max-current) and an `off` bypass complete the user-facing mode set.

## 2. Operational Modes

A single integration-created `select.pv_optimizer_ev_mode` entity governs
top-level behaviour. Default `auto`.

| Mode  | Behaviour |
|-------|-----------|
| `auto` | If car connected AND `target_kwh > 0` AND `deadline > now` → LP-planned path. Else → reactive path (cheap-grid predicate + surplus tracking via EVCS). Does **not** react to the car's `CONNECTED_REQUESTING` + low-power signal. |
| `car`  | Writes ACTIVE charger mode + `max_charging_current_a` + start switch ON every tick. **Sticky** by default — stays until the user explicitly switches back. Auto-returns to `auto` on session-done **only** when `switch.pv_optimizer_ev_car_auto_return` is ON (default OFF). |
| `off`  | Integration writes nothing to any EV output entity. Diagnostic sensors continue to read inputs. |

Mode transitions are user-driven. The `car → auto` auto-return is optional, gated by `switch.pv_optimizer_ev_car_auto_return`.

## 3. Configuration Surface

All EV-related config is optional at install time; leaving it blank disables
the feature entirely. When enabled, a new "EV charging" step is added to the
config + options flows.

### 3.1 Static parameters

| Knob | Default | Notes |
|---|---|---|
| `ev_max_charging_power_kw` | — | Charger's max delivered power at its max-current setpoint. |
| `ev_max_charging_current_a` | — | Charger's max-current setting that yields the above power. The ratio `kw_per_amp = ev_max_charging_power_kw / ev_max_charging_current_a` is the only conversion factor the integration uses; no phase or voltage math. |
| `ev_min_charging_current_a` | `6` | Minimum the charger will accept (J1772/Type 2 chargers typically refuse below ~6 A). |
| `ev_buy_price_threshold` | `0` | `your_currency`/kWh. Reactive cheap-grid floor — slots with `price_buy ≤ threshold` trigger grid-charging in the reactive path. |
| `ev_car_battery_kwh` | — | Capacity used for `target_pct ↔ target_kwh` conversion. |
| `ev_current_tolerance_a` | `1` | Hysteresis on max-current writes (mirrors `setpoint_tolerance_w`). |
| `ev_session_done_power_w` | `100` | Charging-power threshold for the session-done detector. |
| `ev_session_done_seconds` | `60` | Sustained-duration requirement for session-done. |

### 3.2 Configured entity IDs (user wires their charger)

**Inputs:**

| ID | Required | Notes |
|---|---|---|
| `ev_charger_state_entity` | yes | String state; classified into three classes via the vocabulary in §3.3. |
| `ev_charging_power_entity` | yes | Instantaneous W or kW. Drives surplus tracking and session-done detection. |
| `ev_session_energy_entity` | no | kWh delivered this session. If absent, integration integrates `ev_charging_power_entity`. |

**Outputs:**

| ID | Required | Notes |
|---|---|---|
| `ev_max_current_entity` | yes | `number` entity (A) the integration writes to. |
| `ev_start_switch_entity` | no | `switch` entity; written `on` when `max_current > 0`, `off` when `0`. Some chargers need both. |
| `ev_charger_mode_entity` | no | `select`-like entity for the charger's own native mode. Enables the "mode-switching" reactive variant (§4.1). |

### 3.3 State vocabulary

`ev_charger_state_entity` values are classified into one of three classes.
Matching is case-insensitive against a fixed list of substrings (the
charger publishes `Wait sun` or `wait_sun` — both classify the same):

- **`disconnected`** — no car (default substrings: `disconnect`, `idle`,
  `unplug`)
- **`connected_idle`** — plugged in, not requesting (default substrings:
  `charged`, `connect` *(only when no `requesting` substring also
  matches)*)
- **`connected_requesting`** — car wants power (default substrings:
  `charging`, `wait sun`, `wait_sun`, `wait time`, `wait start`,
  `wait rfid`)

The mapping is overridable in the config flow for chargers whose
vocabulary differs from the defaults — the user supplies a list of
substrings per class. Classification precedence is
`disconnected` → `connected_requesting` → `connected_idle`, so an unknown
state safely defaults to `connected_idle` (treated as plugged-in-but-not-requesting,
which is the most conservative classification).

**Caveat (documented in README, not encoded):** some chargers collapse the
`connected_requesting` state to `connected_idle` when the integration sets
`max_current = 0` in the charger's "car" mode. For chargers where that's
the case (EVCS NS included), the user can either configure
`ev_charger_mode_entity` to enable mode-switching (§4.1) or accept the
trade-off that the cheap-grid predicate (§4.1) works only when the request
signal survives suppression.

### 3.4 Integration-created entities

| Entity | Purpose |
|---|---|
| `select.pv_optimizer_ev_mode` | `auto` / `car` / `off`, default `auto`. |
| `switch.pv_optimizer_ev_car_auto_return` | When ON, `car` mode auto-returns to `auto` on session-done. Default OFF (sticky). |
| `number.pv_optimizer_ev_target_kwh` | Current session target in kWh. |
| `number.pv_optimizer_ev_target_pct` | Same target expressed as % of `ev_car_battery_kwh`. Writing one updates the other. |
| `datetime.pv_optimizer_ev_deadline` | Deadline for the LP-planned charging. |

## 4. Reactive Auto Algorithm

Runs when `mode == auto` AND the LP-planned path is not engaged (no target,
no deadline, deadline in the past, or car not connected).

### 4.1 With mode-switching (`ev_charger_mode_entity` configured)

Resting state: charger in its native solar-only / passive mode (e.g. EVCS NS
"Auto"), `max_current = ev_max_charging_current_a`. The charger handles
surplus natively and keeps the request signal clean.

Per tick the integration evaluates a single **cheap-grid predicate**:

```
cheap_grid_active = price_buy[now] <= ev_buy_price_threshold
```

This is a per-tick Boolean — no trigger/release asymmetry. When `cheap_grid_active`
is True, the integration switches the charger to active-control mode (e.g. EVCS
NS "Manual") and writes `max_current = ev_max_charging_current_a`. When False,
the charger returns to its passive resting mode and the EVCS handles surplus
natively.

Car mode is handled at the outer mode level (§6) rather than here.

### 4.2 Without mode-switching (`ev_charger_mode_entity` not configured)

In this branch the charger is assumed to be in its active-control mode at
all times (a one-time setup by the user). The integration is solely
responsible for the surplus / cheap-grid decisions.

Per planner tick:

1. Classify `ev_charger_state` → `disconnected` / `connected_idle` /
   `connected_requesting`.
2. **`disconnected`** → `max_current = 0`; bail.
3. **Cheap grid** (`price_buy[now] ≤ ev_buy_price_threshold`) →
   `max_current = ev_max_charging_current_a`.
4. **Else — surplus tracking:**

   ```
   surplus_kw = max(0, (-grid_power_w + ev_charging_power_w) / 1000)
   target_a   = surplus_kw / kw_per_amp
   ```

   The `+ ev_charging_power_w` term back-adds what the EV is already
   drawing so the loop converges to "consume all surplus" instead of
   ramping itself down to zero. The home battery takes surplus first
   via passive self-consumption, so `grid_power < 0` means the battery
   is full and there is true leftover available to the EV.

   - If `target_a < ev_min_charging_current_a` → `max_current = 0`
     (skip-below-min).
   - Else → `max_current = clamp(target_a, ev_min_charging_current_a,
     ev_max_charging_current_a)`.

Setpoint writes only happen when the new value differs from the last
written value by more than `ev_current_tolerance_a`. Dropping to 0
requires sustained-below-min for one tick (avoids reacting to a single-tick
surplus dip).

### 4.3 Start switch

If `ev_start_switch_entity` is configured, the integration writes `on`
whenever `max_current > 0` and `off` when `max_current = 0`. Some chargers
need both signals to actually deliver power.

### 4.4 Cadence

Runs at the existing planner cadence (`update_seconds`, default 300 s).
Sufficient for typical surplus tracking on residential PV; users wanting
sub-minute cloud tracking lower `update_seconds`.

## 5. LP Extension for Deadline-Based Charging

Engaged when `mode == auto` AND car connected AND `target_kwh > 0` AND
`deadline > now`. Otherwise the LP runs unchanged (no EV variables added —
regression no-op for existing users).

### 5.1 Optimizer additions

One decision variable per slot, mirroring the §8.5 soft-floor pattern from
the existing PRD:

```
p_ev_chg[t] ∈ [0, ev_max_kw_per_slot[t]]
  where ev_max_kw_per_slot[t] = ev_max_charging_power_kw  if slot.start < deadline
                                else                   0
```

Extended power balance:

```
pv[t] + p_dis[t] + p_buy[t]
  == load_house[t] + p_ev_chg[t] + p_chg[t] + p_sell[t] + p_curt[t]
```

Soft total-energy constraint:

```
ev_deficit ≥ 0
ev_deficit ≥ remaining_kwh - Σ_t (dt[t] · p_ev_chg[t])
```

Objective gains `ev_deficit_penalty · ev_deficit`. The penalty is sized
above the highest realistic `price_buy` over the horizon (e.g., `100 ·
max(price_buy)`) so the LP prefers importing at peak prices over leaving
the target unmet. The slack keeps the LP feasible when capacity before
deadline is genuinely insufficient (deadline too soon, plug-in too late) —
graceful degradation rather than `OptimizerError`.

No `cycle_cost` term is added for `p_ev_chg` itself; the home battery's
`p_dis` already carries its own wear cost, and the LP correctly charges
the EV from `p_dis` only when `cycle_cost < price_buy`.

### 5.2 Planner bookkeeping per tick

```
energy_delivered_session_kwh =
  ev_session_energy_entity (if configured)
  else integrated ev_charging_power_entity since plug-in

remaining_kwh = max(0, target_kwh - energy_delivered_session_kwh)
```

When `remaining_kwh == 0` the LP-planned path falls back to reactive (no EV
variables added). When `deadline` passes with `remaining_kwh > 0`, all
`ev_max_kw_per_slot[t]` are 0, deficit absorbs the gap, the LP plan
becomes the zero plan for EV, and reactive takes over for any remaining
opportunistic charging.

### 5.3 Slot-0 translation

```
if state_class == DISCONNECTED:
    max_current = 0
elif p_ev_chg[0] == 0:
    max_current = 0
else:
    target_a    = p_ev_chg[0] / kw_per_amp
    max_current = clamp(target_a, ev_min_charging_current_a,
                                  ev_max_charging_current_a)
```

When the LP plans `p_ev_chg[0] > 0` but the converted current is below
min, **clamp upward** to `ev_min_charging_current_a` — the user has
committed to a target, so a minor slot-0 overshoot is acceptable. The
next tick re-plans with reduced `remaining_kwh`. This contrasts with the
reactive path's skip-below-min (§4.2) and is intentional.

When the LP plans `p_ev_chg[0] = 0`, the planner writes `0` even if the
car is in `CONNECTED_REQUESTING` state — the user's LP plan takes
precedence over the car's request signal.

### 5.4 Mode switching

If `ev_charger_mode_entity` is configured, the charger is switched to its
active-control mode (e.g. EVCS NS "Manual") whenever the slot-0 logic
above writes a non-zero `max_current` — i.e., when `p_ev_chg[0] > 0`.
When the slot-0 write is 0, the charger returns to its resting passive
mode. Same dwell hysteresis as §4.1.

### 5.5 Session reset

A `disconnected → connected` transition resets the internal session
integrator (`energy_delivered_session_kwh = 0`). The user's `target_kwh`
entity is **not** auto-reset — it persists across sessions until the user
changes it.

## 6. Car Mode and Off Modes

### 6.1 Car mode (`mode = car`)

Per tick while in car mode:

- `max_current = ev_max_charging_current_a`
- If `ev_start_switch_entity` configured → `on`
- If `ev_charger_mode_entity` configured → active-control mode

Setpoint-write tolerance (§4) still applies.

`car` mode is **sticky by default** — it stays until the user explicitly
switches back to `auto` or `off`. No auto-exit occurs unless
`switch.pv_optimizer_ev_car_auto_return` is ON.

**Optional auto-return** (requires `switch.pv_optimizer_ev_car_auto_return` =
ON): auto-return to `auto` when "session done" fires, defined as either:

- `ev_charger_state` classifies to `disconnected`, OR
- `ev_charger_state` classifies to `connected_idle` AND
  `ev_charging_power < ev_session_done_power_w` for ≥
  `ev_session_done_seconds`, AND the car has drawn ≥ `ev_session_done_power_w`
  at some point during the session (guards against EVCS-side gating
  false-positives).

When session-done fires and auto-return is ON, the integration writes `auto`
back into `select.pv_optimizer_ev_mode`.

Interaction with LP target: car mode fully precedes the LP path while
engaged. After auto-return, if `remaining_kwh > 0` and `deadline > now` and
the car is still plugged in (rare — typically only when the car physically
reached full before the LP target was reached), the LP path resumes on the
next tick. Harmless — the car won't draw more than its physical capacity
allows.

### 6.2 Off mode (`mode = off`)

Integration writes nothing to `ev_max_current_entity`,
`ev_start_switch_entity`, or `ev_charger_mode_entity` for the duration.
All EV diagnostic sensors continue to update by reading inputs, so the
user can observe charger / car state while the integration is a passive
observer. No auto-exit — user explicitly changes the mode select to leave.

## 7. Diagnostic Sensors

Created by the integration:

- `sensor.pv_optimizer_ev_status` — one of the following strings,
  evaluated in precedence order (first match wins):
  1. `disconnected` — `state_class == DISCONNECTED`.
  2. `off` — mode is `off`.
  3. `car_mode` — mode is `car`, regardless of LP plan or power flow.
  4. `charging_lp_planned` — LP plan slot 0 has `p_ev_chg_kw > 0`.
  5. `charging_cheap_grid` — auto + reactive + `cheap_grid_active`.
  6. `charging_surplus` — auto + reactive + last written current > 0.
  7. `idle` — connected, none of the above.

  The mode-based checks (`off`, `car_mode`) come before the
  activity-based ones so a stale `last_written_current_a` from a
  previous session does not bleed through after switching modes.

- `sensor.pv_optimizer_ev_session_energy_kwh` — energy delivered since
  last plug-in event.
- `sensor.pv_optimizer_ev_remaining_kwh` — `max(0, target_kwh -
  session_energy_kwh)`.
- `sensor.pv_optimizer_ev_planned_current_a` — last value written to
  `ev_max_current_entity`.
- `sensor.pv_optimizer_ev_deficit_kwh` — LP slack from §5.1; non-zero
  means deadline unachievable.

The existing `sensor.pv_optimizer_plan` attributes (PRD §9) gain a
`p_ev_chg_kw` field on each per-slot entry so dashboards can chart EV
power as part of the same plan series.

## 8. Edge Cases

- **Charger state input unavailable** (`unknown` / `unavailable` /
  `None`): treat as `disconnected`. Log a warning once per state-change;
  no writes attempted.
- **Target set without deadline (or vice versa):** LP path doesn't
  engage; reactive path runs.
- **Deadline in the past:** LP path doesn't engage.
- **Negative `target_kwh` or `target_pct`:** clamped to 0, warning
  logged.
- **Mid-session disconnect/reconnect:** session integrator resets on the
  next `disconnected → connected` transition; `target_kwh` persists.
- **Car mode + auto-return OFF while car already full:** integration
  writes max current every tick indefinitely. Charger reports `Charged`
  but no session-done exit occurs — the planner just keeps writing
  every tick with no auto-return. User must manually switch mode to exit.
- **Config error** (`ev_max_charging_current_a ≤ 0`, `ev_max_charging_power_kw ≤ 0`):
  rejected at config-flow validation.
- **Output write ordering per tick:** mode → start switch → max_current.
  Some chargers interpret `max_current` only after their mode is set.

## 9. Testing Strategy

Mirrors PRD §10.

- `tests/test_optimizer.py` extended: LP correctness with the EV variable
  — cheap-hour scheduling, infeasible-deadline soft-deficit, zero-target
  regression no-op (no variables created when EV inputs are absent).
- `tests/test_planner.py` extended: state-vocabulary classification
  (default + override), reactive surplus tracking convergence with the
  `+ ev_charging_power_w` term, mode-switching (configured / unconfigured),
  cheap-grid branch, skip-below-min, car mode + auto-return switch
  (ON/OFF), off-mode write-suppression, plug-in session integrator reset.
- `tests/test_ev_controller.py` (new, mirroring
  `tests/test_load_forecaster.py`): the pure reactive decision function
  — surplus math, cheap-grid predicate, min-current floor, hysteresis.

HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`) remain
thin shims and are exercised in a live HA instance, not in this
repository's CI.
