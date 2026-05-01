"""Pure planning layer: reads from a state source, runs the optimizer, applies setpoints.

This module has no Home Assistant dependency. The HA coordinator (separate
module) is a thin shim that supplies ``StateReader``/``ServiceCaller``
implementations backed by ``hass``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol

from .load_forecaster import LoadForecaster
from .models import (
    BatteryParams,
    OptimizerError,
    OptimizerInputs,
    OptimizerResult,
    TariffSlot,
)
from .optimizer import solve

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateView:
    """Minimal state shape (mirrors HA's ``State`` for our purposes)."""

    state: str | None
    attributes: dict[str, Any] = field(default_factory=dict)


class StateReader(Protocol):
    def get(self, entity_id: str) -> StateView | None: ...


class ServiceCaller(Protocol):
    def call(self, domain: str, service: str, data: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class PlannerConfig:
    # Inputs (entity IDs).
    load_power_entity: str
    pv_power_entity: str
    grid_power_entity: str
    battery_soc_entity: str
    buy_price_today_entity: str
    sell_price_today_entity: str
    pv_forecast_entity: str
    buy_price_tomorrow_entity: str | None = None
    sell_price_tomorrow_entity: str | None = None
    load_forecast_entity: str | None = None
    feedin_override_entity: str | None = None
    # Outputs (entity IDs).
    grid_setpoint_entity: str = ""
    feedin_switch_entity: str = ""
    # Battery / solver parameters.
    battery: BatteryParams = field(default_factory=lambda: BatteryParams(
        capacity_kwh=10.0, soc_min_kwh=1.0, soc_max_kwh=9.0,
        p_chg_max_kw=5.0, p_dis_max_kw=5.0,
    ))
    p_grid_imp_max_kw: float = 25.0
    p_grid_exp_max_kw: float = 25.0
    slot_minutes: int = 60
    horizon_hours: int = 24
    setpoint_tolerance_w: float = 50.0
    # Attribute keys used to read hourly price arrays (Nordpool convention).
    price_today_attr: str = "today"
    price_tomorrow_attr: str = "tomorrow"


@dataclass
class PlanCycle:
    """One planner step result, suitable for diagnostics / sensors."""

    now: datetime
    result: OptimizerResult | None
    applied_setpoint_w: float | None
    applied_feedin: bool | None
    error: str | None = None


class Planner:
    """End-to-end: read state, solve, apply setpoint + feed-in switch."""

    def __init__(self, config: PlannerConfig, reader: StateReader, caller: ServiceCaller,
                 load_forecaster: LoadForecaster | None = None) -> None:
        self.config = config
        self.reader = reader
        self.caller = caller
        self.load_forecaster = load_forecaster
        self.last: PlanCycle | None = None

    # ---- public API ------------------------------------------------------
    def step(self, now: datetime) -> PlanCycle:
        try:
            inputs = self._build_inputs(now)
            result = solve(inputs)
        except (OptimizerError, ValueError, KeyError) as exc:
            _LOGGER.warning("Planning step failed: %s", exc)
            cycle = PlanCycle(now=now, result=None, applied_setpoint_w=None,
                              applied_feedin=None, error=str(exc))
            self.last = cycle
            return cycle

        first = result.slots[0]
        net_grid_kw = first.p_buy_kw - first.p_sell_kw
        setpoint_w = net_grid_kw * 1000.0
        feedin = first.p_sell_kw > 1e-3

        prev_sp = self.last.applied_setpoint_w if self.last else None
        if prev_sp is None or abs(setpoint_w - prev_sp) > self.config.setpoint_tolerance_w:
            self._apply_setpoint(setpoint_w)
        else:
            setpoint_w = prev_sp  # unchanged
        prev_fi = self.last.applied_feedin if self.last else None
        if prev_fi is None or feedin != prev_fi:
            self._apply_feedin(feedin)

        cycle = PlanCycle(now=now, result=result, applied_setpoint_w=setpoint_w,
                          applied_feedin=feedin, error=None)
        self.last = cycle
        return cycle

    # ---- input assembly --------------------------------------------------
    def _build_inputs(self, now: datetime) -> OptimizerInputs:
        cfg = self.config
        soc_pct = self._read_float(cfg.battery_soc_entity)
        load_kw_now = self._read_float(cfg.load_power_entity) / 1000.0

        slot_h = cfg.slot_minutes / 60.0
        n_slots = max(1, int(round(cfg.horizon_hours / slot_h)))
        first_start = _floor_to_slot(now, cfg.slot_minutes)
        candidate_starts = [first_start + timedelta(minutes=cfg.slot_minutes * i) for i in range(n_slots)]

        # Build {hour-key -> price} maps. Both list (legacy Nordpool-style) and
        # timestamp-keyed dict shapes are accepted; a missing future hour
        # truncates the horizon, while a missing current hour is a hard error
        # (treats stale-data scenarios explicitly).
        buy_map = self._build_price_map(
            cfg.buy_price_today_entity, cfg.price_today_attr,
            cfg.buy_price_tomorrow_entity, cfg.price_tomorrow_attr,
            first_start.date(),
        )
        sell_map = self._build_price_map(
            cfg.sell_price_today_entity, cfg.price_today_attr,
            cfg.sell_price_tomorrow_entity, cfg.price_tomorrow_attr,
            first_start.date(),
        )
        feedin_global = self._read_bool_optional(cfg.feedin_override_entity, default=True)

        slots: list[TariffSlot] = []
        slot_starts: list[datetime] = []
        for start in candidate_starts:
            hour_key = start.replace(minute=0, second=0, microsecond=0)
            pb = buy_map.get(hour_key)
            ps = sell_map.get(hour_key)
            if pb is None or ps is None:
                if not slots:
                    raise ValueError(
                        f"price data for current slot {hour_key.isoformat()} is missing "
                        "(stale tariff sensor?)"
                    )
                break  # truncate horizon at first missing future hour
            slots.append(TariffSlot(
                start=start, duration_h=slot_h,
                price_buy=pb, price_sell=ps,
                feedin_allowed=feedin_global,
            ))
            slot_starts.append(start)

        pv_kw = self._read_pv_forecast(cfg.pv_forecast_entity, slot_starts, slot_h)
        load_kw = self._read_load_forecast(cfg.load_forecast_entity, slot_starts, slot_h, load_kw_now)
        soc_kwh = max(cfg.battery.soc_min_kwh,
                      min(cfg.battery.soc_max_kwh,
                          soc_pct / 100.0 * cfg.battery.capacity_kwh))

        return OptimizerInputs(
            slots=slots, pv_kw=pv_kw, load_kw=load_kw,
            initial_soc_kwh=soc_kwh, battery=cfg.battery,
            p_grid_imp_max_kw=cfg.p_grid_imp_max_kw,
            p_grid_exp_max_kw=cfg.p_grid_exp_max_kw,
        )

    # ---- price source readers -------------------------------------------
    def _build_price_map(self, entity_today: str, attr_today: str,
                         entity_tomorrow: str | None, attr_tomorrow: str,
                         today_date) -> dict[datetime, float]:
        """Merge today's (and optionally tomorrow's) price entity into one map.

        Keys are naive UTC datetimes truncated to the hour. Tomorrow's entries
        override today's on overlap (rare, but explicit).
        """
        out: dict[datetime, float] = {}
        out.update(self._read_price_source(entity_today, attr_today, today_date))
        if entity_tomorrow:
            try:
                out.update(self._read_price_source(
                    entity_tomorrow, attr_tomorrow, today_date + timedelta(days=1)))
            except (KeyError, ValueError):
                # Tomorrow not yet published — that's fine, planner truncates.
                pass
        return out

    def _read_price_source(self, entity_id: str, attr: str,
                           default_date) -> dict[datetime, float]:
        """Read one price entity.

        Three shapes are accepted:
        * ``attributes[attr]`` is a ``dict[iso_timestamp, float]`` (wrapped),
        * ``attributes[attr]`` is a ``list[float]`` of 24 hourly prices,
        * the entity exposes hour timestamps as *top-level* attribute keys
          (``attributes`` itself is the price map). This is what plugins like
          ``spot_hodinovy_tarif`` do — there is no wrapping attribute.
        """
        st = self._state(entity_id)
        raw = st.attributes.get(attr)
        if isinstance(raw, dict) and raw:
            return self._parse_iso_keyed_dict(raw, lenient=False)
        if isinstance(raw, (list, tuple)) and raw:
            return {datetime.combine(default_date, time(hour=i)): float(v)
                    for i, v in enumerate(raw)}
        # Fallback: hour timestamps are themselves the attribute keys.
        scanned = self._parse_iso_keyed_dict(st.attributes, lenient=True)
        if scanned:
            return scanned
        raise ValueError(
            f"entity {entity_id!r} attribute {attr!r} missing or unsupported shape "
            "(expected list[float], dict[iso_timestamp, float], or iso-keyed attributes)"
        )

    @staticmethod
    def _parse_iso_keyed_dict(raw: dict, *, lenient: bool) -> dict[datetime, float]:
        """Parse {iso_timestamp: number} into {naive-UTC hour: float}.

        With ``lenient=True``, entries whose key isn't an ISO timestamp or
        whose value isn't numeric are silently skipped (used when scanning a
        whole ``attributes`` dict that may carry unrelated metadata).
        """
        out: dict[datetime, float] = {}
        for k, v in raw.items():
            try:
                key = _parse_iso(str(k)).replace(minute=0, second=0, microsecond=0)
            except (ValueError, TypeError):
                if lenient:
                    continue
                raise
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                if lenient:
                    continue
                raise
        return out


    # ---- low-level readers ----------------------------------------------
    def _state(self, entity_id: str) -> StateView:
        st = self.reader.get(entity_id)
        if st is None:
            raise KeyError(f"entity {entity_id!r} not found")
        return st

    def _read_float(self, entity_id: str) -> float:
        st = self._state(entity_id)
        if st.state in (None, "", "unknown", "unavailable"):
            raise ValueError(f"entity {entity_id!r} has no numeric state")
        return float(st.state)

    def _read_bool_optional(self, entity_id: str | None, *, default: bool) -> bool:
        if not entity_id:
            return default
        st = self.reader.get(entity_id)
        if st is None or st.state in (None, "", "unknown", "unavailable"):
            return default
        return str(st.state).lower() in ("on", "true", "1", "yes")

    def _read_pv_forecast(self, entity_id: str, slot_starts: list[datetime], slot_h: float) -> list[float]:
        st = self._state(entity_id)
        # Accept either {"wh_hours": {"<iso>": Wh, ...}} (forecast.solar) or
        # {"forecast": [{"datetime": "<iso>", "power_kw": <kW>}, ...]}.
        wh_hours = st.attributes.get("wh_hours")
        if isinstance(wh_hours, dict) and wh_hours:
            mapping = {_parse_iso(k): float(v) / 1000.0 / 1.0 for k, v in wh_hours.items()}
            # wh_hours values are energy per 1h slot; convert Wh -> kW (since slot=1h).
            # If our slot != 1h we still treat each entry as average power for that hour.
            return [_lookup_forecast(mapping, s, slot_h) for s in slot_starts]
        forecast = st.attributes.get("forecast")
        if isinstance(forecast, list) and forecast:
            mapping = {_parse_iso(p["datetime"]): float(p.get("power_kw", p.get("power", 0)))
                       for p in forecast if "datetime" in p}
            return [_lookup_forecast(mapping, s, slot_h) for s in slot_starts]
        raise ValueError(f"PV forecast entity {entity_id!r} has no recognised attribute")

    def _read_load_forecast(self, entity_id: str | None, slot_starts: list[datetime],
                            slot_h: float, fallback_kw: float) -> list[float]:
        # External-entity escape hatch takes precedence over the built-in
        # forecaster, so users can plug in their own forecast without code
        # changes.
        if entity_id:
            st = self.reader.get(entity_id)
            if st is not None:
                forecast = st.attributes.get("forecast")
                if isinstance(forecast, list) and forecast:
                    mapping = {_parse_iso(p["datetime"]): float(p.get("power_kw", p.get("power", 0)))
                               for p in forecast if "datetime" in p}
                    return [_lookup_forecast(mapping, s, slot_h, default=fallback_kw)
                            for s in slot_starts]
            return [max(0.0, fallback_kw)] * len(slot_starts)
        if self.load_forecaster is not None:
            fc = self.load_forecaster.forecast(slot_starts)
            return [fc.kw_per_slot.get(s, max(0.0, fallback_kw)) if fc.days_used_per_slot.get(s, 0) > 0
                    else max(0.0, fallback_kw) for s in slot_starts]
        return [max(0.0, fallback_kw)] * len(slot_starts)

    # ---- output appliers -------------------------------------------------
    def _apply_setpoint(self, value_w: float) -> None:
        if not self.config.grid_setpoint_entity:
            return
        self.caller.call("number", "set_value",
                         {"entity_id": self.config.grid_setpoint_entity, "value": float(value_w)})

    def _apply_feedin(self, allowed: bool) -> None:
        if not self.config.feedin_switch_entity:
            return
        service = "turn_on" if allowed else "turn_off"
        self.caller.call("switch", service, {"entity_id": self.config.feedin_switch_entity})


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _floor_to_slot(now: datetime, slot_minutes: int) -> datetime:
    minute = (now.minute // slot_minutes) * slot_minutes
    return now.replace(minute=minute, second=0, microsecond=0)


def _parse_iso(s: str) -> datetime:
    # Accept "...Z" and offset-aware/naive ISO strings; return naive UTC for keying.
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def _lookup_forecast(mapping: dict[datetime, float], slot_start: datetime,
                     slot_h: float, default: float = 0.0) -> float:
    key = slot_start.replace(tzinfo=None) if slot_start.tzinfo else slot_start
    key = key.replace(second=0, microsecond=0, minute=(key.minute // 60) * 60)
    return float(mapping.get(key, default))
