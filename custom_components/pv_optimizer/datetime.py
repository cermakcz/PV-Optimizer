"""EV deadline datetime."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import BAD_STATES, DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coord = hass.data[DOMAIN][entry.entry_id]
    if coord.config.ev is None:
        return
    async_add_entities([_DeadlineDateTime(entry.entry_id)])


class _DeadlineDateTime(RestoreEntity, DateTimeEntity):
    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{entry_id}_ev_deadline"
        self._attr_name = "PV LP Optimizer EV Deadline"
        # Pin entity_id to match what the planner reads.
        self.entity_id = "datetime.pv_optimizer_ev_deadline"
        self._value: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        return self._value

    async def async_set_value(self, value: datetime) -> None:
        self._value = value
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in BAD_STATES:
            try:
                self._value = datetime.fromisoformat(last.state)
            except ValueError:
                self._value = None
