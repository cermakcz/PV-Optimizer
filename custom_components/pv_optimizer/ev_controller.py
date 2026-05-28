"""Pure decision logic for the EV charging feature.

No Home Assistant imports. Owned by tests/test_ev_controller.py.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Sequence


class EVStateClass(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTED_IDLE = "connected_idle"
    CONNECTED_REQUESTING = "connected_requesting"


# Default substring vocabulary. All matches are case-insensitive.
# Precedence in classify_state: DISCONNECTED > CONNECTED_REQUESTING > CONNECTED_IDLE.
DEFAULT_STATE_VOCAB: Mapping[EVStateClass, Sequence[str]] = {
    EVStateClass.DISCONNECTED: ("disconnect", "idle", "unplug"),
    EVStateClass.CONNECTED_REQUESTING: (
        "charging", "wait sun", "wait_sun",
        "wait time", "wait start", "wait rfid",
    ),
    EVStateClass.CONNECTED_IDLE: ("charged", "connect"),
}

_UNAVAILABLE_STATES = frozenset({"unknown", "unavailable", "none", ""})


def classify_state(
    state: str | None,
    vocab: Mapping[EVStateClass, Sequence[str]] = DEFAULT_STATE_VOCAB,
) -> EVStateClass:
    """Classify a raw charger-state string into one of three classes.

    Returns ``DISCONNECTED`` for ``None`` / empty / ``unknown`` / ``unavailable``
    so the planner treats stale inputs as "no car" and bails (per spec §8).
    """
    if state is None:
        return EVStateClass.DISCONNECTED
    s = state.strip().lower()
    if not s or s in _UNAVAILABLE_STATES:
        return EVStateClass.DISCONNECTED
    # Precedence: disconnected > requesting > idle.
    for cls in (
        EVStateClass.DISCONNECTED,
        EVStateClass.CONNECTED_REQUESTING,
        EVStateClass.CONNECTED_IDLE,
    ):
        for needle in vocab.get(cls, ()):
            if needle.lower() in s:
                return cls
    return EVStateClass.CONNECTED_IDLE  # conservative fallback


@dataclass(frozen=True)
class ReactiveDecision:
    """Decision output for one planner tick (reactive branch)."""

    max_current_a: int   # integer A; 0 disables charging


def decide_reactive(
    *,
    state_class: EVStateClass,
    grid_power_w: float,
    ev_charging_power_w: float,
    price_buy: float,
    ev,  # EVParams (avoid cyclic import at module top)
) -> ReactiveDecision:
    """One-shot reactive decision per §4.2 (no mode-switching).

    Args:
        state_class: classified state of the charger.
        grid_power_w: site-level grid power (positive = import, negative = export).
        ev_charging_power_w: power the EV is currently drawing.
        price_buy: current buy price (currency/kWh, all-in).
        ev: EVParams with kw_per_amp, min/max current, buy_price_threshold.
    """
    if state_class == EVStateClass.DISCONNECTED:
        return ReactiveDecision(max_current_a=0)
    if state_class == EVStateClass.CONNECTED_REQUESTING:
        # Ultimate override — car has actively negotiated for power (§4.3).
        return ReactiveDecision(max_current_a=int(round(ev.max_charging_current_a)))
    if price_buy <= ev.buy_price_threshold:
        return ReactiveDecision(max_current_a=int(round(ev.max_charging_current_a)))
    # Surplus tracking: back-add what EV is already drawing so loop converges.
    surplus_kw = max(0.0, (-grid_power_w + ev_charging_power_w) / 1000.0)
    target_a = surplus_kw / ev.kw_per_amp
    if target_a < ev.min_charging_current_a:
        return ReactiveDecision(max_current_a=0)
    clamped = max(ev.min_charging_current_a,
                  min(ev.max_charging_current_a, target_a))
    return ReactiveDecision(max_current_a=int(clamped))


@dataclass(frozen=True)
class LatchState:
    """Persistent state for the mode-switching reactive variant (§4.1)."""

    cheap_grid: bool = False
    ultimate_override: bool = False

    @property
    def any_set(self) -> bool:
        return self.cheap_grid or self.ultimate_override


def update_latches(
    prev: LatchState,
    *,
    state_class: EVStateClass,
    price_buy: float,
    ev_charging_power_w: float,
    last_state_class: EVStateClass,
    time_in_current_class_s: float,
    ev,
) -> LatchState:
    """Advance latches one tick per §4.1 trigger/release semantics.

    Args:
        prev: previous latch state.
        state_class: current classified state of the charger.
        price_buy: current buy price.
        ev_charging_power_w: instantaneous EV charging power.
        last_state_class: state class on the previous tick (used to
            detect "leaves CONNECTED_REQUESTING" for the override release).
        time_in_current_class_s: seconds the state has been in
            ``state_class`` consecutively (the planner tracks this).
        ev: EVParams.
    """
    # Cheap-grid: symmetric trigger/release on price threshold.
    if price_buy <= ev.buy_price_threshold:
        cheap_grid = True
    else:
        cheap_grid = False  # release on first tick above threshold

    # Ultimate-override: asymmetric.
    #   Trigger: state == REQUESTING AND ev_power < threshold.
    #   Release: state has left REQUESTING for ≥ session_done_seconds.
    if (state_class == EVStateClass.CONNECTED_REQUESTING
            and ev_charging_power_w < ev.session_done_power_w):
        ultimate_override = True
    elif prev.ultimate_override:
        if state_class == EVStateClass.CONNECTED_REQUESTING:
            ultimate_override = True  # still requesting; hold
        elif time_in_current_class_s >= ev.session_done_seconds:
            ultimate_override = False
        else:
            ultimate_override = True
    else:
        ultimate_override = False

    return LatchState(cheap_grid=cheap_grid, ultimate_override=ultimate_override)


def is_session_done(
    *,
    state_class: EVStateClass,
    ev_charging_power_w: float,
    low_power_seconds: float,
    ev,
) -> bool:
    """Return True per §6.1 session-done definition.

    Done iff:
        - disconnected; OR
        - connected_idle AND ev_power < session_done_power_w for
          ≥ session_done_seconds (caller tracks the duration).
    """
    if state_class == EVStateClass.DISCONNECTED:
        return True
    if (state_class == EVStateClass.CONNECTED_IDLE
            and ev_charging_power_w < ev.session_done_power_w
            and low_power_seconds >= ev.session_done_seconds):
        return True
    return False


def translate_lp_slot0(
    *,
    p_ev_chg_kw: float,
    state_class: EVStateClass,
    ev_charging_power_w: float,
    ev,
) -> int:
    """Convert the LP's slot-0 EV power into a charger max-current setpoint (A).

    Per spec §5.3:
    - If car is actively requesting AND not drawing meaningful power
      (ev_charging_power_w < session_done_power_w), honour ultimate-override
      with max current regardless of the LP plan.
    - If LP plans zero, write zero.
    - If LP plans > 0 but the converted current is below
      ``min_charging_current_a``, clamp UP (contrast with reactive's
      skip-below-min): the user has committed to a target, so a minor
      slot-0 overshoot is acceptable. The next tick re-plans with reduced
      remaining_kwh.
    """
    if (state_class == EVStateClass.CONNECTED_REQUESTING
            and ev_charging_power_w < ev.session_done_power_w):
        return int(round(ev.max_charging_current_a))
    if state_class == EVStateClass.DISCONNECTED:
        return 0
    if p_ev_chg_kw <= 0:
        return 0
    target_a = p_ev_chg_kw / ev.kw_per_amp
    clamped = max(ev.min_charging_current_a,
                  min(ev.max_charging_current_a, target_a))
    return int(round(clamped))
