"""UI configuration flow for pv_optimizer.

Imports ``homeassistant`` and is therefore not covered by unit tests in this
repository. Validation logic that does not depend on HA lives in
:mod:`planner` (parameter validation happens via ``BatteryParams`` /
``OptimizerInputs`` post-init checks).
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from . import const as C

_DOMAIN = C.DOMAIN


def _sensor(domain: str = "sensor") -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domain))


def _num(min_value: float, max_value: float, step: float = 0.01,
         unit: str | None = None) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(
        min=min_value, max=max_value, step=step,
        mode=NumberSelectorMode.BOX, unit_of_measurement=unit,
    ))


_ENTITIES_SCHEMA = vol.Schema({
    vol.Required(C.CONF_LOAD_POWER): _sensor(),
    vol.Required(C.CONF_PV_POWER): _sensor(),
    vol.Required(C.CONF_GRID_POWER): _sensor(),
    vol.Required(C.CONF_BATTERY_SOC): _sensor(),
    vol.Required(C.CONF_BUY_PRICE_TODAY): _sensor(),
    vol.Required(C.CONF_SELL_PRICE_TODAY): _sensor(),
    vol.Required(C.CONF_PV_FORECAST): _sensor(),
    vol.Optional(C.CONF_BUY_PRICE_TOMORROW): _sensor(),
    vol.Optional(C.CONF_SELL_PRICE_TOMORROW): _sensor(),
    vol.Optional(C.CONF_LOAD_FORECAST): _sensor(),
    vol.Optional(C.CONF_FEEDIN_OVERRIDE): EntitySelector(
        EntitySelectorConfig(domain=["switch", "input_boolean", "binary_sensor"])
    ),
    vol.Required(C.CONF_GRID_SETPOINT): EntitySelector(
        EntitySelectorConfig(domain=["number", "input_number"])
    ),
    vol.Required(C.CONF_FEEDIN_SWITCH): EntitySelector(
        EntitySelectorConfig(domain=["switch", "input_boolean"])
    ),
})


_BATTERY_SCHEMA = vol.Schema({
    vol.Required(C.CONF_BATTERY_CAPACITY_KWH, default=10.0): _num(0.1, 1000.0, 0.1, "kWh"),
    vol.Required(C.CONF_BATTERY_SOC_MIN_PCT, default=C.DEFAULT_SOC_MIN_PCT): _num(0.0, 100.0, 1.0, "%"),
    vol.Required(C.CONF_BATTERY_SOC_MAX_PCT, default=C.DEFAULT_SOC_MAX_PCT): _num(0.0, 100.0, 1.0, "%"),
    vol.Required(C.CONF_BATTERY_P_CHG_MAX_KW, default=5.0): _num(0.0, 100.0, 0.1, "kW"),
    vol.Required(C.CONF_BATTERY_P_DIS_MAX_KW, default=5.0): _num(0.0, 100.0, 0.1, "kW"),
    vol.Required(C.CONF_BATTERY_ETA_CHG, default=C.DEFAULT_ETA_CHG): _num(0.5, 1.0, 0.01),
    vol.Required(C.CONF_BATTERY_ETA_DIS, default=C.DEFAULT_ETA_DIS): _num(0.5, 1.0, 0.01),
    vol.Required(C.CONF_BATTERY_CYCLE_COST, default=C.DEFAULT_CYCLE_COST): _num(0.0, 1.0, 0.001, "EUR/kWh"),
})


_SOLVER_SCHEMA = vol.Schema({
    vol.Required(C.CONF_SLOT_MINUTES, default=C.DEFAULT_SLOT_MINUTES): _num(5, 240, 5, "min"),
    vol.Required(C.CONF_HORIZON_HOURS, default=C.DEFAULT_HORIZON_HOURS): _num(1, 48, 1, "h"),
    vol.Required(C.CONF_UPDATE_SECONDS, default=C.DEFAULT_UPDATE_SECONDS): _num(30, 3600, 30, "s"),
    vol.Required(C.CONF_GRID_IMPORT_MAX_KW, default=25.0): _num(0.1, 200.0, 0.5, "kW"),
    vol.Required(C.CONF_GRID_EXPORT_MAX_KW, default=25.0): _num(0.0, 200.0, 0.5, "kW"),
    vol.Required(C.CONF_SETPOINT_TOLERANCE_W, default=C.DEFAULT_SETPOINT_TOLERANCE_W): _num(0.0, 5000.0, 10.0, "W"),
})


class PvOptimizerConfigFlow(config_entries.ConfigFlow, domain=_DOMAIN):
    """Multi-step setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()
        return self.async_show_form(step_id="user", data_schema=_ENTITIES_SCHEMA)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solver()
        return self.async_show_form(step_id="battery", data_schema=_BATTERY_SCHEMA)

    async def async_step_solver(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="PV Optimizer", data=self._data)
        return self.async_show_form(step_id="solver", data_schema=_SOLVER_SCHEMA)

    @staticmethod
    def async_get_options_flow(config_entry):
        return PvOptimizerOptionsFlow(config_entry)


class PvOptimizerOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        # For brevity, options flow exposes the solver schema only; entity/battery
        # changes require re-adding the integration.
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=_SOLVER_SCHEMA)
