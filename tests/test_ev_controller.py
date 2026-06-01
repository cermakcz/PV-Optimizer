"""Unit tests for the EV controller (pure decision logic)."""
from __future__ import annotations

import pytest

from custom_components.pv_optimizer.ev_controller import (
    EVStateClass,
    classify_state,
    DEFAULT_STATE_VOCAB,
)


def test_classify_disconnected_default_substrings() -> None:
    assert classify_state("Disconnected") == EVStateClass.DISCONNECTED
    assert classify_state("Unplugged") == EVStateClass.DISCONNECTED


def test_classify_bare_idle_falls_back_to_idle() -> None:
    """'idle' is intentionally not a DISCONNECTED token so 'charging_idle'
    / 'connected_idle' aren't mis-routed. A bare 'idle' lands on the
    conservative CONNECTED_IDLE fallback.
    """
    assert classify_state("idle") == EVStateClass.CONNECTED_IDLE


def test_classify_connected_idle_compound_states() -> None:
    """Firmwares that emit 'connected_idle' should classify as IDLE
    (matches the 'connect' substring before the IDLE-fallback branch).
    """
    assert classify_state("connected_idle") == EVStateClass.CONNECTED_IDLE


def test_classify_connected_requesting_default_substrings() -> None:
    assert classify_state("Charging") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("wait_sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait time") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait start") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("WAIT RFID") == EVStateClass.CONNECTED_REQUESTING


def test_classify_evcs_waiting_for_substrings() -> None:
    """The EVCS HACS integration spells gated states as 'waiting_for_*'."""
    assert classify_state("waiting_for_sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("WAITING_FOR_START") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("waiting_for_rfid") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("waiting_for_time") == EVStateClass.CONNECTED_REQUESTING


def test_classify_connected_idle_default_substrings() -> None:
    assert classify_state("Charged") == EVStateClass.CONNECTED_IDLE
    assert classify_state("Connected") == EVStateClass.CONNECTED_IDLE


def test_classify_low_soc_is_idle() -> None:
    """EVCS reports low_soc when home-battery preservation pauses charging.

    It does NOT signal car-side request, so we treat it as IDLE and let the
    LP plan or planner-manual mode decide whether to override the EVCS.
    """
    assert classify_state("low_soc") == EVStateClass.CONNECTED_IDLE
    assert classify_state("LOW_SOC") == EVStateClass.CONNECTED_IDLE


def test_classify_unknown_falls_back_to_connected_idle() -> None:
    """Conservative default: unknown plugged-in classifies safely."""
    assert classify_state("WeirdStatus") == EVStateClass.CONNECTED_IDLE


def test_classify_handles_none_and_unavailable() -> None:
    assert classify_state(None) == EVStateClass.DISCONNECTED
    assert classify_state("unknown") == EVStateClass.DISCONNECTED
    assert classify_state("unavailable") == EVStateClass.DISCONNECTED


def test_classify_precedence_disconnected_wins_over_requesting() -> None:
    """If a state somehow contains both a DISCONNECTED and a REQUESTING
    substring, DISCONNECTED takes precedence per §3.3."""
    # Pathological compound — pick disconnected on tie.
    assert classify_state("unplugged charging") == EVStateClass.DISCONNECTED


def test_classify_custom_vocab_override() -> None:
    custom = {
        EVStateClass.DISCONNECTED: ("frei",),
        EVStateClass.CONNECTED_REQUESTING: ("laedt",),
        EVStateClass.CONNECTED_IDLE: ("voll",),
    }
    assert classify_state("Frei", vocab=custom) == EVStateClass.DISCONNECTED
    assert classify_state("Laedt", vocab=custom) == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("voll", vocab=custom) == EVStateClass.CONNECTED_IDLE


# ---------------------------------------------------------------------------
# Task 6: ReactiveDecision / decide_reactive
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import (
    ReactiveDecision,
    decide_reactive,
)
from custom_components.pv_optimizer.models import EVParams


def _ev() -> EVParams:
    return EVParams(
        max_charging_power_kw=8.0,
        max_charging_current_a=20.0,
        min_charging_current_a=6.0,
        car_battery_kwh=60.0,
        buy_price_threshold=0.0,
    )


def test_reactive_disconnected_writes_zero() -> None:
    out = decide_reactive(
        state_class=EVStateClass.DISCONNECTED,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=0.50,
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_requesting_no_surplus_no_cheap_grid_writes_zero() -> None:
    """REQUESTING is not a short-circuit: with no surplus and an
    above-threshold price, the function returns 0 even while the car
    is asking — auto mode honours the LP / surplus constraint rather
    than the car's request."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=5000.0,         # importing — no surplus
        ev_charging_power_w=0.0,
        price_buy=0.50,              # not cheap
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_requesting_with_surplus_tracks_surplus() -> None:
    """REQUESTING state falls through to surplus math — no short-circuit."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=-3000.0,        # 3 kW surplus
        ev_charging_power_w=0.0,
        price_buy=0.50,
        ev=_ev(),
    )
    # kw_per_amp = 0.4 -> 3 kW / 0.4 = 7.5 -> truncate to 7.
    assert out.max_current_a == 7


def test_reactive_requesting_with_cheap_grid_grants_max() -> None:
    """Cheap-grid still wins for REQUESTING too — it's price-driven, not request-driven."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=5000.0,
        ev_charging_power_w=0.0,
        price_buy=-0.05,             # below threshold
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_cheap_grid_grants_max() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=-0.05,  # below threshold of 0
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_surplus_tracking_above_min() -> None:
    """grid=-3kW (exporting 3kW) + ev=0 -> 3kW surplus -> 7.5A clamps to 6A floor? No 3000/400=7.5A."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-3000.0,
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    # kw_per_amp = 8/20 = 0.4. surplus = 3 kW. target_a = 3/0.4 = 7.5 -> rounded.
    assert out.max_current_a == 7


def test_reactive_surplus_back_adds_current_ev_power() -> None:
    """Convergence: when EV is already drawing, back-add so loop doesn't ramp down."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,          # net zero — EV consumes all surplus
        ev_charging_power_w=3000.0,
        price_buy=0.30,
        ev=_ev(),
    )
    # available = -0 + 3 = 3 kW -> 7A, stable.
    assert out.max_current_a == 7


def test_reactive_surplus_below_min_writes_zero() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-1000.0,  # 1 kW surplus -> 2.5A, below min 6A
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_surplus_above_max_clamps() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-20000.0,  # huge surplus
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_cheap_grid_threshold_inclusive() -> None:
    """price_buy <= threshold triggers cheap-grid; default threshold is 0."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=0.0,  # equal -> trigger
        ev=_ev(),
    )
    assert out.max_current_a == 20


# ---------------------------------------------------------------------------
# Task 7: LatchState / update_latches
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import (
    LatchState,
    update_latches,
)


def test_latches_start_clear() -> None:
    s = LatchState()
    assert not s.cheap_grid
    assert not s.ultimate_override


def test_cheap_grid_latch_trigger_and_release() -> None:
    ev = _ev()
    s = LatchState()
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=-0.05, ev_charging_power_w=0.0,
        time_in_current_class_s=0.0,
        ev=ev,
    )
    assert s.cheap_grid
    # Same conditions next tick — still latched.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=-0.05, ev_charging_power_w=0.0,
        time_in_current_class_s=300.0,
        ev=ev,
    )
    assert s.cheap_grid
    # Price now above threshold for one tick — latch releases (≥1 tick rule).
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=0.05, ev_charging_power_w=0.0,
        time_in_current_class_s=600.0,
        ev=ev,
    )
    assert not s.cheap_grid


def test_ultimate_override_latch_triggers_on_denied_request() -> None:
    ev = _ev()
    s = LatchState()
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=0.0,
        time_in_current_class_s=10.0,
        ev=ev,
    )
    assert s.ultimate_override


def test_ultimate_override_latch_does_not_flap_during_charge() -> None:
    """Once latched and car is drawing power, state stays REQUESTING — latch holds.

    This is the bug we explicitly designed around (spec §4.1 last paragraph).
    """
    ev = _ev()
    s = update_latches(
        LatchState(),
        state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=0.0,
        time_in_current_class_s=10.0,
        ev=ev,
    )
    assert s.ultimate_override
    # Next tick — car now drawing 6 kW; state still REQUESTING; latch must hold.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=6000.0,
        time_in_current_class_s=300.0,
        ev=ev,
    )
    assert s.ultimate_override


def test_ultimate_override_releases_after_dwell_out_of_requesting() -> None:
    ev = _ev()
    s = update_latches(
        LatchState(),
        state_class=EVStateClass.CONNECTED_REQUESTING,
        price_buy=0.50, ev_charging_power_w=0.0,
        time_in_current_class_s=10.0,
        ev=ev,
    )
    assert s.ultimate_override
    # State leaves REQUESTING; not yet past the dwell window.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=0.50, ev_charging_power_w=0.0,
        time_in_current_class_s=30.0,  # < session_done_seconds (60)
        ev=ev,
    )
    assert s.ultimate_override
    # Past the dwell — release.
    s = update_latches(
        s, state_class=EVStateClass.CONNECTED_IDLE,
        price_buy=0.50, ev_charging_power_w=0.0,
        time_in_current_class_s=120.0,
        ev=ev,
    )
    assert not s.ultimate_override


# ---------------------------------------------------------------------------
# Task 8: is_session_done
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import is_session_done


def test_session_done_when_disconnected() -> None:
    ev = _ev()
    assert is_session_done(
        state_class=EVStateClass.DISCONNECTED,
        ev_charging_power_w=0.0, low_power_seconds=0.0, ev=ev,
    )


def test_session_done_when_idle_and_low_power_long_enough() -> None:
    ev = _ev()
    assert is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=50.0, low_power_seconds=120.0, ev=ev,
    )


def test_session_not_done_when_idle_but_brief() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=50.0, low_power_seconds=30.0, ev=ev,
    )


def test_session_not_done_when_idle_but_drawing_power() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=2000.0, low_power_seconds=600.0, ev=ev,
    )


def test_session_not_done_when_still_requesting() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev_charging_power_w=0.0, low_power_seconds=600.0, ev=ev,
    )


# ---------------------------------------------------------------------------
# Task 9: translate_lp_slot0
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import translate_lp_slot0


def test_translate_disconnected_yields_zero() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.DISCONNECTED,
        ev=ev,
    ) == 0


def test_translate_lp_zero_yields_zero() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 0


def test_translate_lp_positive_above_min_converts_to_amps() -> None:
    ev = _ev()
    # 4 kW / 0.4 kw/A = 10 A.
    assert translate_lp_slot0(
        p_ev_chg_kw=4.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 10


def test_translate_lp_below_min_clamps_up_to_floor() -> None:
    """Contrast with reactive: LP path clamps up because user committed to a target."""
    ev = _ev()
    # 1 kW / 0.4 = 2.5 A < 6 A floor -> clamp UP.
    assert translate_lp_slot0(
        p_ev_chg_kw=1.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 6


def test_translate_lp_above_max_clamps_down() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=100.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 20


def test_translate_lp_zero_yields_zero_even_when_requesting() -> None:
    """LP plan of 0 yields 0 A regardless of car state — the user's plan
    is authoritative, not the car's request signal."""
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev=ev,
    ) == 0


def test_translate_lp_positive_when_requesting_uses_lp_value() -> None:
    """REQUESTING with a positive LP plan: current is derived from the
    LP-planned watts, not from the charger's max."""
    ev = _ev()
    # 4 kW / 0.4 kw/A = 10 A.
    assert translate_lp_slot0(
        p_ev_chg_kw=4.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev=ev,
    ) == 10


def test_car_auto_return_switch_class_shape() -> None:
    """Smoke test: switch platform imports cleanly and defaults to off."""
    import sys
    from unittest.mock import MagicMock

    # Stub out HA dependencies so the module can be imported without a full
    # HA install in the test environment.  RestoreEntity and SwitchEntity must
    # be distinct classes so Python doesn't reject the MRO.
    class _FakeSwitchEntity: ...
    class _FakeRestoreEntity: ...

    _ha_stubs = {
        "homeassistant": MagicMock(),
        "homeassistant.components": MagicMock(),
        "homeassistant.components.switch": MagicMock(SwitchEntity=_FakeSwitchEntity),
        "homeassistant.config_entries": MagicMock(),
        "homeassistant.core": MagicMock(),
        "homeassistant.helpers": MagicMock(),
        "homeassistant.helpers.entity_platform": MagicMock(),
        "homeassistant.helpers.restore_state": MagicMock(RestoreEntity=_FakeRestoreEntity),
    }
    _inserted = {k for k in _ha_stubs if k not in sys.modules}
    sys.modules.update(_ha_stubs)
    # Remove any previously-cached version of the module under test.
    sys.modules.pop("custom_components.pv_optimizer.switch", None)
    try:
        from custom_components.pv_optimizer.switch import _EVCarAutoReturnSwitch
        s = _EVCarAutoReturnSwitch("entry-id-1")
        assert s.entity_id == "switch.pv_optimizer_ev_car_auto_return"
        assert s._attr_unique_id == "entry-id-1_ev_car_auto_return"
        assert s.is_on is False
    finally:
        for k in _inserted:
            sys.modules.pop(k, None)
        sys.modules.pop("custom_components.pv_optimizer.switch", None)
