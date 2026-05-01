"""Home Assistant coordinator: thin shim over :mod:`planner`.

This module imports ``homeassistant`` and is therefore *not* covered by unit
tests in this repository. All testable logic lives in :mod:`planner` and is
exercised through ``tests/test_planner.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .load_forecaster import HistoryReader, LoadForecaster, LoadForecasterConfig
from .planner import PlanCycle, Planner, PlannerConfig, ServiceCaller, StateReader, StateView


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
        self._hass.async_create_task(
            self._hass.services.async_call(domain, service, data, blocking=False)
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
        states = state_changes_during_period(
            self._hass, start_aware, end_aware,
            entity_id=entity_id, include_start_time_state=True,
        ).get(entity_id, [])
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
        return out


class PvOptimizerCoordinator(DataUpdateCoordinator[PlanCycle]):
    """Periodic coordinator that runs one planner step per update."""

    def __init__(self, hass: HomeAssistant, config: PlannerConfig,
                 update_seconds: int,
                 forecaster_opts: LoadForecasterOptions | None = None) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="pv_optimizer",
            update_interval=timedelta(seconds=update_seconds),
        )
        self.forecaster: LoadForecaster | None = None
        # Built-in forecaster only kicks in when the user did not point to
        # an external load_forecast_entity (escape hatch contract).
        if not config.load_forecast_entity and config.load_power_entity:
            opts = forecaster_opts or LoadForecasterOptions()
            self.forecaster = LoadForecaster(
                LoadForecasterConfig(
                    entity_id=config.load_power_entity,
                    lookback_days=opts.lookback_days,
                    cap_kw=opts.cap_kw,
                    weekday_aware=opts.weekday_aware,
                    slot_minutes=config.slot_minutes,
                ),
                _HassHistoryReader(hass),
            )
        self._planner = Planner(
            config, _HassStateReader(hass), _HassServiceCaller(hass),
            load_forecaster=self.forecaster,
        )

    async def _async_update_data(self) -> PlanCycle:
        # The LP solve is CPU-bound and synchronous; run in executor to avoid
        # blocking the event loop. The history reader (also sync) is invoked
        # transitively from planner.step in the same executor thread.
        return await self.hass.async_add_executor_job(self._planner.step, datetime.utcnow())

    @callback
    def get_last_cycle(self) -> PlanCycle | None:
        return self._planner.last
