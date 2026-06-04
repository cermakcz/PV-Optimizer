# EV-corrected load forecast — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Subtract historical EV charging draw from the built-in load forecaster's per-bucket median so the LP doesn't double-count EV consumption (its own `p_ev_chg` variable in `auto` mode plus inflated `load[t]`).

**Architecture:** `LoadForecaster.forecast()` gains a second `get_history` call for the EV power entity, subtracts the per-bucket EV average from the per-bucket load average (linearity of expectation makes this equivalent to per-sample subtraction), and clamps at 0. The capability is wired via a new `LoadForecasterConfig.ev_power_entity_id` field; the per-tick gate is a new `subtract_ev` kwarg on `forecast()`, set by the planner to `(self._read_mode() == "auto")`. A new `LoadForecast.ev_subtracted` field surfaces what actually happened on the diagnostic sensor.

**Spec:** `docs/superpowers/specs/2026-06-04-ev-load-forecast-correction-design.md`.

**Tech Stack:** Python 3, dataclasses, PuLP, pytest. Home Assistant on the HA-side files (not tested in this repo).

---

## File Structure

- `custom_components/pv_optimizer/load_forecaster.py` — extend `LoadForecasterConfig` with `ev_power_entity_id`; extend `LoadForecast` with `ev_subtracted`; extend `LoadForecaster.forecast()` with `subtract_ev` kwarg and the subtraction algorithm. Pure Python, no HA.
- `tests/test_load_forecaster.py` — add a `_MultiHistory` helper and tests for the new behavior.
- `custom_components/pv_optimizer/coordinator.py:259-268` — pass `ev_power_entity_id` to `LoadForecasterConfig` when an EV config is present.
- `custom_components/pv_optimizer/planner.py:659-678` (`_read_load_forecast`) — pass `subtract_ev = (self._read_mode() == "auto") if cfg.ev else False` to the forecaster.
- `tests/test_planner.py` — one new test asserting the mode-based gating reaches the forecaster.
- `custom_components/pv_optimizer/sensor.py:178-189` (`_LoadForecastSensor.extra_state_attributes`) — add `ev_subtracted` attribute.
- `PRD.md` §4.2 and §9 — append EV-correction sub-paragraph and new sensor attribute.

---

## Task 1: Add `ev_power_entity_id` to `LoadForecasterConfig` and `ev_subtracted` to `LoadForecast`

Shape-only change — no behavior, no new tests. Existing tests must still pass.

**Files:**
- Modify: `custom_components/pv_optimizer/load_forecaster.py:38-53` (`LoadForecasterConfig`)
- Modify: `custom_components/pv_optimizer/load_forecaster.py:55-60` (`LoadForecast`)

- [ ] **Step 1: Add `ev_power_entity_id` field to `LoadForecasterConfig`**

Replace `LoadForecasterConfig` with:

```python
@dataclass(frozen=True)
class LoadForecasterConfig:
    entity_id: str
    lookback_days: int = 7
    cap_kw: float | None = None
    weekday_aware: bool = False
    slot_minutes: int = 60
    ev_power_entity_id: str | None = None

    def __post_init__(self) -> None:
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be > 0")
        if self.cap_kw is not None and self.cap_kw <= 0:
            raise ValueError("cap_kw must be > 0 when set")
        if self.slot_minutes <= 0 or 1440 % self.slot_minutes != 0:
            raise ValueError("slot_minutes must divide 1440")
```

- [ ] **Step 2: Add `ev_subtracted` field to `LoadForecast`**

Replace `LoadForecast` with:

```python
@dataclass(frozen=True)
class LoadForecast:
    """Forecast result keyed by slot-start (naive UTC)."""

    kw_per_slot: dict[datetime, float]
    days_used_per_slot: dict[datetime, int] = field(default_factory=dict)
    ev_subtracted: bool = False
```

- [ ] **Step 3: Run all existing tests; verify regression no-op**

Run: `pytest tests/test_load_forecaster.py tests/test_planner.py -v`
Expected: ALL PASS.

- [ ] **Step 4: Commit**

```bash
git add custom_components/pv_optimizer/load_forecaster.py
git commit -m "$(cat <<'EOF'
feat(forecaster): add ev_power_entity_id config + ev_subtracted result fields

Skeleton for EV-corrected load forecast (spec
docs/superpowers/specs/2026-06-04-ev-load-forecast-correction-design.md).
No behavior change yet — both fields default to neutral values.
EOF
)"
```

---

## Task 2: Happy-path EV subtraction (constant histories)

Add a multi-entity history helper, write a failing test that demands per-bucket subtraction, then implement.

**Files:**
- Modify: `tests/test_load_forecaster.py` — add `_MultiHistory` helper + test
- Modify: `custom_components/pv_optimizer/load_forecaster.py:72-114` (`LoadForecaster.forecast`)

- [ ] **Step 1: Add `_MultiHistory` helper to the test file**

Add at the top of `tests/test_load_forecaster.py`, after the existing `FakeHistory` class and `_constant_history` helper:

```python
class _MultiHistory:
    """Per-entity step-wise history with carry-forward.

    Mirrors FakeHistory's contract but dispatches by entity_id so a single
    reader can return distinct streams for the load and EV power entities.
    """

    def __init__(self, by_entity: dict[str, list[tuple[datetime, float]]]) -> None:
        self._by_entity = {
            k: sorted(v, key=lambda s: s[0]) for k, v in by_entity.items()
        }

    def get_history(self, entity_id: str, start: datetime, end: datetime
                    ) -> list[tuple[datetime, float]]:
        samples = self._by_entity.get(entity_id, [])
        carry: list[tuple[datetime, float]] = []
        in_win: list[tuple[datetime, float]] = []
        for ts, v in samples:
            if ts <= start:
                carry = [(ts, v)]
            elif ts < end:
                in_win.append((ts, v))
        return carry + in_win


def _constant_stream(value_kw: float, days: int = 10,
                     step_minutes: int = 15) -> list[tuple[datetime, float]]:
    """Build a constant-value sample stream covering [NOW - days, NOW + 1d)."""
    base = NOW - timedelta(days=days)
    cursor = base
    end = NOW + timedelta(hours=24)
    samples = []
    while cursor < end:
        samples.append((cursor, value_kw))
        cursor += timedelta(minutes=step_minutes)
    return samples
```

- [ ] **Step 2: Write the failing test — constant load minus constant EV**

Append to `tests/test_load_forecaster.py`:

```python
# ---------------------------------------------------------------------------
# EV-corrected load forecast
# ---------------------------------------------------------------------------


def test_ev_subtraction_full_history() -> None:
    """Constant 8 kW load with constant 3 kW EV draw → 5 kW corrected median."""
    reader = _MultiHistory({
        "sensor.load_w": _constant_stream(8.0),
        "sensor.ev_w": _constant_stream(3.0),
    })
    fc = LoadForecaster(
        LoadForecasterConfig(
            entity_id="sensor.load_w",
            ev_power_entity_id="sensor.ev_w",
        ),
        reader,
    )
    out = fc.forecast(_slots(4))
    for s in _slots(4):
        assert out.kw_per_slot[s] == pytest.approx(5.0)
        assert out.days_used_per_slot[s] == 7
    assert out.ev_subtracted is True
```

- [ ] **Step 3: Run test; verify it fails**

Run: `pytest tests/test_load_forecaster.py::test_ev_subtraction_full_history -v`
Expected: FAIL — the forecast comes out as 8.0 kW (no subtraction yet) and `ev_subtracted` is `False`.

- [ ] **Step 4: Implement EV subtraction in `forecast()`**

Replace the body of `LoadForecaster.forecast` with:

```python
    def forecast(
        self,
        slot_starts: list[datetime],
        *,
        subtract_ev: bool = True,
    ) -> LoadForecast:
        """Build a forecast for the given slot starts (naive UTC, ascending).

        Returns 0.0 (with ``days_used = 0``) for any slot with no usable
        history; the planner can still proceed with a degraded forecast.

        When ``subtract_ev=True`` and ``config.ev_power_entity_id`` is set,
        the per-bucket EV average is subtracted from the per-bucket load
        average before taking the median, with a bucket-level clamp at 0.
        """
        if not slot_starts:
            return LoadForecast(kw_per_slot={}, days_used_per_slot={})
        cfg = self.config
        slot_h = cfg.slot_minutes / 60.0

        earliest_lookback = min(slot_starts) - timedelta(days=cfg.lookback_days)
        latest = max(slot_starts) + timedelta(minutes=cfg.slot_minutes)
        samples = self.reader.get_history(cfg.entity_id, earliest_lookback, latest)

        ev_active = subtract_ev and cfg.ev_power_entity_id is not None
        ev_samples: list[tuple[datetime, float]] = []
        if ev_active:
            ev_samples = self.reader.get_history(
                cfg.ev_power_entity_id, earliest_lookback, latest)

        kw_out: dict[datetime, float] = {}
        used_out: dict[datetime, int] = {}
        for slot_start in slot_starts:
            day_avgs: list[float] = []
            for d in range(1, cfg.lookback_days + 1):
                hist_start = slot_start - timedelta(days=d)
                if cfg.weekday_aware and hist_start.weekday() != slot_start.weekday():
                    continue
                hist_end = hist_start + timedelta(minutes=cfg.slot_minutes)
                load_avg = _bucket_average_kw(samples, hist_start, hist_end)
                if load_avg is None:
                    continue
                if ev_active:
                    ev_avg = _bucket_average_kw(ev_samples, hist_start, hist_end) or 0.0
                    day_avgs.append(max(0.0, load_avg - ev_avg))
                else:
                    day_avgs.append(load_avg)
            if day_avgs:
                v = statistics.median(day_avgs)
                if cfg.cap_kw is not None:
                    v = min(v, cfg.cap_kw)
                kw_out[slot_start] = max(0.0, v)
                used_out[slot_start] = len(day_avgs)
            else:
                kw_out[slot_start] = 0.0
                used_out[slot_start] = 0
        _LOGGER.debug(
            "load forecast: %d slots, lookback=%d, weekday_aware=%s, slot_h=%s, ev_subtracted=%s",
            len(slot_starts), cfg.lookback_days, cfg.weekday_aware, slot_h, ev_active,
        )
        result = LoadForecast(
            kw_per_slot=kw_out,
            days_used_per_slot=used_out,
            ev_subtracted=ev_active,
        )
        self.last_forecast = result
        return result
```

- [ ] **Step 5: Run test; verify PASS**

Run: `pytest tests/test_load_forecaster.py::test_ev_subtraction_full_history -v`
Expected: PASS.

- [ ] **Step 6: Run the rest of the load-forecaster suite for regressions**

Run: `pytest tests/test_load_forecaster.py -v`
Expected: ALL PASS (existing tests unchanged because `ev_power_entity_id` defaults to `None`).

- [ ] **Step 7: Commit**

```bash
git add custom_components/pv_optimizer/load_forecaster.py tests/test_load_forecaster.py
git commit -m "$(cat <<'EOF'
feat(forecaster): subtract EV history from per-bucket load average

Per-day subtract-then-median to avoid LP double-count of EV draw in auto
mode (see spec
docs/superpowers/specs/2026-06-04-ev-load-forecast-correction-design.md).
Capability gated on ev_power_entity_id; per-call subtract_ev kwarg lets
the planner opt out in car/off mode (wired in a later task).
EOF
)"
```

---

## Task 3: Bucket-level clamp at 0

The previous task already implements `max(0, load_avg - ev_avg)`. This task adds the explicit test that exercises it.

**Files:**
- Modify: `tests/test_load_forecaster.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_load_forecaster.py`:

```python
def test_ev_subtraction_clamps_at_zero() -> None:
    """EV draw > measured load → bucket-level clamp keeps median ≥ 0."""
    reader = _MultiHistory({
        "sensor.load_w": _constant_stream(2.0),
        "sensor.ev_w": _constant_stream(3.0),
    })
    fc = LoadForecaster(
        LoadForecasterConfig(
            entity_id="sensor.load_w",
            ev_power_entity_id="sensor.ev_w",
        ),
        reader,
    )
    out = fc.forecast(_slots(2))
    for s in _slots(2):
        assert out.kw_per_slot[s] == pytest.approx(0.0)
```

- [ ] **Step 2: Run; verify PASS**

Run: `pytest tests/test_load_forecaster.py::test_ev_subtraction_clamps_at_zero -v`
Expected: PASS (clamp is already in place from Task 2).

- [ ] **Step 3: Commit**

```bash
git add tests/test_load_forecaster.py
git commit -m "test(forecaster): EV subtraction clamps at zero on overshoot"
```

---

## Task 4: Partial EV history (`None` on some lookback days)

Driven by a failing test that mixes EV-present and EV-absent days.

**Files:**
- Modify: `tests/test_load_forecaster.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_load_forecaster.py`:

```python
def test_ev_subtraction_partial_history() -> None:
    """EV history exists for some lookback days, not others.

    Target hour: 12:00. Load is constant 8 kW for all 7 lookback days.
    EV draws 4 kW only at hour 12 on 4 of the 7 days (the most recent 4),
    nothing on the earlier 3.

    Per-day corrected contributions: [4, 4, 4, 4, 8, 8, 8] → median 4.0 kW.
    Without partial-history handling the test would crash (None - 4) or
    drop the EV-less days entirely.
    """
    load_stream = _constant_stream(8.0)

    # Build EV stream covering only the four most recent lookback days at
    # hour 12 (one sample at hour 12, then zero immediately after to end
    # the step). Empty entries on day 5/6/7 → no EV samples in those
    # buckets → ev_avg becomes None → treated as 0 → load passes through.
    ev_samples: list[tuple[datetime, float]] = []
    for d in (1, 2, 3, 4):
        h12 = (NOW - timedelta(days=d)).replace(hour=12, minute=0, second=0)
        ev_samples.append((h12, 4.0))
        ev_samples.append((h12 + timedelta(hours=1), 0.0))

    reader = _MultiHistory({
        "sensor.load_w": load_stream,
        "sensor.ev_w": ev_samples,
    })
    fc = LoadForecaster(
        LoadForecasterConfig(
            entity_id="sensor.load_w",
            ev_power_entity_id="sensor.ev_w",
        ),
        reader,
    )
    # Forecast the 12:00 slot specifically.
    target = NOW.replace(hour=12, minute=0, second=0)
    out = fc.forecast([target])
    assert out.kw_per_slot[target] == pytest.approx(4.0)
    assert out.days_used_per_slot[target] == 7
```

- [ ] **Step 2: Run; expect PASS (the `or 0.0` in Task 2 already handles `None`)**

Run: `pytest tests/test_load_forecaster.py::test_ev_subtraction_partial_history -v`
Expected: PASS.

Note: if this test fails, double-check Task 2 step 4 — the `ev_avg = _bucket_average_kw(...) or 0.0` line is what makes partial history work. The test confirms the contract.

- [ ] **Step 3: Commit**

```bash
git add tests/test_load_forecaster.py
git commit -m "test(forecaster): EV subtraction handles partial-history days"
```

---

## Task 5: Per-call gate — `subtract_ev=False`

Verify that the capability can be wired but the per-call flag suppresses the subtraction.

**Files:**
- Modify: `tests/test_load_forecaster.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_load_forecaster.py`:

```python
def test_ev_subtraction_disabled_per_call() -> None:
    """Capability wired but subtract_ev=False → no subtraction; raw load."""
    reader = _MultiHistory({
        "sensor.load_w": _constant_stream(8.0),
        "sensor.ev_w": _constant_stream(3.0),
    })
    fc = LoadForecaster(
        LoadForecasterConfig(
            entity_id="sensor.load_w",
            ev_power_entity_id="sensor.ev_w",
        ),
        reader,
    )
    out = fc.forecast(_slots(2), subtract_ev=False)
    for s in _slots(2):
        assert out.kw_per_slot[s] == pytest.approx(8.0)
    assert out.ev_subtracted is False
```

- [ ] **Step 2: Run; expect PASS (Task 2 already gates on `subtract_ev` AND `ev_power_entity_id`)**

Run: `pytest tests/test_load_forecaster.py::test_ev_subtraction_disabled_per_call -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_load_forecaster.py
git commit -m "test(forecaster): subtract_ev=False disables EV correction per-call"
```

---

## Task 6: Sample-cadence misalignment

Confirms the two `get_history` calls don't need timestamp-for-timestamp alignment because bucket averages are computed independently.

**Files:**
- Modify: `tests/test_load_forecaster.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_load_forecaster.py`:

```python
def test_ev_subtraction_independent_cadences() -> None:
    """Load samples every 30 s vs EV samples every 5 min — both still average
    correctly per bucket because subtraction happens on bucket averages, not
    on aligned per-sample pairs.
    """
    load_stream = _constant_stream(8.0, step_minutes=0.5)
    ev_stream = _constant_stream(3.0, step_minutes=5)
    reader = _MultiHistory({
        "sensor.load_w": load_stream,
        "sensor.ev_w": ev_stream,
    })
    fc = LoadForecaster(
        LoadForecasterConfig(
            entity_id="sensor.load_w",
            ev_power_entity_id="sensor.ev_w",
        ),
        reader,
    )
    out = fc.forecast(_slots(2))
    for s in _slots(2):
        assert out.kw_per_slot[s] == pytest.approx(5.0)
```

Note: `_constant_stream` uses `timedelta(minutes=step_minutes)`. `step_minutes=0.5` works because `timedelta` accepts floats.

- [ ] **Step 2: Run; expect PASS**

Run: `pytest tests/test_load_forecaster.py::test_ev_subtraction_independent_cadences -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_load_forecaster.py
git commit -m "test(forecaster): EV subtraction tolerates cadence mismatch"
```

---

## Task 7: Wire `ev_power_entity_id` in the coordinator

The capability is enabled whenever an `EVConfig` exists at coordinator construction time. No HA-side unit test — coordinator changes are smoke-tested in a live HA instance per PRD §10.

**Files:**
- Modify: `custom_components/pv_optimizer/coordinator.py:259-268`

- [ ] **Step 1: Pass `ev_power_entity_id` to the forecaster config**

Replace the `LoadForecaster` construction block (around line 259-268) with:

```python
            self.forecaster = LoadForecaster(
                LoadForecasterConfig(
                    entity_id=config.load_power_entity,
                    lookback_days=opts.lookback_days,
                    cap_kw=opts.cap_kw,
                    weekday_aware=opts.weekday_aware,
                    slot_minutes=config.slot_minutes,
                    ev_power_entity_id=(
                        config.ev.charging_power_entity
                        if config.ev is not None
                        else None
                    ),
                ),
                _HassStatsHistoryReader(hass, stats_period),
            )
```

- [ ] **Step 2: Run the full test suite to confirm no regressions**

Run: `pytest -q`
Expected: ALL PASS.

- [ ] **Step 3: Commit**

```bash
git add custom_components/pv_optimizer/coordinator.py
git commit -m "feat(ev): wire ev_power_entity_id from EVConfig into the load forecaster"
```

---

## Task 8: Planner reads `mode` and passes `subtract_ev` to the forecaster

TDD: write a planner test asserting the mode-based gating reaches the forecaster, then add the one-line wiring.

**Files:**
- Modify: `tests/test_planner.py`
- Modify: `custom_components/pv_optimizer/planner.py:659-678` (`_read_load_forecast`)

- [ ] **Step 1: Inspect the existing planner test scaffolding for mode entity setup**

Read `tests/test_planner.py` around line 1076 (the `select.pv_optimizer_ev_mode` setup pattern) to confirm how to seed the mode state and bind `EVConfig`. The pattern is: build an `EVConfig` with `mode_entity="select.pv_optimizer_ev_mode"`, attach it to the `PlannerConfig`, and add a `StateView` for that entity in `states`.

- [ ] **Step 2: Write a failing planner test**

Append to `tests/test_planner.py` (after the existing built-in-forecaster tests, near line 645). Mirror the surrounding test style — reuse the helpers `_states`, `_config`, `FakeReader`, `FakeCaller`, `_FakeHistory`, `StateView`. The point of the test is to assert the forecaster sees the right `subtract_ev` value depending on mode:

```python
def test_planner_forecaster_subtract_ev_gated_on_auto_mode() -> None:
    """Planner reads EV mode; subtract_ev=True only in auto, False in car/off."""

    class _SpyForecaster:
        """Stand-in for LoadForecaster that records the subtract_ev kwarg."""

        def __init__(self) -> None:
            self.calls: list[bool] = []
            self.last_forecast = None

        def forecast(self, slot_starts, *, subtract_ev: bool = True):
            self.calls.append(subtract_ev)
            from custom_components.pv_optimizer.load_forecaster import LoadForecast
            kw = {s: 1.0 for s in slot_starts}
            used = {s: 7 for s in slot_starts}
            self.last_forecast = LoadForecast(
                kw_per_slot=kw, days_used_per_slot=used,
                ev_subtracted=subtract_ev,
            )
            return self.last_forecast

    # Build a planner config with an EV binding so cfg.ev is not None.
    # Use the same EV-config shape the existing car/off tests use.
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(),
        charger_state_entity="sensor.evcs_state",
        charging_power_entity="sensor.ev_power_w",
        max_current_entity="number.ev_current",
        mode_entity="select.pv_optimizer_ev_mode",
    )
    base_states = _states(buy=[0.10] * 24, sell=[0.05] * 24, load_w=1000.0)
    base_states["sensor.evcs_state"] = StateView(state="disconnected")
    base_states["sensor.ev_power_w"] = StateView(state="0")

    for mode, expected in (("auto", True), ("car", False), ("off", False)):
        states = dict(base_states)
        states["select.pv_optimizer_ev_mode"] = StateView(state=mode)
        spy = _SpyForecaster()
        planner = Planner(
            _config(ev=ev_cfg),
            FakeReader(states),
            FakeCaller(),
            load_forecaster=spy,
        )
        planner.step(NOW)
        assert spy.calls, f"forecaster not called in mode={mode}"
        assert spy.calls[-1] is expected, (
            f"mode={mode} expected subtract_ev={expected}, got {spy.calls[-1]}"
        )
```

Notes for the test author:
- If `_config()` doesn't already accept an `ev=` kwarg, extend the helper (or just construct a `PlannerConfig` directly the way the surrounding tests do).
- The spy returns `1.0 kW` per slot for every slot regardless of mode — the assertions only inspect what was passed *in*, not what came out.
- `_FakeHistory` from existing tests is NOT used here because we're replacing the forecaster object entirely.

- [ ] **Step 3: Run; verify FAIL**

Run: `pytest tests/test_planner.py::test_planner_forecaster_subtract_ev_gated_on_auto_mode -v`
Expected: FAIL — `spy.calls[-1]` is `True` for every mode because the planner currently doesn't pass `subtract_ev`.

- [ ] **Step 4: Wire `subtract_ev` in `_read_load_forecast`**

Edit `custom_components/pv_optimizer/planner.py:659-678`. Replace the `_read_load_forecast` method with:

```python
    def _read_load_forecast(self, entity_id: str | None, slot_starts: list[datetime],
                            slot_h: float, fallback_kw: float) -> list[float]:
        # External-entity escape hatch takes precedence over the built-in
        # forecaster, so users can plug in their own forecast without code
        # changes.
        if entity_id:
            st = self.reader.get(entity_id)
            if st is not None:
                forecast = st.attributes.get("forecast")
                if isinstance(forecast, list) and forecast:
                    mapping = {_parse_iso(p["datetime"]): float(p.get("power_kw", p.get("power", 0)))
                               for p in forecast if "datetime" in p}
                    return [_lookup_forecast(mapping, s, slot_h, default=fallback_kw)
                            for s in slot_starts]
            return [max(0.0, fallback_kw)] * len(slot_starts)
        if self.load_forecaster is not None:
            # Subtract EV history from the load forecast only in auto mode,
            # where the LP separately models p_ev_chg. In car/off the planner
            # ignores/suppresses LP-side EV writes, so historical EV is
            # legitimate opaque household load that the LP must plan around.
            subtract_ev = (
                self.config.ev is not None and self._read_mode() == "auto"
            )
            fc = self.load_forecaster.forecast(slot_starts, subtract_ev=subtract_ev)
            return [fc.kw_per_slot.get(s, max(0.0, fallback_kw)) if fc.days_used_per_slot.get(s, 0) > 0
                    else max(0.0, fallback_kw) for s in slot_starts]
        return [max(0.0, fallback_kw)] * len(slot_starts)
```

- [ ] **Step 5: Run the new test; verify PASS**

Run: `pytest tests/test_planner.py::test_planner_forecaster_subtract_ev_gated_on_auto_mode -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite for regressions**

Run: `pytest -q`
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "$(cat <<'EOF'
feat(ev): planner gates EV-history subtraction on auto mode

car/off modes treat the EV as opaque load (the LP ignores/suppresses its
own EV writes), so the historical EV draw belongs in the load forecast.
auto mode tells the forecaster to subtract because the LP separately
budgets p_ev_chg.
EOF
)"
```

---

## Task 9: Expose `ev_subtracted` on the diagnostic sensor

**Files:**
- Modify: `custom_components/pv_optimizer/sensor.py:178-189` (`_LoadForecastSensor.extra_state_attributes`)

- [ ] **Step 1: Add the attribute**

Edit `custom_components/pv_optimizer/sensor.py:178-189`. Replace the existing `extra_state_attributes` body of `_LoadForecastSensor` with:

```python
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fc = self.coordinator.forecaster
        if fc is None or fc.last_forecast is None:
            return {}
        kw = fc.last_forecast.kw_per_slot
        used = fc.last_forecast.days_used_per_slot
        return {
            "lookback_days": fc.config.lookback_days,
            "cap_kw": fc.config.cap_kw,
            "weekday_aware": fc.config.weekday_aware,
            "kw_per_slot": {naive_utc_to_iso(k): round(v, 3) for k, v in kw.items()},
            "days_used_per_slot": {naive_utc_to_iso(k): used.get(k, 0) for k in kw},
            "ev_subtracted": fc.last_forecast.ev_subtracted,
        }
```

(The exact closing-brace context follows the existing return-dict — if the file has additional keys after `days_used_per_slot` that this excerpt didn't show, preserve them and just add the `ev_subtracted` line before the closing `}`.)

- [ ] **Step 2: Run the suite to confirm no syntax regressions**

Run: `pytest -q`
Expected: ALL PASS.

The sensor module isn't unit-tested in this repo (per PRD §10) — verification is via live HA dev-tools after deploy.

- [ ] **Step 3: Commit**

```bash
git add custom_components/pv_optimizer/sensor.py
git commit -m "feat(sensor): expose ev_subtracted on the load-forecast sensor"
```

---

## Task 10: PRD update

**Files:**
- Modify: `PRD.md` §4.2 (line range around 94-127) and §9 (line range around 391-393).

- [ ] **Step 1: Amend §4.2**

Find the existing §4.2 paragraph that ends with:

> If no usable history exists (fresh install, or no samples at the target hour for any of the lookback days), the planner falls back to the current load-power reading for that slot — same behavior as if no forecaster were present at all.

Append immediately after it:

```markdown

**EV correction.** When an EV is configured (see EV-charging spec), the
forecaster also reads the EV charging-power entity's history and subtracts
the per-bucket EV average from the per-bucket load average before taking
the median (bucket-level clamp at 0). The subtraction is gated on the
*current* EV mode being `auto` — the only mode in which the LP also
budgets `p_ev_chg` separately. In `car`/`off` the EV is treated as
opaque household load and historical draws are included.
```

- [ ] **Step 2: Amend §9 (sensor attributes list for `sensor.pv_optimizer_load_forecast`)**

Find the bullet that introduces `sensor.pv_optimizer_load_forecast` (around line 391):

> `sensor.pv_optimizer_load_forecast` (next-slot kW; full per-slot series in attributes). Only published when the built-in forecaster is active.

Append a clarifying sub-bullet under it (matching the indentation style of the surrounding sensors):

```markdown
- `sensor.pv_optimizer_load_forecast` (next-slot kW; full per-slot series in
  attributes). Only published when the built-in forecaster is active.
  Attributes include `ev_subtracted: bool` indicating whether the most
  recent cycle had the EV-history correction (§4.2) applied.
```

- [ ] **Step 3: Commit**

```bash
git add PRD.md
git commit -m "docs(ev): amend PRD for EV-corrected load forecast"
```

---

## Done — verify end-to-end

- [ ] **Run the full suite one more time**

Run: `pytest -q`
Expected: ALL PASS.

- [ ] **Manual smoke check (live HA, optional but recommended)**

After deploying to the Home Assistant instance:
1. Verify `sensor.pv_optimizer_load_forecast` shows `ev_subtracted: true` in attributes when the mode select is `auto`.
2. Switch the mode select to `car` or `off`; on the next planner tick, the same sensor should now report `ev_subtracted: false`.
3. Compare the load-forecast median to recent days' totals — should now exclude the EV draw on hours when you typically charged.
