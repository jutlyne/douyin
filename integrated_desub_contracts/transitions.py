from __future__ import annotations

from types import MappingProxyType
from typing import Final

from .enums import (
    FAILURE_STATES,
    MAX_CLEAN_REPAIR_ATTEMPTS,
    MAX_COMPACT_RETRIES,
    NONTERMINAL_STATES,
    STATE_SET,
    TERMINAL_STATES,
)


class InvalidTransition(ValueError):
    """Raised when a requested v1 state transition is not in the closed graph."""


_GENERIC_TERMINALS: Final = frozenset({"failed", "cancelled"})

_PRIMARY_TRANSITIONS = {
    "queued": {"downloading"},
    "downloading": {"detecting"},
    "detecting": {"inpainting"},
    "inpainting": {"encoding_clean"},
    "encoding_clean": {"verifying_clean_machine"},
    "verifying_clean_machine": {
        "verifying_clean_agents",
        "repairing_clean",
        "clean_qa_failed",
    },
    "verifying_clean_agents": {
        "clean_approved",
        "repairing_clean",
        "clean_qa_failed",
    },
    "repairing_clean": {"detecting"},
    "clean_approved": {"aligning_speech"},
    "aligning_speech": {"translating", "translation_failed"},
    "translating": {"verifying_translation", "translation_failed"},
    "verifying_translation": {"synthesizing_tts", "translation_failed"},
    "compacting_translation": {"verifying_translation", "translation_failed"},
    "synthesizing_tts": {"scheduling_tts", "tts_failed"},
    "scheduling_tts": {
        "compacting_translation",
        "rendering_vietsub",
        "scheduling_failed",
    },
    "rendering_vietsub": {"mixing_audio", "subtitle_failed"},
    "mixing_audio": {"verifying_dub_machine"},
    "verifying_dub_machine": {"verifying_dub_agents", "dub_qa_failed"},
    "verifying_dub_agents": {"signing_release", "dub_qa_failed"},
    "signing_release": {"completed", "dub_qa_failed"},
}

_closed_graph: dict[str, frozenset[str]] = {}
for _state in NONTERMINAL_STATES:
    _closed_graph[_state] = frozenset(
        _PRIMARY_TRANSITIONS.get(_state, set()) | _GENERIC_TERMINALS
    )
for _state in TERMINAL_STATES:
    _closed_graph[_state] = frozenset()

TRANSITIONS: Final = MappingProxyType(_closed_graph)


def allowed_transitions(state: str) -> frozenset[str]:
    if state not in STATE_SET:
        raise InvalidTransition(f"unknown state: {state!r}")
    return TRANSITIONS[state]


def validate_transition(
    current: str,
    target: str,
    *,
    compact_retries_used: int | None = None,
    clean_repair_attempts_used: int | None = None,
) -> None:
    if current not in STATE_SET:
        raise InvalidTransition(f"unknown current state: {current!r}")
    if target not in STATE_SET:
        raise InvalidTransition(f"unknown target state: {target!r}")
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"transition {current!r} -> {target!r} is not allowed")
    if target == "repairing_clean":
        if isinstance(clean_repair_attempts_used, bool) or not isinstance(
            clean_repair_attempts_used, int
        ):
            raise InvalidTransition(
                "clean_repair_attempts_used must be an integer"
            )
        if not 0 <= clean_repair_attempts_used < MAX_CLEAN_REPAIR_ATTEMPTS:
            raise InvalidTransition("automatic clean repair budget is exhausted")
    if current == "scheduling_tts" and target == "compacting_translation":
        if isinstance(compact_retries_used, bool) or not isinstance(
            compact_retries_used, int
        ):
            raise InvalidTransition("compact_retries_used must be an integer")
        if not 0 <= compact_retries_used < MAX_COMPACT_RETRIES:
            raise InvalidTransition("translation compact retry budget is exhausted")


def is_terminal(state: str) -> bool:
    if state not in STATE_SET:
        raise InvalidTransition(f"unknown state: {state!r}")
    return state in TERMINAL_STATES


def assert_closed_graph() -> None:
    if set(TRANSITIONS) != STATE_SET:
        raise AssertionError("transition graph does not cover the frozen state set")
    for state, targets in TRANSITIONS.items():
        unknown = set(targets) - STATE_SET
        if unknown:
            raise AssertionError(f"{state!r} has unknown targets: {sorted(unknown)!r}")
    for state in TERMINAL_STATES:
        if TRANSITIONS[state]:
            raise AssertionError(f"terminal state {state!r} has outgoing transitions")
    if set(FAILURE_STATES) - STATE_SET:
        raise AssertionError("failure state set is inconsistent")
