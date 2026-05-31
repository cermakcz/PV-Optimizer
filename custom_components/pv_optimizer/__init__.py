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
    from .coordinator import LoadForecasterOptions, PvOptimizerCoordinator
    from .models import BatteryParams, EVParams
    from .planner import EVConfig, PlannerConfig
    from . import const as C

    data = {**entry.data, **entry.options}
    capacity = float(data[C.CONF_BATTERY_CAPACITY_KWH])
    soc_min_kwh = capacity * float(data[C.CONF_BATTERY_SOC_MIN_PCT]) / 100.0
    soc_max_kwh = capacity * float(data[C.CONF_BATTERY_SOC_MAX_PCT]) / 100.0
    soc_health_kwh = capacity * float(
        data.get(C.CONF_BATTERY_SOC_HEALTH_PCT, C.DEFAULT_SOC_HEALTH_PCT)
    ) / 100.0
    battery = BatteryParams(
        capacity_kwh=capacity,
        soc_min_kwh=soc_min_kwh,
        soc_max_kwh=soc_max_kwh,
        p_chg_max_kw=float(data[C.CONF_BATTERY_P_CHG_MAX_KW]),
        p_dis_max_kw=float(data[C.CONF_BATTERY_P_DIS_MAX_KW]),
        eta_chg=float(data[C.CONF_BATTERY_ETA_CHG]),
        eta_dis=float(data[C.CONF_BATTERY_ETA_DIS]),
        cycle_cost_per_kwh=float(data[C.CONF_BATTERY_CYCLE_COST]),
        soc_health_kwh=soc_health_kwh,
        low_soc_penalty_per_kwh_h=float(
            data.get(C.CONF_BATTERY_LOW_SOC_PENALTY, C.DEFAULT_LOW_SOC_PENALTY)
        ),
    )
    ev_state_entity = data.get(C.CONF_EV_CHARGER_STATE)
    ev_power_entity = data.get(C.CONF_EV_CHARGING_POWER)
    ev_current_entity = data.get(C.CONF_EV_MAX_CURRENT)
    ev_max_kw = data.get(C.CONF_EV_MAX_CHARGING_POWER_KW)
    ev_max_a = data.get(C.CONF_EV_MAX_CHARGING_CURRENT_A)
    ev_car_kwh = data.get(C.CONF_EV_CAR_BATTERY_KWH)
    ev_cfg: EVConfig | None = None
    if (ev_state_entity and ev_power_entity and ev_current_entity
            and ev_max_kw is not None
            and ev_max_a is not None
            and ev_car_kwh is not None):
        ev_params = EVParams(
            max_charging_power_kw=float(ev_max_kw),
            max_charging_current_a=float(ev_max_a),
            min_charging_current_a=float(data.get(
                C.CONF_EV_MIN_CHARGING_CURRENT_A,
                C.DEFAULT_EV_MIN_CHARGING_CURRENT_A)),
            car_battery_kwh=float(ev_car_kwh),
            current_tolerance_a=float(data.get(
                C.CONF_EV_CURRENT_TOLERANCE_A,
                C.DEFAULT_EV_CURRENT_TOLERANCE_A)),
            session_done_power_w=float(data.get(
                C.CONF_EV_SESSION_DONE_POWER_W,
                C.DEFAULT_EV_SESSION_DONE_POWER_W)),
            session_done_seconds=float(data.get(
                C.CONF_EV_SESSION_DONE_SECONDS,
                C.DEFAULT_EV_SESSION_DONE_SECONDS)),
            buy_price_threshold=float(data.get(
                C.CONF_EV_BUY_PRICE_THRESHOLD,
                C.DEFAULT_EV_BUY_PRICE_THRESHOLD)),
        )
        ev_cfg = EVConfig(
            params=ev_params,
            charger_state_entity=ev_state_entity,
            charging_power_entity=ev_power_entity,
            max_current_entity=ev_current_entity,
            session_energy_entity=data.get(C.CONF_EV_SESSION_ENERGY),
            start_switch_entity=data.get(C.CONF_EV_START_SWITCH),
            charger_mode_entity=data.get(C.CONF_EV_CHARGER_MODE),
            charger_mode_option_active=str(data.get(
                C.CONF_EV_CHARGER_MODE_OPTION_ACTIVE,
                C.DEFAULT_EV_CHARGER_MODE_OPTION_ACTIVE)),
            charger_mode_option_passive=str(data.get(
                C.CONF_EV_CHARGER_MODE_OPTION_PASSIVE,
                C.DEFAULT_EV_CHARGER_MODE_OPTION_PASSIVE)),
            mode_entity="select.pv_optimizer_ev_mode",
            target_kwh_entity="number.pv_optimizer_ev_target_kwh",
            target_pct_entity="number.pv_optimizer_ev_target_pct",
            deadline_entity="datetime.pv_optimizer_ev_deadline",
            planned_start_entity="datetime.pv_optimizer_ev_planned_start",
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
        force_pv_export_entity=data.get(C.CONF_FORCE_PV_EXPORT),
        grid_setpoint_entity=data[C.CONF_GRID_SETPOINT],
        feedin_switch_entity=data[C.CONF_FEEDIN_SWITCH],
        battery=battery,
        p_grid_imp_max_kw=float(data[C.CONF_GRID_IMPORT_MAX_KW]),
        p_grid_exp_max_kw=float(data[C.CONF_GRID_EXPORT_MAX_KW]),
        slot_minutes=int(data[C.CONF_SLOT_MINUTES]),
        horizon_hours=int(data[C.CONF_HORIZON_HOURS]),
        setpoint_tolerance_w=float(data[C.CONF_SETPOINT_TOLERANCE_W]),
        min_sell_price_per_kwh=float(data.get(C.CONF_MIN_SELL_PRICE,
                                              C.DEFAULT_MIN_SELL_PRICE)),
        ev=ev_cfg,
    )
    cap_raw = float(data.get(C.CONF_LOAD_FORECAST_CAP_KW, C.DEFAULT_LOAD_FORECAST_CAP_KW))
    forecaster_opts = LoadForecasterOptions(
        lookback_days=int(data.get(C.CONF_LOAD_FORECAST_LOOKBACK_DAYS,
                                   C.DEFAULT_LOAD_FORECAST_LOOKBACK_DAYS)),
        cap_kw=cap_raw if cap_raw > 0 else None,
        weekday_aware=bool(data.get(C.CONF_LOAD_FORECAST_WEEKDAY_AWARE,
                                    C.DEFAULT_LOAD_FORECAST_WEEKDAY_AWARE)),
    )
    coord = PvOptimizerCoordinator(
        hass, config, int(data[C.CONF_UPDATE_SECONDS]),
        forecaster_opts=forecaster_opts,
        currency=str(data.get(C.CONF_CURRENCY, C.DEFAULT_CURRENCY)),
    )
    await coord.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload the entry whenever the user saves the Options form, so changes
    # to entity bindings / battery params / solver knobs take effect without
    # an HA restart. ``async_on_unload`` ensures the listener is detached on
    # unload so a subsequent reload doesn't double-register.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass, entry):  # type: ignore[no-untyped-def]
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):  # type: ignore[no-untyped-def]
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
