"""Switch entities for the EV charging feature."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([_EVCarAutoReturnSwitch(entry.entry_id)])


class _EVCarAutoReturnSwitch(RestoreEntity, SwitchEntity):
    """Opt-in: when ON, 'car' mode auto-returns to 'auto' on session-done."""

    _attr_translation_key = "ev_car_auto_return"

    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{entry_id}_ev_car_auto_return"
        self._attr_name = "PV LP Optimizer EV Car Auto-Return"
        # Pin the entity_id so the planner's hard-coded read
        # (switch.pv_optimizer_ev_car_auto_return) lines up regardless
        # of the slug HA would derive from the friendly name.
        self.entity_id = "switch.pv_optimizer_ev_car_auto_return"
        self._state = False

    @property
    def is_on(self) -> bool:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._state = False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state == "on":
            self._state = True
