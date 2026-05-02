"""Constants for the pv_optimizer integration."""
from __future__ import annotations

DOMAIN = "pv_optimizer"
PLATFORMS = ["sensor"]

# --- Configuration keys: input sensor entity ids ---
CONF_LOAD_POWER = "load_power_entity"
CONF_PV_POWER = "pv_power_entity"
CONF_GRID_POWER = "grid_power_entity"
CONF_BATTERY_SOC = "battery_soc_entity"
CONF_BUY_PRICE_TODAY = "buy_price_today_entity"
CONF_BUY_PRICE_TOMORROW = "buy_price_tomorrow_entity"
CONF_SELL_PRICE_TODAY = "sell_price_today_entity"
CONF_SELL_PRICE_TOMORROW = "sell_price_tomorrow_entity"
CONF_PV_FORECAST = "pv_forecast_entity"
CONF_LOAD_FORECAST = "load_forecast_entity"
CONF_FEEDIN_OVERRIDE = "feedin_override_entity"

# --- Configuration keys: control entity ids ---
CONF_GRID_SETPOINT = "grid_setpoint_entity"
CONF_FEEDIN_SWITCH = "feedin_switch_entity"
CONF_MAX_CHARGE_POWER_ENTITY = "max_charge_power_entity"
CONF_MAX_DISCHARGE_POWER_ENTITY = "max_discharge_power_entity"

# --- Battery parameters ---
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_SOC_MIN_PCT = "battery_soc_min_pct"
CONF_BATTERY_SOC_MAX_PCT = "battery_soc_max_pct"
CONF_BATTERY_P_CHG_MAX_KW = "battery_p_chg_max_kw"
CONF_BATTERY_P_DIS_MAX_KW = "battery_p_dis_max_kw"
CONF_BATTERY_ETA_CHG = "battery_eta_chg"
CONF_BATTERY_ETA_DIS = "battery_eta_dis"
CONF_BATTERY_CYCLE_COST = "battery_cycle_cost_per_kwh"

# --- Solver / planning ---
CONF_SLOT_MINUTES = "slot_minutes"
CONF_HORIZON_HOURS = "horizon_hours"
CONF_UPDATE_SECONDS = "update_seconds"
CONF_GRID_IMPORT_MAX_KW = "grid_import_max_kw"
CONF_GRID_EXPORT_MAX_KW = "grid_export_max_kw"
CONF_SETPOINT_TOLERANCE_W = "setpoint_tolerance_w"

# --- Tariff surcharges (added to spot prices) ---
CONF_BUY_SURCHARGE = "buy_surcharge_per_kwh"
CONF_SELL_SURCHARGE = "sell_surcharge_per_kwh"

# --- Currency (display only; the planner is currency-agnostic and treats
# tariff prices as opaque per-kWh numbers in whatever unit the user's
# tariff sensors report). Used as the unit_of_measurement for cost/savings
# sensors and as a label suffix on currency-denominated config fields.
CONF_CURRENCY = "currency"

# --- Built-in load forecaster ---
CONF_LOAD_FORECAST_LOOKBACK_DAYS = "load_forecast_lookback_days"
CONF_LOAD_FORECAST_CAP_KW = "load_forecast_cap_kw"
CONF_LOAD_FORECAST_WEEKDAY_AWARE = "load_forecast_weekday_aware"

# --- Defaults ---
DEFAULT_SLOT_MINUTES = 60
DEFAULT_HORIZON_HOURS = 24
DEFAULT_UPDATE_SECONDS = 300
DEFAULT_SOC_MIN_PCT = 10.0
DEFAULT_SOC_MAX_PCT = 100.0
DEFAULT_ETA_CHG = 0.95
DEFAULT_ETA_DIS = 0.95
DEFAULT_CYCLE_COST = 0.05  # currency/kWh of throughput; tune to your currency
                           # (typical: ~0.05 EUR, ~0.06 USD, ~1.5 CZK, etc.)
DEFAULT_SETPOINT_TOLERANCE_W = 50.0
DEFAULT_LOAD_FORECAST_LOOKBACK_DAYS = 7
DEFAULT_LOAD_FORECAST_CAP_KW = 0.0   # 0 = no cap
DEFAULT_LOAD_FORECAST_WEEKDAY_AWARE = False
DEFAULT_CURRENCY = "EUR"
