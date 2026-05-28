"""EV target inputs (kWh and %)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
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
    cap = coord.config.ev.params.car_battery_kwh
    async_add_entities([
        _TargetKwh(entry.entry_id, cap),
        _TargetPct(entry.entry_id, cap),
    ])


class _TargetKwh(RestoreEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, entry_id: str, cap: float) -> None:
        self._attr_unique_id = f"{entry_id}_ev_target_kwh"
        self._attr_name = "PV LP Optimizer EV Target kWh"
        self._attr_native_max_value = float(cap)
        self._value = 0.0

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = max(0.0, float(value))
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "", "unknown", "unavailable"):
            try:
                self._value = float(last.state)
            except ValueError:
                pass


class _TargetPct(RestoreEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "%"

    def __init__(self, entry_id: str, cap: float) -> None:
        self._attr_unique_id = f"{entry_id}_ev_target_pct"
        self._attr_name = "PV LP Optimizer EV Target %"
        self._cap = float(cap)
        self._value = 0.0

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = max(0.0, min(100.0, float(value)))
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "", "unknown", "unavailable"):
            try:
                self._value = float(last.state)
            except ValueError:
                pass
