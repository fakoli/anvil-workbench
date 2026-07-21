"""Gap-detectable stream sequence + state-version metadata (chat-first-voice T008).

The relay (:mod:`workbench.chat_stream`, T003.2) yields typed frames and the
lifecycle store (:mod:`workbench.response_lifecycle_store`, T003.3) persists the
reconnect-safe record.  This slice adds the thin metadata that lets a
*reconnecting* client detect a dropped frame and refresh to last-committed state
without duplicating the response:

* **Frame sequencing.** Every emitted frame is stamped with a strictly-monotonic
  per-conversation ``seq`` drawn from the durable allocator
  (:meth:`~workbench.response_lifecycle_store.MemoryResponseLifecycleStore.next_seq`).
  The allocator lives in the shared row container, so ``seq`` continues strictly
  above the last committed value after a hub restart and never resets
  (criterion 1).
* **Gap detection (client-side).** A strictly-monotonic stream delivers frame
  ``last_seq + 1`` next; a frame whose ``seq`` skips past that means one or more
  frames were dropped in transit.  :func:`detect_gap` flags exactly that, and
  :func:`needs_snapshot_refresh` turns it into the resync decision.  These are
  pure functions, mirrored byte-for-byte by the browser helper
  ``web/src/chat-api.js`` so the client and the hub agree on the contract
  (criterion 2).
* **Stale-frame refusal.** A frame at or below the last seen ``seq`` is
  stale/duplicate; :func:`is_stale_frame` flags it so the client ignores it
  (never re-rendering it) and the store refuses to commit it — a stale-sequence
  frame can never regress the lifecycle nor a terminal (criterion 3, whose
  backbone is the store's terminal-immutability).

Nothing here opens a socket or persists content; it is pure sequencing metadata
over the already-hermetic relay and store.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, Iterator

from .chat_stream import RelayEvent

#: Mirror of the durable sequence ceiling; a stamped seq never exceeds it.
from .response_lifecycle_store import MAX_SEQUENCE  # noqa: F401  (re-exported bound)


def detect_gap(last_seq: int, frame_seq: int) -> bool:
    """True when ``frame_seq`` skips past the next expected frame (a drop).

    A strictly-monotonic stream delivers ``last_seq + 1`` next; anything higher
    means at least one frame was dropped between them.  This is the primitive a
    reconnecting client uses to notice a dropped frame.  Non-integer inputs are
    treated as no-gap (the caller validates types elsewhere).
    """
    if not _is_int(last_seq) or not _is_int(frame_seq):
        return False
    return frame_seq > last_seq + 1


def is_stale_frame(last_seq: int, frame_seq: int) -> bool:
    """True when ``frame_seq`` is at or below the last seen seq (stale/duplicate).

    A stale frame carries no forward progress; the client ignores it (so a
    replayed frame never duplicates the response) and the store refuses to
    commit it (so it cannot regress the lifecycle).
    """
    if not _is_int(last_seq) or not _is_int(frame_seq):
        return False
    return frame_seq <= last_seq


def needs_snapshot_refresh(last_seq: int, frame_seq: int) -> bool:
    """True when a detected gap means the client must refresh from the snapshot.

    A gap means the client missed committed frames; it must fetch the
    last-committed snapshot to resync WITHOUT the response being re-streamed or
    duplicated.  A contiguous or stale frame needs no refresh.
    """
    return detect_gap(last_seq, frame_seq)


def sequence_events(
    events: Iterable[RelayEvent], allocate: Callable[[], int]
) -> Iterator[RelayEvent]:
    """Stamp each relay frame with the next per-conversation ``seq``.

    ``allocate`` is normally bound to the store's ``next_seq`` for the owning
    response, so the deltas and the single terminal all carry a strictly-ascending
    ``seq`` a client can check for gaps.  The relay stays stateless and hermetic;
    this is a pure transform over its frames.
    """
    for event in events:
        if not isinstance(event, RelayEvent):
            raise TypeError("sequence_events requires RelayEvent frames")
        yield replace(event, seq=allocate())


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
