"""Home Assistant coordinator: thin shim over :mod:`planner`.

This module imports ``homeassistant`` and is therefore *not* covered by unit
tests in this repository. All testable logic lives in :mod:`planner` and is
exercised through ``tests/test_planner.py``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .planner import PlanCycle, Planner, PlannerConfig, ServiceCaller, StateReader, StateView

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


class PvOptimizerCoordinator(DataUpdateCoordinator[PlanCycle]):
    """Periodic coordinator that runs one planner step per update."""

    def __init__(self, hass: HomeAssistant, config: PlannerConfig,
                 update_seconds: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="pv_optimizer",
            update_interval=timedelta(seconds=update_seconds),
        )
        self._planner = Planner(config, _HassStateReader(hass), _HassServiceCaller(hass))

    async def _async_update_data(self) -> PlanCycle:
        # The LP solve is CPU-bound and synchronous; run in executor to avoid
        # blocking the event loop.
        return await self.hass.async_add_executor_job(self._planner.step, datetime.utcnow())

    @callback
    def get_last_cycle(self) -> PlanCycle | None:
        return self._planner.last
