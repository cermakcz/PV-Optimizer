# EV charging from curtailed solar surplus — design

Date: 2026-06-23
Status: Approved (pending spec review)

## Problem

The EVCS's native "Auto" mode detects PV surplus by watching **grid export**:
it ramps charging current to hold export near a threshold. The planner,
however, forces feed-in off whenever the slot's sell price is unattractive
(`feedin = first.p_sell_kw > _FORCE_EPS`, `planner.py:290`; the optimizer pins
`p_sell = 0` for those slots, and the per-slot `min_sell_price_per_kwh` floor
does the same).

That creates a **curtailment-blindness loop**:

1. Sun is producing more than the house consumes.
2. The home battery is full, so it can't absorb the surplus.
3. The slot's sell price is below the floor, so the planner has disabled
   export — the surplus cannot flow to the grid.
4. The inverter therefore **clips PV production** to match house load; grid
   power sits at ~0 ("the rest spills to grid or is curtailed",
   `planner.py:1069`).
5. The EVCS sees no export, concludes there is no surplus, and stays idle.

Free solar that the EV could absorb is thrown away. Crucially, the planner's
own `pv_power_entity` is **also** blind here: a clipping inverter reports the
*clipped* (consumed) production, not the *potential*, so `pv − load ≈ 0`. The
surplus is potential energy that no sensor measures.

This bites hardest in two no-LP-charge situations:

- **Handback / no target** — the reactive branch hands the charger to the EVCS
  (`planner.py:846`), which is blind.
- **Planned-start gate** — while `planned_start` is in the future the LP
  reserves charging for *later* slots, so the *current* clipped slot's surplus
  is wasted while the car sits idle (the gate path added in
  `fix(ev): surplus-charge during future planned-start gate` hands to passive
  "Auto", which is blind).

## Goal

When solar surplus is being curtailed and the EVCS cannot see it, have the
planner actively drive the charger to absorb that otherwise-wasted energy —
without ever exporting at a bad price and without pulling from the grid.

## Design decision: keep the asymmetric split (EVCS follows export)

We deliberately do **not** make the planner own surplus charging in all cases.
The dividing question is *"would this solar otherwise be wasted?"*

| Battery | Export this slot | Surplus | Who charges the EV |
|---------|------------------|---------|--------------------|
| Full | disabled (bad price) | yes | **Planner probe** (this design) — surplus is curtailed, EVCS blind |
| Full | enabled (ok price)   | yes | **EVCS Auto** — surplus is exported, EVCS follows it competently |
| Not full | either            | yes | Battery self-consumption absorbs it; EVCS Auto mostly idle |
| —       | —                  | no  | EVCS Auto idle (correct no-op) |

- **Export disabled** → the energy is genuinely wasted, so grabbing it is pure
  gain with no opportunity cost. A blind regulator is appropriate.
- **Export enabled** → the energy is *sellable*, so diverting it to the EV has
  an opportunity cost. That is a value decision for the LP (when a target
  exists) or the EVCS's own logic — not for a grid-following regulator.

So each controller runs where it is competent: the EVCS follows the export
boundary when there is one; the planner follows the import boundary only when
export is clamped and the EVCS can see nothing. They never run simultaneously.
The planner is uniquely able to make this call because it *set* the export
disable and knows the battery SoC.

## Approach

### Why a probe, not a measurement

Because clipped PV telemetry hides the potential surplus, the surplus cannot be
*measured* — it must be *discovered* by pushing load and watching the grid.
This is a zero-import regulator with the EV as the actuator: raise EV current
while grid import stays ≈ 0 (the extra load unclips more PV); when grid import
lifts, we have passed the available surplus and must back off.

The feedback is **one-sided**: under curtailment, grid import is ~0 for every
current at or below the available surplus, and only rises once we exceed it. So
we get a clear signal when we *overshoot* (step down), but discovering *more*
headroom is inherently speculative (step up and see).

### Arm condition

Evaluated in `_apply_ev`, which runs last in `step()` (`planner.py:303`) and
already receives `result.slots[0]` as `plan_first`. The probe arms when **all**
hold:

- `mode == "auto"` (and, for the gate, while `planned_start` is in the future).
- `plan_first.p_ev_chg_kw <= eps` — the LP is **not** already charging the EV
  (if it is, the LP owns the charger; don't interfere).
- `plan_first.p_sell_kw <= eps` — the LP is **not** exporting this slot. This is
  exactly the "EVCS follows export when export is enabled" boundary: a
  non-trivial `p_sell` means hand to the EVCS, not the probe.
- Battery effectively full: `battery_soc >= soc_max_kwh - SOC_FULL_EPS_KWH`
  (read live from `battery_soc_entity`). Below full, the battery itself absorbs
  surplus, so there is nothing being curtailed.
- PV forecast for the slot exceeds live load by a margin (`pv_forecast > load +
  PROBE_FORECAST_MARGIN_KW`) — a reason to believe surplus exists, so we never
  kick the charger awake on a dark/cloudy clamp.
- Car connected: `classify_state(charger_state) != DISCONNECTED`.
- A battery-power signal is available. The probe **requires** `battery_power_entity`
  (see "Detecting overshoot" below); if it is unconfigured, or its reading or
  `grid_power` is stale, the probe does not arm and we fall back to passive
  handback. Open-loop probing without these signals would drain the battery or
  import.

### Detecting overshoot: watch the battery discharge, not just grid import

A full battery still *discharges*. If we command more EV current than the PV
surplus supports, the inverter covers the gap from the battery first — grid
import stays ~0 until the battery is drained. So grid import is a *late*
indicator of overshoot; the *leading* indicator is the battery starting to
discharge. Watching only grid import would let the probe silently drain the home
battery into the car — a lossy round-trip we never want.

The probe therefore reads a **battery-power signal** and treats overshoot as
"battery discharging beyond a small threshold, **or** grid importing beyond a
small threshold." At the correct operating point under curtailment the battery
is idle (full, nothing to charge) and grid is ~0; the moment EV demand exceeds
PV, the battery discharge reading lifts first and triggers the down-step.

This requires a new optional `battery_power_entity` on `PlannerConfig`. The
deployment's entity reports signed power with **negative = discharging**; the
reader normalises this to a discharge-positive magnitude
(`discharge_w = max(0, -battery_power)`), so a unit/sign mismatch can't silently
read as "no discharge". The probe **requires** it: without a
battery-power reading we cannot distinguish "charging from surplus" from
"draining the battery", so the probe stays disabled and we fall back to passive
handback. We read an explicit entity rather than deriving battery flow from the
PV/load/grid balance, because clipped PV telemetry and load-sensor scope make
that derivation unreliable.

### Control law

A stateful regulator (contrast the stateless `decide_reactive`). Per cycle,
given the current commanded probe current, the battery discharge, and the
measured grid import:

- **Down (responsive):** if battery discharge exceeds `PROBE_DISCHARGE_CEILING_W`
  *or* grid import exceeds `PROBE_IMPORT_CEILING_W`, step down one amp
  immediately. Either means EV demand has passed the PV surplus; correct it
  before the battery drains or we pay for grid (also handles a cloud suddenly
  cutting PV).
- **Up (speculative, lazy):** at most one amp every `PROBE_UP_INTERVAL_CYCLES`,
  and only while not overshooting and forecast headroom remains. This is the
  only way to discover increased surplus, so it is rate-limited to keep the
  residual ±1 A blip rare and cheap.
- **Rest:** otherwise hold. The down-threshold deadband plus rate-limited
  up-probe means a steady clamp converges to a fixed current and then sits
  still.
- **Bias to undershoot:** prefer resting one amp *below* the true surplus.
  Leaving <1 A of free solar curtailed is the cheap error; importing is not.
- **Below minimum:** if the regulator would settle below `min_charging_current_a`,
  command 0 / start off (mirrors `decide_reactive`'s skip-below-min) and stay
  armed, waiting for more sun.

When armed, the planner writes active ("Manual") charger mode + the regulator's
current + start on/off — so the EVCS obeys the commanded current and the
inverter ramps production to feed it, keeping grid ≈ 0 with no export.

### Anti-oscillation

Limit-cycling is the main risk with a quantized actuator (integer amps,
one amp = `kw_per_amp`) and one-sided feedback. Three levers prevent it:

1. **Down-side ceilings** (`PROBE_DISCHARGE_CEILING_W` / `PROBE_IMPORT_CEILING_W`)
   give a deadband that absorbs sensor noise before a down-step fires.
2. **Rate-limited speculative up-probe** (`PROBE_UP_INTERVAL_CYCLES`) bounds the
   up direction. A failed up-probe (caused import) forces a down-step and the
   current latches; the next up-probe is at least an interval away, so the
   worst case is a small 1-amp import blip once per interval, not per cycle.
3. **Forecast-headroom gate** on up-probes suppresses pointless probing once the
   commanded current approaches the forecast potential.

The existing `current_tolerance_a` write-suppression (`planner.py:896`) further
ensures sub-threshold jitter never reaches the hardware.

The phase wrinkle: the deadband should exceed one amp's power. Three-phase
(~0.69 kW/A) implies a wider effective rest band — up to ~1 A of free solar
left curtailed at rest — than single-phase (~0.23 kW/A). Defaults below are
initial values to validate empirically; they may later be derived from
`kw_per_amp`.

### Disarm

The probe disarms — the planner writes passive "Auto" to hand back to the EVCS
and resets probe state — when any arm condition fails: export re-enabled,
battery no longer full, car disconnected, or the gate window ends.

No stickiness is applied to the export-re-enable transition. The sell price is
hourly, so a crossing of the floor flips the charger Manual↔Auto at most about
once per hour — acceptable responsiveness, not churn.

One subtlety: the probe's *own* brief overshoot (a down-correction may dip SoC a
hair below `soc_max`) must not trip the battery-full disarm. So disarm uses a
wider SoC margin than arm: stay armed while
`soc >= soc_max_kwh - SOC_DISARM_EPS_KWH` with
`SOC_DISARM_EPS_KWH > SOC_FULL_EPS_KWH`.

### Module structure

Following the existing split (`ev_controller.py` owns pure, HA-free decision
logic, tested by `tests/test_ev_controller.py`):

- New pure helpers in `ev_controller.py`:
  - `should_probe_surplus(...) -> bool` — the arm predicate.
  - `decide_surplus_probe(...) -> SurplusProbeDecision` — the regulator step
    (next current + updated counters) given battery discharge, grid import,
    current setpoint, and probe state.
- New optional `battery_power_entity` on `PlannerConfig` (signed, negative =
  discharging; normalised to discharge-positive on read), required for the probe
  to arm.
- New probe fields on `EVRuntimeState` (`planner.py`): commanded probe current,
  cycles-since-up counter, armed flag. Reset on disarm / disconnect.
- `_apply_ev` wires both no-LP-charge paths (the planned-start gate and the
  reactive branch) through the same arm-check + regulator, so they cannot drift.

### Tunable defaults (initial, to validate empirically)

- `SOC_FULL_EPS_KWH = 0.2` — how close to `soc_max_kwh` counts as "full" (arm).
- `SOC_DISARM_EPS_KWH = 0.5` — wider SoC margin for disarm, so the probe's own
  overshoot-dip doesn't disarm it.
- `PROBE_FORECAST_MARGIN_KW = 0.5` — forecast must exceed load by this to arm.
- `PROBE_DISCHARGE_CEILING_W = 300` — step down above this battery discharge
  (primary overshoot signal).
- `PROBE_IMPORT_CEILING_W = 500` — step down above this measured grid import
  (secondary guard).
- `PROBE_UP_INTERVAL_CYCLES = 3` — minimum cycles between speculative up-steps
  (~15 min at the default 300 s cadence).

Cadence is the existing planner cycle (`update_seconds`, default 300 s); no new
loop. This was accepted as fast enough.

## Data flow

Sunny, battery full, sell price below floor, car connected, no LP charge →
`_apply_ev` arms the probe → commands Manual + min current → inverter unclips PV
to feed it, grid stays ≈ 0 → next cycles step the current up (lazily) until
grid import would lift, then hold one amp below → EV soaks the curtailed solar.
Cloud cuts PV → the full battery starts discharging to cover the EV → the
discharge reading lifts above the ceiling → immediate down-step(s), before the
battery meaningfully drains. Sell price rises above the floor → export
re-enabled → disarm → write "Auto" → EVCS resumes following the now-visible
export.

## Out of scope

- Charging from **sellable** (exportable) surplus — left to the EVCS Auto mode
  / the LP, per the asymmetric-split decision above.
- Any LP/optimizer change. The probe is a reactive control layer only; the LP's
  modeling of targeted EV charging is unchanged.
- Deriving battery flow from the PV/load/grid balance — we require an explicit
  `battery_power_entity` instead.
- Deriving the deadband from `kw_per_amp` automatically (possible later;
  defaults are fixed for now).
- Reacting faster than the existing cycle cadence.

## Testing

- **Pure logic (`tests/test_ev_controller.py`):** `should_probe_surplus` arm
  matrix (each condition independently gates, incl. battery-power required), and
  `decide_surplus_probe` behavior — ramp-up while battery idle and grid ≈ 0,
  immediate down on battery-discharge overshoot, immediate down on grid-import
  overshoot, rest inside the deadband (no oscillation across cycles), below-min
  → 0, up-probe rate limiting, forecast-headroom gating.
- **Planner wiring (`tests/test_planner.py`):** with `FakeReader`/`FakeCaller`,
  assert the probe arms in the curtailment corner and writes Manual + a non-zero
  current + start on; does **not** arm when `p_sell > 0` (export enabled),
  battery not full, car disconnected, `battery_power_entity` missing, or a
  reading is bad; steps down when battery discharge is reported; and disarms
  back to "Auto" when export re-enables. The planned-start gate test gains a
  curtailment variant.
- Existing planner/optimizer tests remain the coverage for LP-driven charging
  and the export-enabled handback (EVCS-follows-export) path.
