"""Built-in load forecaster: median-over-N-days at the same hour-of-day.

Pure layer (no Home Assistant dependency). The HA coordinator supplies a
``HistoryReader`` implementation backed by the ``recorder`` component;
unit tests use a synthetic reader.

The median naturally rejects one-off spikes (e.g. an EV charging session
on a single day) without needing to know what a "spike" looks like.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

_LOGGER = logging.getLogger(__name__)

# Sample granularity below which two adjacent samples are treated as
# coincident (avoids zero-duration weights from duplicate timestamps).
_MIN_DT_S = 1.0


class HistoryReader(Protocol):
    """Returns (timestamp, value_kw) samples in ``[start, end)``, sorted ascending.

    Implementations should also include the most recent sample at-or-before
    ``start`` so the forecaster can carry-forward the initial value into the
    first bucket. Naive UTC timestamps are expected.
    """

    def get_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]: ...


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


@dataclass(frozen=True)
class LoadForecast:
    """Forecast result keyed by slot-start (naive UTC)."""

    kw_per_slot: dict[datetime, float]
    days_used_per_slot: dict[datetime, int] = field(default_factory=dict)
    ev_subtracted: bool = False


class LoadForecaster:
    def __init__(self, config: LoadForecasterConfig, reader: HistoryReader) -> None:
        self.config = config
        self.reader = reader
        # Cached output of the most recent forecast() call. Diagnostic sensors
        # consume this directly so the recorder is never queried twice per
        # planning cycle.
        self.last_forecast: LoadForecast | None = None

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


def _bucket_average_kw(
    samples: list[tuple[datetime, float]], start: datetime, end: datetime,
) -> float | None:
    """Time-weighted average of step-wise samples over ``[start, end)``.

    Returns ``None`` when no sample is in effect during the bucket (i.e. no
    sample at-or-before ``end`` start exists), so callers can distinguish
    "no data" from "genuinely zero load".
    """
    if not samples or end <= start:
        return None
    # Find carry-forward sample (last sample at or before `start`).
    carry: float | None = None
    in_window: list[tuple[datetime, float]] = []
    for ts, v in samples:
        if ts <= start:
            carry = v
        elif ts < end:
            in_window.append((ts, v))
        else:
            break
    if carry is None and not in_window:
        return None
    cursor = start
    cur_v = carry if carry is not None else in_window[0][1]
    weighted_sum = 0.0
    total_dt = 0.0
    for ts, v in in_window:
        dt = (ts - cursor).total_seconds()
        if dt >= _MIN_DT_S:
            weighted_sum += cur_v * dt
            total_dt += dt
        cursor = ts
        cur_v = v
    dt = (end - cursor).total_seconds()
    if dt >= _MIN_DT_S:
        weighted_sum += cur_v * dt
        total_dt += dt
    if total_dt <= 0:
        return None
    return weighted_sum / total_dt
