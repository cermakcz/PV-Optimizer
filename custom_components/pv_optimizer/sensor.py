"""Diagnostic sensors exposing the latest planning cycle.

Imports ``homeassistant`` and is therefore not unit-tested in this repository.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PvOptimizerCoordinator
from .planner import PlanCycle


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coord: PvOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        _PlannedSetpointSensor(coord),
        _PlannedFeedInSensor(coord),
        _ExpectedCostSensor(coord),
        _SavingsSensor(coord),
        _PlanSensor(coord),
    ]
    if coord.forecaster is not None:
        entities.append(_LoadForecastSensor(coord))
    async_add_entities(entities)


class _Base(CoordinatorEntity[PvOptimizerCoordinator], SensorEntity):
    _attr_should_poll = False

    def __init__(self, coordinator: PvOptimizerCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}" if coordinator.config_entry else key
        self._attr_name = f"PV Optimizer {name}"

    @property
    def _cycle(self) -> PlanCycle | None:
        return self.coordinator.data

    @property
    def available(self) -> bool:
        c = self._cycle
        return c is not None and c.error is None and c.result is not None


class _PlannedSetpointSensor(_Base):
    _attr_native_unit_of_measurement = "W"

    def __init__(self, coord: PvOptimizerCoordinator) -> None:
        super().__init__(coord, "planned_grid_setpoint", "Planned Grid Setpoint")

    @property
    def native_value(self) -> float | None:
        c = self._cycle
        return None if c is None else c.applied_setpoint_w


class _PlannedFeedInSensor(_Base):
    def __init__(self, coord: PvOptimizerCoordinator) -> None:
        super().__init__(coord, "planned_feed_in", "Planned Feed-In")

    @property
    def native_value(self) -> str | None:
        c = self._cycle
        if c is None or c.applied_feedin is None:
            return None
        return "on" if c.applied_feedin else "off"


class _ExpectedCostSensor(_Base):
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, coord: PvOptimizerCoordinator) -> None:
        super().__init__(coord, "expected_cost_horizon", "Expected Cost (Horizon)")

    @property
    def native_value(self) -> float | None:
        c = self._cycle
        return None if c is None or c.result is None else round(c.result.total_cost_eur, 4)


class _SavingsSensor(_Base):
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, coord: PvOptimizerCoordinator) -> None:
        super().__init__(coord, "savings_vs_passive", "Savings vs Passive")

    @property
    def native_value(self) -> float | None:
        c = self._cycle
        return None if c is None or c.result is None else round(c.result.savings_eur, 4)


class _PlanSensor(_Base):
    _attr_native_unit_of_measurement = "W"

    def __init__(self, coord: PvOptimizerCoordinator) -> None:
        super().__init__(coord, "plan", "Plan")

    @property
    def native_value(self) -> float | None:
        c = self._cycle
        return None if c is None else c.applied_setpoint_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self._cycle
        if c is None or c.result is None:
            return {}
        return {
            "status": c.result.status,
            "solve_time_s": round(c.result.solve_time_s, 4),
            "horizon_slots": len(c.result.slots),
            "slots": [asdict(s) for s in c.result.slots],
            "error": c.error,
        }


class _LoadForecastSensor(_Base):
    """Diagnostic view of the built-in load forecaster's most recent output."""

    _attr_native_unit_of_measurement = "kW"

    def __init__(self, coord: PvOptimizerCoordinator) -> None:
        super().__init__(coord, "load_forecast", "Load Forecast")

    @property
    def available(self) -> bool:
        fc = self.coordinator.forecaster
        return fc is not None and fc.last_forecast is not None

    @property
    def native_value(self) -> float | None:
        fc = self.coordinator.forecaster
        if fc is None or fc.last_forecast is None or not fc.last_forecast.kw_per_slot:
            return None
        # State = next-slot kW (first by chronological order).
        next_slot = min(fc.last_forecast.kw_per_slot)
        return round(fc.last_forecast.kw_per_slot[next_slot], 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fc = self.coordinator.forecaster
        if fc is None or fc.last_forecast is None:
            return {}
        kw = fc.last_forecast.kw_per_slot
        used = fc.last_forecast.days_used_per_slot
        return {
            "lookback_days": fc.config.lookback_days,
            "cap_kw": fc.config.cap_kw,
            "weekday_aware": fc.config.weekday_aware,
            "kw_per_slot": {k.isoformat(): round(v, 3) for k, v in kw.items()},
            "days_used_per_slot": {k.isoformat(): used.get(k, 0) for k in kw},
        }
