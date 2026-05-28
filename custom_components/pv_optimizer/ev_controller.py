"""Pure decision logic for the EV charging feature.

No Home Assistant imports. Owned by tests/test_ev_controller.py.
"""
from __future__ import annotations

import enum
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
