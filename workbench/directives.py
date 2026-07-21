"""Typed delivery semantics for operator directives (plan-task-delivery T008).

An operator directive is an out-of-band steer an operator adds to a session.
This module is a thin, typed wrapper over the EXISTING append-only
``operator.directive`` session-event mechanism (``store.record_session_event``):
it adds no new bridge command and no new bridge effect.  A directive can never
signal, interrupt, or retarget an active Codex process — the only thing this
wrapper does is append a hub-durable session event and read them back — so its
delivery is deferred to the next work packet the bridge assembles for the
session, never pushed into a running process.

Every submission maps to one stable typed outcome code (:data:`DIRECTIVE_OUTCOMES`).
The directive text is scrubbed with :func:`workbench.redaction.redact_config_text`
before it is persisted, so an operator cannot smuggle a secret, endpoint, or
local path into a served/packeted record.

Pending vs. included: a directive is *pending* until a work packet is assembled
that includes it.  When a packet is assembled, the caller records a single
append-only ``operator.directive_packet`` marker naming the highest directive
sequence it carried; :func:`session_directive_view` then classifies each
directive as ``included`` (its sequence is at or below a recorded packet
high-water mark) or ``pending``.  Both the directive and the marker are ordinary
append-only session events — the classification is derived, never mutated in
place.
"""
from __future__ import annotations

from typing import Any, Protocol

from .models import WorkflowEvent
from .redaction import redact_config_text
from .store import StoreError

DIRECTIVE_KIND = "operator.directive"
DIRECTIVE_PACKET_KIND = "operator.directive_packet"

MAX_DIRECTIVE_CHARS = 8_000

#: The closed set of stable typed outcome codes a submission can produce. A
#: caller may assert against this set; a new outcome is an additive schema
#: change, never a silent string.
DIRECTIVE_OUTCOMES = frozenset(
    {
        "directive.queued_pending",
        "directive.rejected_empty",
        "directive.rejected_too_long",
        "directive.rejected_unknown_session",
    }
)


class SupportsDirectives(Protocol):
    def get_session(self, session_id: str) -> Any: ...
    def record_session_event(
        self, session_id: str, workflow_id: str | None, kind: str, data: dict[str, Any]
    ) -> WorkflowEvent: ...
    def list_workflow_events(self, session_id: str, after_sequence: int = 0) -> list[WorkflowEvent]: ...


def submit_directive(
    store: SupportsDirectives,
    session_id: str,
    text: Any,
    actor: str,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Append one operator directive as a session event; return a typed outcome.

    The outcome is one of :data:`DIRECTIVE_OUTCOMES`.  On success the directive
    text is scrubbed and stored as an append-only ``operator.directive`` event
    with the fixed pending-delivery semantics, and the outcome is
    ``directive.queued_pending`` — it is queued for the NEXT work packet, never
    delivered to a running process.  An unknown session, an empty directive, or
    an over-length directive is refused with its stable code and appends nothing.
    """
    # An unknown session is a typed refusal, not a raised error: the wrapper
    # never signals into a process, and it never leaks whether a session exists
    # beyond a plain not-recorded outcome.
    try:
        store.get_session(session_id)
    except StoreError:
        return {"outcome": "directive.rejected_unknown_session", "recorded": False}

    if not isinstance(text, str):
        return {"outcome": "directive.rejected_empty", "recorded": False}
    scrubbed = redact_config_text(text).strip()
    if not scrubbed:
        return {"outcome": "directive.rejected_empty", "recorded": False}
    if len(scrubbed) > MAX_DIRECTIVE_CHARS:
        return {"outcome": "directive.rejected_too_long", "recorded": False}

    event = store.record_session_event(
        session_id,
        workflow_id,
        DIRECTIVE_KIND,
        {
            "content": scrubbed,
            "actor": actor,
            # The fixed, honest delivery semantics: a directive is deferred to the
            # next packet and cannot reach a running Codex process.
            "delivery": "queued for the next bridge work packet for this session",
        },
    )
    return {"outcome": "directive.queued_pending", "recorded": True, "event": event}


def record_packet_inclusion(
    store: SupportsDirectives,
    session_id: str,
    included_up_to_sequence: int,
    workflow_id: str | None = None,
) -> WorkflowEvent:
    """Record that a work packet carried directives up to ``included_up_to_sequence``.

    This is an ordinary append-only session event (``operator.directive_packet``),
    not a bridge command or effect. It is the marker :func:`session_directive_view`
    uses to distinguish directives already carried in a queued packet from those
    still pending.
    """
    if not isinstance(included_up_to_sequence, int) or included_up_to_sequence < 0:
        raise StoreError("packet inclusion requires a non-negative directive sequence high-water mark")
    return store.record_session_event(
        session_id,
        workflow_id,
        DIRECTIVE_PACKET_KIND,
        {"included_up_to_sequence": included_up_to_sequence},
    )


def session_directive_view(store: SupportsDirectives, session_id: str) -> dict[str, Any]:
    """Return the session's directives split into ``pending`` and ``included``.

    A directive whose sequence is at or below the highest recorded packet
    high-water mark was carried in a queued packet (``included``); every later
    directive is still ``pending``.  The classification is derived from the
    append-only event log, never stored as mutable state.
    """
    events = store.list_workflow_events(session_id)
    high_water = 0
    for event in events:
        if event.kind == DIRECTIVE_PACKET_KIND:
            mark = event.data.get("included_up_to_sequence")
            if isinstance(mark, int) and mark > high_water:
                high_water = mark

    pending: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    for event in events:
        if event.kind != DIRECTIVE_KIND:
            continue
        row = {
            "event_id": event.id,
            "sequence": event.sequence,
            "content": event.data.get("content", ""),
            "actor": event.data.get("actor"),
        }
        (included if event.sequence <= high_water else pending).append(row)

    return {
        "session_id": session_id,
        "included_up_to_sequence": high_water,
        "pending": pending,
        "included": included,
    }
