"""Unit tests for the built-in median-over-N-days load forecaster."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.pv_optimizer.load_forecaster import (
    HistoryReader,
    LoadForecaster,
    LoadForecasterConfig,
    _bucket_average_kw,
)


# Reference "now" — Friday 2026-05-01 12:00 UTC (naive). Weekday() == 4.
NOW = datetime(2026, 5, 1, 12, 0, 0)


class FakeHistory:
    """Step-wise history with an optional carry-forward initial value."""

    def __init__(self, samples: list[tuple[datetime, float]]) -> None:
        self._samples = sorted(samples, key=lambda s: s[0])

    def get_history(self, entity_id: str, start: datetime, end: datetime
                    ) -> list[tuple[datetime, float]]:
        # Mimic recorder behavior: include last sample at-or-before ``start``
        # so the consumer can carry-forward into the window.
        carry: list[tuple[datetime, float]] = []
        in_win: list[tuple[datetime, float]] = []
        for ts, v in self._samples:
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


def _constant_history(value_kw: float, days: int = 10,
                      step_minutes: int = 15) -> FakeHistory:
    return FakeHistory(_constant_stream(value_kw, days, step_minutes))


class _MultiHistory:
    """Per-entity step-wise history with carry-forward.

    Dispatches by entity_id to per-entity FakeHistory instances so a
    single reader can return distinct streams for the load and EV power
    entities. Carry-forward semantics are inherited from FakeHistory.
    """

    def __init__(self, by_entity: dict[str, list[tuple[datetime, float]]]) -> None:
        self._by_entity = {k: FakeHistory(v) for k, v in by_entity.items()}

    def get_history(self, entity_id: str, start: datetime, end: datetime
                    ) -> list[tuple[datetime, float]]:
        h = self._by_entity.get(entity_id)
        return h.get_history(entity_id, start, end) if h is not None else []


# ---------------------------------------------------------------------------
# _bucket_average_kw
# ---------------------------------------------------------------------------


def test_bucket_average_constant_value() -> None:
    h = _constant_history(0.5).get_history("e", NOW - timedelta(days=1), NOW)
    avg = _bucket_average_kw(h, NOW - timedelta(hours=2), NOW - timedelta(hours=1))
    assert avg == pytest.approx(0.5)


def test_bucket_average_returns_none_when_no_data() -> None:
    avg = _bucket_average_kw([], NOW - timedelta(hours=1), NOW)
    assert avg is None


def test_bucket_average_carry_forward_only() -> None:
    # Single sample 3h before window, no samples inside → carry-forward fills.
    samples = [(NOW - timedelta(hours=3), 0.8)]
    avg = _bucket_average_kw(samples, NOW - timedelta(hours=1), NOW)
    assert avg == pytest.approx(0.8)


def test_bucket_average_step_change_in_middle() -> None:
    # 0.0 kW for first half of the hour, then steps to 2.0 kW for the second half.
    start = NOW - timedelta(hours=1)
    mid = start + timedelta(minutes=30)
    samples = [(start - timedelta(minutes=10), 0.0), (mid, 2.0)]
    avg = _bucket_average_kw(samples, start, NOW)
    assert avg == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# LoadForecaster
# ---------------------------------------------------------------------------


def _slots(n: int = 4) -> list[datetime]:
    return [NOW + timedelta(hours=i) for i in range(n)]


def test_constant_load_yields_constant_forecast() -> None:
    fc = LoadForecaster(LoadForecasterConfig(entity_id="e"), _constant_history(0.5))
    out = fc.forecast(_slots(4))
    for s in _slots(4):
        assert out.kw_per_slot[s] == pytest.approx(0.5)
        assert out.days_used_per_slot[s] == 7


def test_last_forecast_is_none_until_first_call_then_caches() -> None:
    fc = LoadForecaster(LoadForecasterConfig(entity_id="e"), _constant_history(0.5))
    assert fc.last_forecast is None
    out = fc.forecast(_slots(2))
    assert fc.last_forecast is out
    # A second call replaces the cached value.
    out2 = fc.forecast(_slots(3))
    assert fc.last_forecast is out2
    assert fc.last_forecast is not out


def test_single_day_spike_is_rejected_by_median() -> None:
    # 7 days of 0.5 kW background; on day -3, hour 12..13 has a 7 kW EV spike.
    base = _constant_history(0.5, days=10, step_minutes=15)
    spike_start = NOW - timedelta(days=3)  # same hour-of-day as slot 0.
    spike_end = spike_start + timedelta(hours=1)
    spiked = []
    for ts, v in base._samples:
        if spike_start <= ts < spike_end:
            spiked.append((ts, 7.0))
        else:
            spiked.append((ts, v))
    fc = LoadForecaster(LoadForecasterConfig(entity_id="e"), FakeHistory(spiked))
    out = fc.forecast([NOW])
    # Median of [0.5,0.5,7.0,0.5,0.5,0.5,0.5] == 0.5; spike doesn't move it.
    assert out.kw_per_slot[NOW] == pytest.approx(0.5)


def test_partial_history_uses_what_is_available() -> None:
    # Only 3 days of history available.
    fc = LoadForecaster(
        LoadForecasterConfig(entity_id="e", lookback_days=7),
        _constant_history(0.4, days=3, step_minutes=30),
    )
    out = fc.forecast([NOW])
    assert out.kw_per_slot[NOW] == pytest.approx(0.4)
    assert out.days_used_per_slot[NOW] == 3


def test_no_history_returns_zero_with_zero_days_used() -> None:
    fc = LoadForecaster(LoadForecasterConfig(entity_id="e"), FakeHistory([]))
    out = fc.forecast(_slots(2))
    for s in _slots(2):
        assert out.kw_per_slot[s] == 0.0
        assert out.days_used_per_slot[s] == 0


def test_cap_clips_high_median() -> None:
    fc = LoadForecaster(
        LoadForecasterConfig(entity_id="e", cap_kw=2.0), _constant_history(5.0),
    )
    out = fc.forecast([NOW])
    assert out.kw_per_slot[NOW] == pytest.approx(2.0)


def test_weekday_aware_uses_only_matching_weekdays() -> None:
    # Background 0.5 kW; on every prior Friday (same weekday as NOW), hour
    # 12..13 is elevated to 1.5 kW. weekday_aware must pick *only* those
    # Fridays — yielding median 1.5; without it the six non-Fridays would
    # drag the median back to ~0.5.
    base = _constant_history(0.5, days=20, step_minutes=15)
    elevated_hours = {NOW - timedelta(days=d) for d in (7, 14)}
    modified = [
        (ts, 1.5) if any(h <= ts < h + timedelta(hours=1) for h in elevated_hours)
        else (ts, v)
        for ts, v in base._samples
    ]
    fc = LoadForecaster(
        LoadForecasterConfig(entity_id="e", lookback_days=14, weekday_aware=True),
        FakeHistory(modified),
    )
    out = fc.forecast([NOW])
    assert out.kw_per_slot[NOW] == pytest.approx(1.5)
    assert out.days_used_per_slot[NOW] == 2  # NOW-7d and NOW-14d are both Fri


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
