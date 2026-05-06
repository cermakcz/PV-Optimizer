"""Home Assistant coordinator: thin shim over :mod:`planner`.

This module imports ``homeassistant`` and is therefore *not* covered by unit
tests in this repository. All testable logic lives in :mod:`planner` and is
exercised through ``tests/test_planner.py``.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .load_forecaster import HistoryReader, LoadForecaster, LoadForecasterConfig
from .planner import (
    LiveAverager,
    PlanCycle,
    Planner,
    PlannerConfig,
    ServiceCaller,
    StateReader,
    StateView,
)


@dataclass(frozen=True)
class LoadForecasterOptions:
    """Coordinator-level toggles for the built-in forecaster.

    Kept separate from PlannerConfig so the planner stays oblivious to the
    fact that a forecaster exists at all.
    """

    lookback_days: int = 7
    cap_kw: float | None = None
    weekday_aware: bool = False

_LOGGER = logging.getLogger(__name__)


class _HassStateReader(StateReader):
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def get(self, entity_id: str) -> StateView | None:
        st = self._hass.states.get(entity_id)
        if st is None:
            return None
        return StateView(state=st.state, attributes=dict(st.attributes))


class _HassServiceCaller(ServiceCaller):
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        # Planner.step runs in an executor thread, so we must schedule the
        # service call back onto the event loop in a thread-safe way.
        # ``hass.async_create_task`` is event-loop-only and raises in
        # HA 2025.x when invoked from a worker thread.
        asyncio.run_coroutine_threadsafe(
            self._hass.services.async_call(domain, service, data, blocking=False),
            self._hass.loop,
        )


class _HassHistoryReader(HistoryReader):
    """Recorder-backed history reader.

    Returns ``(naive-UTC ts, kW)`` tuples. Assumes the entity reports power
    in **W** (matches the planner's ``load_power_entity`` contract).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def get_history(self, entity_id: str, start: datetime, end: datetime
                    ) -> list[tuple[datetime, float]]:
        # state_changes_during_period requires aware datetimes (UTC).
        start_aware = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
        end_aware = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
        # Timing breadcrumb: recorder queries dominate planner cycle time
        # when the source entity has many state changes; surface that at
        # DEBUG so users can correlate slow cycles with history volume.
        t0 = _time.perf_counter()
        states = state_changes_during_period(
            self._hass, start_aware, end_aware,
            entity_id=entity_id, include_start_time_state=True,
        ).get(entity_id, [])
        t1 = _time.perf_counter()
        out: list[tuple[datetime, float]] = []
        for st in states:
            try:
                v_kw = float(st.state) / 1000.0
            except (TypeError, ValueError):
                continue
            ts = st.last_changed
            if ts.tzinfo is not None:
                ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
            out.append((ts, v_kw))
        _LOGGER.debug(
            "recorder.get_history %s window=%s -> %d raw / %d parsed in %.2fs",
            entity_id, end_aware - start_aware, len(states), len(out), t1 - t0,
        )
        return out


class _HassStatsHistoryReader(HistoryReader):
    """Statistics-backed history reader for the load forecaster.

    Reads the recorder's pre-aggregated mean buckets (5-minute or hourly,
    depending on the planner's slot size) instead of every raw state
    change. For a power sensor sampled every few seconds this is two to
    three orders of magnitude fewer rows: 8 days of state changes can be
    100k+ rows; the same window in 5-minute stats is ~2.3k, in hourly
    stats ~190.

    Each stats bucket is emitted as a synthetic step-wise sample at the
    bucket start with the bucket's mean as the value. ``_bucket_average_kw``
    treats step-wise samples as constant-until-next, so for contiguous
    uniform buckets the per-slot time-weighted mean equals the simple mean
    of the bucket means within that slot — which is exactly what we want.

    Falls back transparently to per-state-change reads when the entity
    has no statistics yet (e.g. missing ``state_class: measurement``, or
    the recorder hasn't compiled its first batch). Logs a one-shot
    warning per entity so the user knows why a cycle is slow.
    """

    def __init__(self, hass: HomeAssistant, period: str) -> None:
        # ``period`` is one of ``"hour"`` / ``"5minute"`` and is picked at
        # construction time from the planner's slot size: hourly stats are
        # exactly aligned with hourly slots (one stat = one slot, the
        # bucket mean is the slot mean), and 5-minute stats divide every
        # planner-supported sub-hour slot evenly.
        self._hass = hass
        self._period = period
        # Fallback path used when no statistics exist for an entity. Keeps
        # the planner functional on entities lacking ``state_class``,
        # at the cost of the original slow read.
        self._fallback = _HassHistoryReader(hass)
        self._warned_no_stats: set[str] = set()

    def get_history(self, entity_id: str, start: datetime, end: datetime
                    ) -> list[tuple[datetime, float]]:
        start_aware = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
        end_aware = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
        t0 = _time.perf_counter()
        rows = statistics_during_period(
            self._hass, start_aware, end_aware,
            statistic_ids={entity_id}, period=self._period,
            units=None, types={"mean"},
        ).get(entity_id, [])
        t1 = _time.perf_counter()
        if not rows:
            if entity_id not in self._warned_no_stats:
                _LOGGER.warning(
                    "no %s statistics for %s; falling back to per-state-change "
                    "reads (much slower). Ensure the sensor exposes "
                    "`state_class: measurement` and that the recorder has "
                    "had time to compile its first stats batch.",
                    self._period, entity_id,
                )
                self._warned_no_stats.add(entity_id)
            return self._fallback.get_history(entity_id, start, end)
        out: list[tuple[datetime, float]] = []
        for row in rows:
            mean = row.get("mean")
            if mean is None:
                continue
            ts_field = row.get("start")
            # Recent HA serialises bucket boundaries as unix floats; older
            # versions returned ``datetime`` objects. Handle both.
            if isinstance(ts_field, (int, float)):
                ts = datetime.fromtimestamp(float(ts_field), tz=timezone.utc).replace(tzinfo=None)
            elif isinstance(ts_field, datetime):
                ts = (ts_field.astimezone(timezone.utc).replace(tzinfo=None)
                      if ts_field.tzinfo is not None else ts_field)
            else:
                continue
            out.append((ts, float(mean) / 1000.0))
        _LOGGER.debug(
            "stats.get_history %s period=%s window=%s -> %d rows / %d parsed in %.2fs",
            entity_id, self._period, end_aware - start_aware,
            len(rows), len(out), t1 - t0,
        )
        return out


class _HassLiveAverager(LiveAverager):
    """Recorder-backed time-weighted average for the planner's slot-0 PV
    refinement. Reuses :class:`_HassHistoryReader`'s W→kW contract."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._reader = _HassHistoryReader(hass)

    def average_kw(self, entity_id: str, since: datetime, until: datetime
                   ) -> float | None:
        samples = self._reader.get_history(entity_id, since, until)
        if not samples:
            return None
        # Time-weighted mean: each sample holds until the next, last sample
        # holds until ``until``. ``include_start_time_state=True`` upstream
        # guarantees the first sample is at-or-before ``since`` so the
        # whole window is covered without extrapolation.
        total_kwh = 0.0
        total_h = 0.0
        for i, (ts, kw) in enumerate(samples):
            seg_start = max(ts, since)
            seg_end = samples[i + 1][0] if i + 1 < len(samples) else until
            if seg_end <= seg_start:
                continue
            dt_h = (seg_end - seg_start).total_seconds() / 3600.0
            total_kwh += kw * dt_h
            total_h += dt_h
        return total_kwh / total_h if total_h > 0 else None


class PvOptimizerCoordinator(DataUpdateCoordinator[PlanCycle]):
    """Periodic coordinator that runs one planner step per update."""

    def __init__(self, hass: HomeAssistant, config: PlannerConfig,
                 update_seconds: int,
                 forecaster_opts: LoadForecasterOptions | None = None,
                 currency: str = "EUR") -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="pv_optimizer",
            update_interval=timedelta(seconds=update_seconds),
        )
        # Display-only currency label; surfaced via cost/savings sensor units.
        self.currency = currency
        # Mirror the coordinator's update cadence into the planner so the
        # slot-0 PV trailing average uses the same window length.
        config = replace(config, update_seconds=update_seconds)
        # Exposed so HA-side entities (e.g. the plan sensor) can surface
        # battery / horizon parameters as attributes for frontend charts
        # without reaching into the planner's privates.
        self.config = config
        self.forecaster: LoadForecaster | None = None
        # Built-in forecaster only kicks in when the user did not point to
        # an external load_forecast_entity (escape hatch contract).
        if not config.load_forecast_entity and config.load_power_entity:
            opts = forecaster_opts or LoadForecasterOptions()
            # Pick the coarsest stats period that still divides the slot:
            # hourly when slots are >= 60 min (one stat per slot, exact),
            # 5-minute otherwise. 5-minute stats are typically retained
            # for ~``recorder.purge_keep_days`` (default 10), comfortably
            # covering the default 7-day lookback.
            stats_period = "hour" if config.slot_minutes >= 60 else "5minute"
            self.forecaster = LoadForecaster(
                LoadForecasterConfig(
                    entity_id=config.load_power_entity,
                    lookback_days=opts.lookback_days,
                    cap_kw=opts.cap_kw,
                    weekday_aware=opts.weekday_aware,
                    slot_minutes=config.slot_minutes,
                ),
                _HassStatsHistoryReader(hass, stats_period),
            )
        self._planner = Planner(
            config, _HassStateReader(hass), _HassServiceCaller(hass),
            load_forecaster=self.forecaster,
            live_averager=_HassLiveAverager(hass),
        )

    async def _async_update_data(self) -> PlanCycle:
        # The LP solve is CPU-bound and synchronous; run in executor to avoid
        # blocking the event loop. The history reader (also sync) is invoked
        # transitively from planner.step in the same executor thread.
        return await self.hass.async_add_executor_job(self._planner.step, datetime.utcnow())

    @callback
    def get_last_cycle(self) -> PlanCycle | None:
        return self._planner.last
