"""Mode select for the EV charging feature."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coord = hass.data[DOMAIN][entry.entry_id]
    if coord.config.ev is None:
        return
    async_add_entities([_EVModeSelect(entry.entry_id)])


class _EVModeSelect(RestoreEntity, SelectEntity):
    _attr_options = ["auto", "manual", "off"]
    _attr_translation_key = "ev_mode"

    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{entry_id}_ev_mode"
        self._attr_name = "PV LP Optimizer EV Mode"
        # Pin the entity_id so the planner's hard-coded reads
        # (select.pv_optimizer_ev_mode) line up regardless of the
        # slug HA would derive from the friendly name.
        self.entity_id = "select.pv_optimizer_ev_mode"
        self._state = "auto"

    @property
    def current_option(self) -> str:
        return self._state

    async def async_select_option(self, option: str) -> None:
        self._state = option
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._state = last.state
