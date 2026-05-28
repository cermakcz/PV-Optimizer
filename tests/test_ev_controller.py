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
    assert classify_state("idle") == EVStateClass.DISCONNECTED
    assert classify_state("Unplugged") == EVStateClass.DISCONNECTED


def test_classify_connected_requesting_default_substrings() -> None:
    assert classify_state("Charging") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("wait_sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait time") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait start") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("WAIT RFID") == EVStateClass.CONNECTED_REQUESTING


def test_classify_connected_idle_default_substrings() -> None:
    assert classify_state("Charged") == EVStateClass.CONNECTED_IDLE
    assert classify_state("Connected") == EVStateClass.CONNECTED_IDLE


def test_classify_unknown_falls_back_to_connected_idle() -> None:
    """Conservative default: unknown plugged-in classifies safely."""
    assert classify_state("WeirdStatus") == EVStateClass.CONNECTED_IDLE


def test_classify_handles_none_and_unavailable() -> None:
    assert classify_state(None) == EVStateClass.DISCONNECTED
    assert classify_state("unknown") == EVStateClass.DISCONNECTED
    assert classify_state("unavailable") == EVStateClass.DISCONNECTED


def test_classify_precedence_disconnected_wins_over_requesting() -> None:
    """If a state somehow contains both 'idle' and 'charging' substrings,
    'disconnected' classification takes precedence per §3.3."""
    # Pathological example — pick disconnected on tie.
    assert classify_state("idle charging") == EVStateClass.DISCONNECTED


def test_classify_custom_vocab_override() -> None:
    custom = {
        EVStateClass.DISCONNECTED: ("frei",),
        EVStateClass.CONNECTED_REQUESTING: ("laedt",),
        EVStateClass.CONNECTED_IDLE: ("voll",),
    }
    assert classify_state("Frei", vocab=custom) == EVStateClass.DISCONNECTED
    assert classify_state("Laedt", vocab=custom) == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("voll", vocab=custom) == EVStateClass.CONNECTED_IDLE
