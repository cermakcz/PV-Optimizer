"""The pv_optimizer integration.

The package is intentionally importable without ``homeassistant`` being
installed (only :mod:`models`, :mod:`optimizer`, :mod:`planner` and
:mod:`const` are imported at module load). HA-side wiring (``async_setup_entry``,
coordinator, config flow, sensors) is loaded lazily so unit tests can run
in a plain virtualenv.
"""
from __future__ import annotations

from .const import DOMAIN, PLATFORMS

__all__ = ["DOMAIN", "PLATFORMS", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass, entry):  # type: ignore[no-untyped-def]
    """Set up pv_optimizer from a config entry (HA path)."""
    # Imports deferred so this module is import-safe without homeassistant.
    from .coordinator import PvOptimizerCoordinator
    from .models import BatteryParams
    from .planner import PlannerConfig
    from . import const as C

    data = {**entry.data, **entry.options}
    capacity = float(data[C.CONF_BATTERY_CAPACITY_KWH])
    soc_min_kwh = capacity * float(data[C.CONF_BATTERY_SOC_MIN_PCT]) / 100.0
    soc_max_kwh = capacity * float(data[C.CONF_BATTERY_SOC_MAX_PCT]) / 100.0
    battery = BatteryParams(
        capacity_kwh=capacity,
        soc_min_kwh=soc_min_kwh,
        soc_max_kwh=soc_max_kwh,
        p_chg_max_kw=float(data[C.CONF_BATTERY_P_CHG_MAX_KW]),
        p_dis_max_kw=float(data[C.CONF_BATTERY_P_DIS_MAX_KW]),
        eta_chg=float(data[C.CONF_BATTERY_ETA_CHG]),
        eta_dis=float(data[C.CONF_BATTERY_ETA_DIS]),
        cycle_cost_eur_per_kwh=float(data[C.CONF_BATTERY_CYCLE_COST]),
    )
    config = PlannerConfig(
        load_power_entity=data[C.CONF_LOAD_POWER],
        pv_power_entity=data[C.CONF_PV_POWER],
        grid_power_entity=data[C.CONF_GRID_POWER],
        battery_soc_entity=data[C.CONF_BATTERY_SOC],
        buy_price_today_entity=data[C.CONF_BUY_PRICE_TODAY],
        sell_price_today_entity=data[C.CONF_SELL_PRICE_TODAY],
        pv_forecast_entity=data[C.CONF_PV_FORECAST],
        buy_price_tomorrow_entity=data.get(C.CONF_BUY_PRICE_TOMORROW),
        sell_price_tomorrow_entity=data.get(C.CONF_SELL_PRICE_TOMORROW),
        load_forecast_entity=data.get(C.CONF_LOAD_FORECAST),
        feedin_override_entity=data.get(C.CONF_FEEDIN_OVERRIDE),
        grid_setpoint_entity=data[C.CONF_GRID_SETPOINT],
        feedin_switch_entity=data[C.CONF_FEEDIN_SWITCH],
        battery=battery,
        p_grid_imp_max_kw=float(data[C.CONF_GRID_IMPORT_MAX_KW]),
        p_grid_exp_max_kw=float(data[C.CONF_GRID_EXPORT_MAX_KW]),
        slot_minutes=int(data[C.CONF_SLOT_MINUTES]),
        horizon_hours=int(data[C.CONF_HORIZON_HOURS]),
        setpoint_tolerance_w=float(data[C.CONF_SETPOINT_TOLERANCE_W]),
    )
    coord = PvOptimizerCoordinator(hass, config, int(data[C.CONF_UPDATE_SECONDS]))
    await coord.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):  # type: ignore[no-untyped-def]
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
