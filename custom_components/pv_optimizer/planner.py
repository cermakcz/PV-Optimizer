"""Pure planning layer: reads from a state source, runs the optimizer, applies setpoints.

This module has no Home Assistant dependency. The HA coordinator (separate
module) is a thin shim that supplies ``StateReader``/``ServiceCaller``
implementations backed by ``hass``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol

import pulp

from .load_forecaster import LoadForecaster
from .models import (
    BatteryParams,
    OptimizerError,
    OptimizerInputs,
    OptimizerResult,
    SlotPlan,
    TariffSlot,
)
from .optimizer import solve

_LOGGER = logging.getLogger(__name__)

# Threshold (kW) above which an LP variable is treated as a meaningful
# command rather than solver noise. Shared between force-charge / force-
# discharge detection and the passive-projection helper so both agree on
# which slots are "active" vs "passive".
_FORCE_EPS = 1e-3


@dataclass(frozen=True)
class StateView:
    """Minimal state shape (mirrors HA's ``State`` for our purposes)."""

    state: str | None
    attributes: dict[str, Any] = field(default_factory=dict)


class StateReader(Protocol):
    def get(self, entity_id: str) -> StateView | None: ...


class ServiceCaller(Protocol):
    def call(self, domain: str, service: str, data: dict[str, Any]) -> None: ...


class LiveAverager(Protocol):
    """Computes a time-weighted-average of a numeric power entity.

    The planner uses this to refine slot-0 PV against live measurements,
    so the active force-export branch responds to clouds within one
    planner cycle instead of waiting for the upstream PV forecaster
    (typically hourly) to refresh.
    """

    def average_kw(self, entity_id: str, since: datetime, until: datetime
                   ) -> float | None: ...


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
    force_pv_export_entity: str | None = None
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
    # Per-slot floor on sell price. Slots with ``price_sell`` strictly below
    # this value have their ``feedin_allowed`` flag forced off, which the
    # optimizer enforces as ``p_sell[t] = 0``. Useful when the marginal sell
    # revenue (e.g. 0.1 CZK/kWh) doesn't justify exporting at all. 0.0
    # disables the floor entirely (any non-negative sell price is acceptable).
    min_sell_price_per_kwh: float = 0.0
    # Cycle cadence; doubles as the trailing window for the slot-0 PV
    # smoothing helper so the active force-export branch reacts to clouds
    # at the same granularity it replans.
    update_seconds: int = 300
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
    force_pv_export_enabled: bool | None = None
    error: str | None = None


class Planner:
    """End-to-end: read state, solve, apply setpoint + feed-in switch."""

    def __init__(self, config: PlannerConfig, reader: StateReader, caller: ServiceCaller,
                 load_forecaster: LoadForecaster | None = None,
                 live_averager: LiveAverager | None = None) -> None:
        self.config = config
        self.reader = reader
        self.caller = caller
        self.load_forecaster = load_forecaster
        # Optional. When supplied, slot-0 PV is refined via
        # ``min(forecast, trailing_avg)`` so the active force-export branch
        # can't speculate above measured production.
        self.live_averager = live_averager
        self.last: PlanCycle | None = None

    # ---- public API ------------------------------------------------------
    def step(self, now: datetime) -> PlanCycle:
        try:
            inputs = self._build_inputs(now)
            result = solve(inputs)
        except (OptimizerError, ValueError, KeyError, pulp.PulpError, OSError) as exc:
            _LOGGER.warning("Planning step failed: %s", exc)
            cycle = PlanCycle(now=now, result=None, applied_setpoint_w=None,
                              applied_feedin=None, error=str(exc))
            self.last = cycle
            return cycle

        # Read the force-export toggle once so the projection and the
        # control branch agree on which slots are LP-driven and which fall
        # back to inverter self-consumption.
        force_pv_export_enabled = self._read_bool_optional(
            self.config.force_pv_export_entity, default=False)

        # Augment the LP slots with a physical SoC projection. The LP
        # bookkeeping curtails surplus PV in passive slots (so soc_start_kwh
        # may stay flat across midday); the projection re-runs the inverter's
        # self-consumption rule to estimate the SoC the battery will actually
        # reach. Useful for chart overlays, not used for control.
        result = replace(result,
                         slots=_attach_physical_soc(
                             inputs, result.slots, _FORCE_EPS,
                             force_pv_export_enabled=force_pv_export_enabled))

        # Translate the LP plan for the next slot into inverter actions.
        #
        # The grid setpoint should only override the inverter's natural
        # self-consumption logic when the LP actively wants to move energy
        # *between the battery and the grid*. In every other case
        # (PV surplus export, PV-deficit import, idle battery) we hand
        # control back to the inverter with setpoint = 0 — the Multiplus
        # then runs self-consumption mode (PV → load → battery → grid)
        # with surplus exported iff the feed-in switch is on.
        #
        # Forcing a non-zero setpoint when the LP doesn't intend a battery
        # transfer is brittle to forecast error: a PV undershoot would make
        # the inverter discharge the battery to hit a negative target it
        # was never asked to defend. Setpoint = 0 puts the inverter back
        # in charge of those degrees of freedom.
        #
        # Opt-in extension via ``force_pv_export_entity``: when the toggle
        # is on, also force the setpoint when the LP wants a pure PV export
        # (``p_sell > 0`` with the battery idle). This is only safe because
        # ``_build_inputs`` substitutes ``min(forecast, live_avg)`` for
        # slot-0 PV, so the LP can't speculate above measured production.
        #
        # Force-hold-import (PRD §8.6): the LP can also rationally plan to
        # cover load purely from the grid with the battery idle (typically
        # when the §8.5 health floor makes further discharge expensive, or
        # when the configured ``soc_min`` sits above the inverter's BMS
        # floor). The native EMS would still drain the battery first in
        # that case, silently violating the plan, so we explicitly pin the
        # grid set-point to the planned import. Always-on: when the
        # predicate doesn't fire (e.g. battery already at hard floor) the
        # forced positive set-point produces the same physical behaviour
        # as set-point 0.
        first = result.slots[0]
        force_discharge = first.p_dis_kw > _FORCE_EPS and first.p_sell_kw > _FORCE_EPS
        force_charge = first.p_buy_kw > _FORCE_EPS and first.p_chg_kw > _FORCE_EPS
        force_export = (force_pv_export_enabled
                        and first.p_sell_kw > _FORCE_EPS
                        and first.p_chg_kw < _FORCE_EPS
                        and first.p_dis_kw < _FORCE_EPS)
        force_hold_import = (first.p_buy_kw > _FORCE_EPS
                             and first.p_chg_kw < _FORCE_EPS
                             and first.p_dis_kw < _FORCE_EPS)
        if force_discharge or force_charge or force_export or force_hold_import:
            setpoint_w = (first.p_buy_kw - first.p_sell_kw) * 1000.0
        else:
            setpoint_w = 0.0
        feedin = first.p_sell_kw > _FORCE_EPS

        prev_sp = self.last.applied_setpoint_w if self.last else None
        if prev_sp is None or abs(setpoint_w - prev_sp) > self.config.setpoint_tolerance_w:
            self._apply_setpoint(setpoint_w)
        else:
            setpoint_w = prev_sp  # unchanged
        prev_fi = self.last.applied_feedin if self.last else None
        if prev_fi is None or feedin != prev_fi:
            self._apply_feedin(feedin)

        cycle = PlanCycle(now=now, result=result, applied_setpoint_w=setpoint_w,
                          applied_feedin=feedin,
                          force_pv_export_enabled=force_pv_export_enabled,
                          error=None)
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
            # Per-slot sell-price floor. Sub-threshold slots get
            # ``feedin_allowed=False`` so the optimizer pins ``p_sell=0`` and
            # the LP plans around the lost revenue (e.g. keeps PV in the
            # battery for a higher-price slot later in the day).
            feedin_for_slot = feedin_global and (ps >= cfg.min_sell_price_per_kwh)
            slots.append(TariffSlot(
                start=start, duration_h=slot_h,
                price_buy=pb, price_sell=ps,
                feedin_allowed=feedin_for_slot,
            ))
            slot_starts.append(start)

        pv_kw = self._read_pv_forecast(cfg.pv_forecast_entity, slot_starts, slot_h)
        # Refine slot-0 PV against a measured trailing average so the active
        # force-export branch responds to clouds within one planner cycle.
        # ``min(forecast, live)`` is asymmetric on purpose: we only cut the
        # plan when reality undershoots the forecast — never speculate above
        # the upstream forecaster's view of the slot average.
        if pv_kw and self.live_averager is not None and cfg.pv_power_entity:
            window = max(timedelta(seconds=cfg.update_seconds), timedelta(seconds=30))
            live = self.live_averager.average_kw(
                cfg.pv_power_entity, now - window, now)
            if live is not None:
                pv_kw[0] = min(pv_kw[0], max(0.0, live))
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

        Four shapes are accepted, tried in order:
        * ``attributes[attr]`` is a ``dict[iso_timestamp, float]`` (wrapped),
        * ``attributes[attr]`` is a ``list[float]`` of 24 hourly prices,
        * any *other* dict-valued attribute looks like an ISO-keyed price map
          (auto-discovery — handles templates that publish under names like
          ``prices`` / ``raw_today`` without requiring user config),
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
        # Auto-discovery: scan other dict-valued attributes for one whose keys
        # parse as ISO timestamps. Picks the largest such map, so a stray small
        # dict elsewhere in attributes doesn't shadow the real price map.
        best: dict[datetime, float] = {}
        for k, v in st.attributes.items():
            if k == attr or not isinstance(v, dict) or not v:
                continue
            parsed = self._parse_iso_keyed_dict(v, lenient=True)
            if len(parsed) > len(best):
                best = parsed
        if best:
            return best
        # Last resort: hour timestamps are themselves the attribute keys.
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


def naive_utc_to_iso(dt: datetime) -> str:
    """Serialise a naive-UTC datetime as an offset-aware ISO 8601 string.

    Internal slot keys are naive UTC (see ``_parse_iso``). Frontends like
    apexcharts-card need an explicit offset to convert reliably to the
    browser's local timezone; without one, ISO strings are interpreted
    inconsistently across cards/browsers and series end up misaligned on
    the x-axis. Already-aware datetimes are converted to UTC first so the
    output is always ``...+00:00``.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _lookup_forecast(mapping: dict[datetime, float], slot_start: datetime,
                     slot_h: float, default: float = 0.0) -> float:
    key = slot_start.replace(tzinfo=None) if slot_start.tzinfo else slot_start
    key = key.replace(second=0, microsecond=0, minute=(key.minute // 60) * 60)
    return float(mapping.get(key, default))


def _attach_physical_soc(inputs: OptimizerInputs,
                         plan_slots: list[SlotPlan],
                         eps: float,
                         *,
                         force_pv_export_enabled: bool = False) -> list[SlotPlan]:
    """Return ``plan_slots`` with ``soc_physical_kwh`` filled in.

    For each slot we estimate the SoC the inverter will physically reach
    at slot start, distinct from the LP's bookkeeping ``soc_start_kwh``:

    * If the LP forces a battery action (force-charge, force-discharge,
      force-export, or force-hold-import — the same predicate the planner
      uses to override the inverter setpoint), the inverter follows the
      LP and we apply the LP's own SoC update. For force-export and
      force-hold-import the LP keeps the battery idle (``p_chg = p_dis
      = 0``) so the projection stays flat too.
    * Otherwise the inverter runs in self-consumption mode (setpoint=0):
      surplus PV charges the battery up to ``soc_max``, deficit is met by
      discharging down to ``soc_min``; the rest spills to grid or is
      curtailed. This is what the Multiplus actually does between forced
      slots, so the projected SoC tracks reality more closely than the LP
      bookkeeping does (which curtails surplus PV in passive slots).
    """
    bat = inputs.battery
    soc = inputs.initial_soc_kwh
    out: list[SlotPlan] = []
    for t, sp in enumerate(plan_slots):
        dt = inputs.slots[t].duration_h
        pv = inputs.pv_kw[t]
        load = inputs.load_kw[t]
        force_discharge = sp.p_dis_kw > eps and sp.p_sell_kw > eps
        force_charge = sp.p_buy_kw > eps and sp.p_chg_kw > eps
        force_export = (force_pv_export_enabled
                        and sp.p_sell_kw > eps
                        and sp.p_chg_kw < eps
                        and sp.p_dis_kw < eps)
        force_hold_import = (sp.p_buy_kw > eps
                             and sp.p_chg_kw < eps
                             and sp.p_dis_kw < eps)
        out.append(replace(sp, soc_physical_kwh=soc))
        if force_charge or force_discharge or force_export or force_hold_import:
            dsoc = dt * (bat.eta_chg * sp.p_chg_kw - sp.p_dis_kw / bat.eta_dis)
        else:
            net = pv - load  # >0 surplus, <0 deficit, all on AC side
            if net > eps:
                # AC kW that fits in remaining headroom over this slot.
                max_chg_ac = (bat.soc_max_kwh - soc) / (bat.eta_chg * dt) if dt > 0 else 0.0
                chg_ac = min(net, bat.p_chg_max_kw, max(0.0, max_chg_ac))
                dsoc = dt * bat.eta_chg * chg_ac
            elif net < -eps:
                # AC kW deliverable from remaining usable energy over this slot.
                max_dis_ac = (soc - bat.soc_min_kwh) * bat.eta_dis / dt if dt > 0 else 0.0
                dis_ac = min(-net, bat.p_dis_max_kw, max(0.0, max_dis_ac))
                dsoc = -dt * dis_ac / bat.eta_dis
            else:
                dsoc = 0.0
        soc = max(bat.soc_min_kwh, min(bat.soc_max_kwh, soc + dsoc))
    return out
