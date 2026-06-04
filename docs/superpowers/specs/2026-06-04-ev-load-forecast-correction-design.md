# EV-corrected load forecast

Status: approved 2026-06-04.

## 1. Problem

The built-in load forecaster (`load_forecaster.py`, PRD §4.2) takes a
per-slot median over the last N days of `load_power_entity`. That
entity reads total household AC load — including the EV charger draw
whenever the car is plugged in. The LP optimizer separately models EV
charging as its own decision variable `p_ev_chg[t]` in the power
balance (`optimizer.py:97-116`), so any EV draw that bleeds into the
load forecast is **double-counted**: the LP budgets grid imports /
battery discharge for the EV via `p_ev_chg` *and* via inflated
`load[t]`.

The median naturally rejects a single outlier day — but when the user
charges on ≥ 4 of the last 7 days at the same hour-of-day, the median
sits squarely in the inflated band and the bias survives.

## 2. Algorithm

Inside `LoadForecaster.forecast()`, when an EV power entity is
configured *and* the caller asks for it (per-call flag, §3.2), fetch a
second history series for the EV charging-power entity over the same
`[earliest_lookback, latest)` window. For each lookback day's
hour-bucket, compute the time-weighted average of both series
independently via the existing `_bucket_average_kw`, then derive that
day's corrected contribution:

```
load_avg = _bucket_average_kw(load_samples, hist_start, hist_end)
ev_avg   = _bucket_average_kw(ev_samples,   hist_start, hist_end)  # 0 if None
corrected = max(0.0, load_avg - ev_avg)
```

Linearity of expectation means `avg(load - ev) == avg(load) - avg(ev)`
exactly, so subtracting the two bucket averages is equivalent to
sample-level subtraction without needing a merged-stream pass. The
`max(0, ...)` clamp at bucket level handles meter noise / sensor lag
where EV power briefly registers higher than the totalizer.

The median is then taken over the corrected per-day values, same as
today.

### Why this works where median-of-median doesn't

Median-of-median (`median(load) − median(ev)`) is biased when ≥ 4 of 7
same-hour days have EV draw. Example at hour 18 with loads
`[10, 8, 5, 9, 9, 7, 8]` and matching EVs `[2, 4, 0, 4, 4, 2, 2]`:

- Truth (`load - ev` per day): `[8, 4, 5, 5, 5, 5, 6]` → median `5`.
- Median-of-median: `median(load) - median(ev) = 8 - 2 = 6` (biased
  +20 %, same direction as the original bug).
- Per-day-subtract-then-median: `5` (exact).

## 3. Wiring

### 3.1 Capability — `LoadForecasterConfig`

Add a new optional field:

```python
@dataclass(frozen=True)
class LoadForecasterConfig:
    entity_id: str
    lookback_days: int = 7
    cap_kw: float | None = None
    weekday_aware: bool = False
    slot_minutes: int = 60
    ev_power_entity_id: str | None = None   # NEW
```

Default `None` keeps every existing call-site (and unit test)
unchanged.

The coordinator (`coordinator.py:259`) populates
`ev_power_entity_id=ev.charging_power_entity` when an `EVConfig` is
present at construction time, else leaves it `None`.

### 3.2 Per-call gate — `LoadForecaster.forecast(...)`

```python
def forecast(
    self,
    slot_starts: list[datetime],
    *,
    subtract_ev: bool = True,
) -> LoadForecast:
    ...
```

Subtraction happens iff `subtract_ev=True` **and**
`config.ev_power_entity_id is not None`. Default `True` so callers
that don't care (none today, but future ones) get the corrected
behavior automatically.

`planner._read_load_forecast` decides per tick:

```python
subtract_ev = (self._read_mode() == "auto") if self.config.ev else False
fc = self.load_forecaster.forecast(slot_starts, subtract_ev=subtract_ev)
```

The mode is already read elsewhere in the planner (`_apply_ev`, line
699). The new read costs one extra state lookup per tick.

### 3.3 Mode-gate rationale

| Current mode | LP `p_ev_chg`?                             | Subtract historical EV? |
|--------------|--------------------------------------------|-------------------------|
| `auto`       | Yes when target + deadline + connected     | **Yes**                 |
| `car`        | Maybe added by LP but writes overridden    | **No**                  |
| `off`        | Maybe added by LP but writes suppressed    | **No**                  |

In `auto`, any future EV draw is either LP-planned (`p_ev_chg`) or
reactive PV-surplus (free, shouldn't inflate the budget) — so the LP
should see a load forecast that excludes historical EV.

In `car` and `off`, the planner ignores or suppresses the LP's EV
writes; the EV is effectively opaque household load that the LP must
plan around. Historical EV draw is a legitimate input.

Reading the *current* mode (not the mode in effect when each
historical sample was recorded) is intentional: the question is "how
will the LP treat EV on *this* run", not "what was the mode
yesterday". The forecast may differ between consecutive ticks if the
user toggles the mode; that's the intended consequence.

## 4. Edge cases

- **EV history missing for a lookback day** (entity didn't exist,
  recorder purged) → `_bucket_average_kw` returns `None` for the EV
  bucket; treat as `0` so the load value passes through uncorrected.
  The day still contributes to the median.
- **Load history missing for a lookback day** → unchanged from today:
  that day's contribution is skipped.
- **EV draw > measured load** (meter noise, sensor lag) → bucket-level
  `max(0, ...)` clamp.
- **Car not plugged in during the lookback window** → EV history is
  all zeros, `corrected ≡ load`, output matches today's behavior
  exactly.

## 5. Diagnostics

`sensor.pv_optimizer_load_forecast` gains one new attribute:

- `ev_subtracted: bool` — `true` iff both the capability was wired
  (`ev_power_entity_id is not None`) *and* the last call used
  `subtract_ev=True`. Lets the user verify from dev-tools that the
  current forecast is EV-corrected.

No new sensor for "EV history median" — the correction is per-day, and
exposing it as a published series would invite the user to read it as
something it isn't.

## 6. Configuration

No config-flow changes. The behavior is tied to the existing EV
binding: if an EV is configured, the capability is on; the per-tick
gate is determined by the existing mode entity.

Users on a separate EV circuit (EV not in `load_power_entity`'s
reading) can fall back to the existing planner-level escape hatch — set
`load_forecast_entity` to a user-built template sensor and the
built-in forecaster is bypassed entirely.

## 7. Tests

In `tests/test_load_forecaster.py`:

1. **Capability off** — `ev_power_entity_id=None` → output identical
   to today (regression no-op).
2. **Per-call flag off** — capability wired but `subtract_ev=False` →
   no subtraction.
3. **Full EV history, subtraction on** — 7 days each with load=8 kW,
   ev=3 kW at the target hour → corrected median = 5 kW.
4. **Partial EV history** — EV samples exist for only 4 of 7 lookback
   days at the target hour; load present for all 7. The 3 EV-less
   days pass through uncorrected; the 4 EV-present days subtract.
   Median is taken over all 7 corrected values.
5. **Bucket-level clamp** — load=2 kW, ev=3 kW at the target hour →
   corrected value = 0, not negative.
6. **Sample-cadence misalignment** — load samples every 30 s, EV
   samples every 5 min → bucket averages computed independently;
   subtraction still works without timestamp-for-timestamp alignment.

In `tests/test_planner.py`:

7. **Mode gate** — planner test that asserts
   `forecaster.forecast(..., subtract_ev=True)` in auto mode and
   `subtract_ev=False` in car/off mode. Exercises the
   `planner._read_load_forecast` plumbing.

## 8. PRD update

PRD §4.2 gains a sub-paragraph (placed after the existing
"Configuration knobs" table):

> **EV correction.** When an EV is configured (PRD §EV-charging spec),
> the forecaster also reads the EV charging-power entity's history and
> subtracts the per-bucket EV average from the per-bucket load average
> before taking the median. The subtraction is gated on the *current*
> EV mode being `auto`; in `car`/`off` the EV is treated as opaque
> household load.

§6 (Configuration) is not amended — no new knobs.
§9 (Diagnostic Sensors) gains `ev_subtracted: bool` on the
`sensor.pv_optimizer_load_forecast` attributes list.

## 9. Out of scope

- Surfacing a separate "EV history median" diagnostic series.
- A config-flow opt-out (already covered by the existing
  `load_forecast_entity` escape hatch).
- Retroactive mode tracking (subtracting based on per-sample
  historical mode rather than the current mode) — current-mode is the
  right question to ask given how the LP is parameterized.
