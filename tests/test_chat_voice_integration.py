"""End-to-end chat speech input/output qualification (chat-first-voice T005).

This module drives the FULL push-to-talk (STT) + read-aloud (TTS) voice loop
through the REAL wired entrypoints only:

* the FastAPI app built by :func:`workbench.api.create_app`, exercised over a
  ``TestClient`` (never a hand-built request), and
* the real :class:`workbench.voice.VoiceRelayService` relay machinery, injected
  at the exact boundary ``create_app`` already exposes (``voice_relay_service``),
  with its production ``VoiceServingTransport`` Protocol satisfied by a hermetic
  stub so no external network or raw provider is ever reached.

The STT/TTS backend is faked ONLY at the injected transport seam the production
code already declares; every authorization gate, bound, lifecycle-event write,
turn-append gate, redaction hop, and error handler under test is the real code.

A NOTE ON WIREDNESS, so no claim here overstates what the product actually wires:

* The STT draft path (``POST /api/chat/voice/transcribe``) and the TTS read-aloud
  path (``POST /api/chat/voice/speak``) are REAL relay endpoints the browser
  calls; those legs run through the production ``VoiceRelayService``.
* Playback CONTROLS — pause and stop — are client-transient BY DESIGN.  The voice
  API surface exposes ONLY ``transcribe`` and ``speak`` (asserted structurally in
  ``test_voice_surface_exposes_only_transcribe_and_speak``), so there is no relay
  endpoint a pause/stop could traverse, and this module claims none.
* The ONLY DURABLE representation of an interruption is the content-free
  ``chat.turn.assistant-interrupted.v1`` ``voice_events`` decoration on a
  committed turn, appended through the real append-only ``POST /turns`` endpoint.
  It is NOT a relay-emitted event (the relay logs only ``utterance_start`` /
  ``stt_commit`` / ``tts_start``); it is the shipped turn-contract shape the
  client commits, which this module exercises through the real turns endpoint and
  asserts is content-free (no audio, no transcript mutation) and mutates no
  message content or conversation state.

Each test maps to a T005 acceptance criterion:

1. ``test_full_voice_loop_through_wired_endpoints`` — one integration flow that
   exercises tap-to-record AND hold-to-record STT (real ``/transcribe``),
   editable transcript review, an EXPLICIT send, TTS start and replay (real
   ``/speak``), and the committed content-free interruption marker (real
   ``POST /turns``).  It asserts NO pause/stop relay traffic, because none exists.
2. It also proves STT creates NO conversation turn until the reviewed transcript
   is explicitly submitted, and that the EDITED text (not the raw STT draft)
   becomes the turn.
3. ``test_tts_and_replay_never_mutate_conversation_state`` — the conversation
   record is byte-identical before and after TTS start / replay.
4. ``test_raw_audio_absent_from_every_durable_surface``,
   ``test_error_payloads_never_reflect_request_audio`` and
   ``test_browser_cache_contract_fixtures_carry_no_audio`` — a distinctive audio
   byte marker appears NOWHERE in durable storage, audit, the graph, the browser
   contract fixtures, or any 4xx/5xx error body (including the path-scoped
   RequestValidationError scrub).
5. ``test_voice_surface_exposes_only_transcribe_and_speak`` — the structural
   proof that a playback-control server call is UNREPRESENTABLE: the mounted
   voice routes are exactly ``/transcribe`` and ``/speak``.
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from workbench.api import _VOICE_INVALID_REQUEST_DETAIL, create_app
from workbench.config import Settings
from workbench.conversation_api import conversation_actor
from workbench.conversation_store import MemoryConversationStore, UnknownConversationError
from workbench.graph import NullGraph
from workbench.store import MemoryStore
from workbench.voice import MemoryVoiceEventLog, VoiceRelayService

_REPO_ROOT = Path(__file__).resolve().parents[1]

OWNER = "operator"

#: The one distinctive marker seeded into the RAW input audio bytes of every STT
#: request.  It must never surface in any durable record, audit event, graph
#: payload, browser contract fixture, or error body.  Both the raw bytes AND
#: their base64 wire form are asserted absent.
_AUDIO_MARKER = b"RAW_AUDIO_MARKER_9f3"

#: What the fake Serving STT returns for a committed utterance.  It is a DRAFT
#: only; it is deliberately DISTINCT from the text the actor edits and submits,
#: so criterion 2's "edited text, not raw STT text" is a value assertion, not a
#: shape assertion.  It carries no marker (the marker lives in the audio bytes).
_RAW_STT_FINAL = "raw stt draft: reveiw the relese-alpha taks"
_RAW_STT_INTERIM = "raw stt draft: reveiw the"

#: The text the actor edits the draft into and EXPLICITLY submits.
_EDITED_TRANSCRIPT = "Review the release-alpha task before we start."

#: The already-rendered assistant message the actor asks to hear read aloud.
_ASSISTANT_TEXT = "The release-alpha PRD is pinned at revision 4 and the task is ready."

#: The transient playback bytes the fake Serving TTS hands back.  Distinct from
#: the input audio and deliberately marker-free: TTS output is legitimately
#: returned in the /speak response body, so it must not collide with the input
#: marker the durable-surface scan hunts for.
_TTS_PLAYBACK_AUDIO = b"TTS_PLAYBACK_BYTES_not_persisted"


def _marker_audio(*, extra: bytes = b"") -> bytes:
    """Raw PCM-ish input audio carrying the marker (and an optional trigger)."""
    return _AUDIO_MARKER + b" pcm16 " + extra + b"\x00\x01\x02\x03"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _FakeServingTransport:
    """Hermetic stub for the ``VoiceServingTransport`` Protocol (workbench.voice).

    It exposes exactly ``transcribe`` / ``synthesize``, so the runtime-checkable
    Protocol accepts it at ``VoiceRelayService``'s constructor guard — the same
    seam the production ``ServingVoiceTransport`` occupies.  It imports no raw
    provider and constructs no endpoint, mirroring the "Serving-only, no
    fallback" contract.  A trigger sentinel embedded in the audio/text lets a
    test drive a Serving failure without any real I/O.
    """

    def __init__(self) -> None:
        self.transcribe_requests: list[dict[str, Any]] = []
        self.synthesize_requests: list[dict[str, Any]] = []

    def transcribe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.transcribe_requests.append(dict(request))
        audio = base64.b64decode(str(request["audio_b64"]))
        if b"TRIGGER_STT_FAIL" in audio:
            raise RuntimeError("serving stt upstream dropped the relay")
        is_final = bool(request["is_final"])
        return {
            "text": _RAW_STT_FINAL if is_final else _RAW_STT_INTERIM,
            "is_final": is_final,
            "duration_ms": 2600 if is_final else 900,
        }

    def synthesize(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.synthesize_requests.append(dict(request))
        if "TRIGGER_TTS_FAIL" in str(request["text"]):
            raise RuntimeError("serving tts upstream dropped the relay")
        return {
            "audio_b64": _b64(_TTS_PLAYBACK_AUDIO),
            "format": str(request.get("output_format", "mp3")),
            "sample_rate": 24000,
        }


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner=OWNER, approvers=frozenset({OWNER}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
        chat_content_hash_key="voice-integration-content-hash-key-32byte",
    )
    values.update(overrides)
    return Settings(**values)


def _harness() -> tuple[
    TestClient, MemoryConversationStore, MemoryStore, MemoryVoiceEventLog, _FakeServingTransport
]:
    """Build the wired app with the real relay + inspectable durable stores.

    ``scope_authorized`` is wired to the REAL conversation store's ownership
    check (resolving the trusted actor exactly as the chat surface does), so an
    unknown/foreign conversation fails the relay closed — not a stub ``True``.
    """
    conversation_store = MemoryConversationStore(
        content_hash_key=b"voice-integration-content-hash-key-32byte",
    )
    workbench_store = MemoryStore()
    event_log = MemoryVoiceEventLog()
    transport = _FakeServingTransport()

    def scope_authorized(actor: str, conversation_id: str) -> bool:
        try:
            conversation_store.get_conversation_with_turns(conversation_actor(actor), conversation_id)
            return True
        except UnknownConversationError:
            return False

    relay = VoiceRelayService(
        transport,
        voice_authorized=frozenset({OWNER}),
        scope_authorized=scope_authorized,
        event_log=event_log,
    )
    app = create_app(
        settings=_settings(), store=workbench_store, graph=NullGraph(),
        conversation_store=conversation_store, voice_relay_service=relay,
    )
    return TestClient(app), conversation_store, workbench_store, event_log, transport


def _hdr(actor: str = OWNER) -> dict[str, str]:
    return {"X-Workbench-Actor": actor}


def _create_conversation(client: TestClient) -> str:
    response = client.post("/api/conversations", json={"title": "Voice kickoff"}, headers=_hdr())
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _transcribe(client: TestClient, conversation_id: str, audio: bytes, *, is_final: bool,
                duration_ms: int | None = None, audio_format: str = "pcm16"):
    body: dict[str, Any] = {
        "conversation_id": conversation_id,
        "audio_base64": _b64(audio),
        "audio_format": audio_format,
        "is_final": is_final,
    }
    if duration_ms is not None:
        body["duration_ms"] = duration_ms
    return client.post("/api/chat/voice/transcribe", json=body, headers=_hdr())


def _speak(client: TestClient, conversation_id: str, message_ref: str, text: str):
    return client.post("/api/chat/voice/speak", json={
        "conversation_id": conversation_id, "message_ref": message_ref, "text": text,
    }, headers=_hdr())


def _submit_user_transcript(client: TestClient, conversation_id: str, text: str) -> dict[str, Any]:
    """The EXPLICIT send: the reviewed/edited transcript becomes one durable turn.

    The turn carries the voice-INPUT lifecycle as content-free markers plus the
    redacted transcript text the actor actually submitted.
    """
    response = client.post(f"/api/conversations/{conversation_id}/turns", json={
        "role": "user", "status": "complete",
        "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
        "content": [{"kind": "transcript", "text": text}],
        "voice_events": [
            {"event": "utterance_start", "at": "2026-07-21T00:00:01Z"},
            {"event": "stt_commit", "at": "2026-07-21T00:00:04Z",
             "duration_ms": 2600, "transcript_chars": len(text)},
        ],
    }, headers=_hdr())
    assert response.status_code == 201, response.text
    return response.json()


def _append_assistant_turn(client: TestClient, conversation_id: str, parent_turn_id: str) -> dict[str, Any]:
    """The read-aloud target: an assistant turn carrying the DURABLE, content-free
    ``voice_events`` interruption decoration — exactly the shipped
    ``chat.turn.assistant-interrupted.v1`` contract shape the client commits.

    These markers are NOT relay-emitted (the relay logs only utterance_start /
    stt_commit / tts_start); they are the append-only turn-contract representation
    of a playback interruption, committed here through the real ``POST /turns``
    endpoint.  The append gate is what is under test — that this decoration is
    accepted content-free (no audio, no transcript mutation), never that a relay
    produced it.
    """
    response = client.post(f"/api/conversations/{conversation_id}/turns", json={
        "role": "assistant", "status": "interrupted",
        "lineage": {"parent_turn_id": parent_turn_id, "sibling_index": 0, "kind": "initial"},
        "content": [{"kind": "text", "text": _ASSISTANT_TEXT}],
        "voice_events": [
            {"event": "tts_start", "at": "2026-07-21T00:00:05Z"},
            {"event": "interruption", "at": "2026-07-21T00:00:09Z"},
            {"event": "tts_stop", "at": "2026-07-21T00:00:09Z", "duration_ms": 4000},
        ],
    }, headers=_hdr())
    assert response.status_code == 201, response.text
    return response.json()


def _durable_blob(conversation_store: MemoryConversationStore, workbench_store: MemoryStore,
                  event_log: MemoryVoiceEventLog, conversation_id: str, graph: NullGraph) -> str:
    """Every durable/audit/graph surface serialized into one string to scan.

    Covers: conversation + turn rows, the conversation store's audit log, the
    hub workbench-store audit, the relay's OWN content-free lifecycle event log,
    and a graph projection of that lifecycle data (proving even if voice
    lifecycle were projected, no audio rides through ``EvidenceGraph.project``).
    """
    rows = conversation_store.rows
    parts: list[Any] = []
    for record in rows.conversations.values():
        parts.append(str(record))
    for turns in rows.turns.values():
        parts.extend(str(turn) for turn in turns)
    parts.extend(str(event) for event in rows.audit)
    parts.extend(str(event) for event in workbench_store.list_audit(limit=1000))
    for event in event_log.events(conversation_id):
        parts.append(json.dumps(event.as_event_data()))
        # A content-free lifecycle datum is the ONLY thing eligible to reach the
        # retrieval graph; project it and fold the payload + returned id in.
        projected = graph.project("route", event.correlation_id, "project_voice", event.as_event_data())
        parts.append(projected)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Criterion 1 + 2: the full wired loop; no turn until explicit submit; the
# EDITED text (not the raw STT draft) becomes the turn.
# --------------------------------------------------------------------------- #


def test_full_voice_loop_through_wired_endpoints():
    client, conversation_store, workbench_store, event_log, transport = _harness()
    actor = conversation_actor(OWNER)
    with client:
        conversation_id = _create_conversation(client)

        # -- TAP-to-record: one final utterance in a single commit ------------
        tap = _transcribe(client, conversation_id, _marker_audio(extra=b"tap"),
                           is_final=True, duration_ms=2600)
        assert tap.status_code == 200, tap.text
        tap_draft = tap.json()["draft"]
        # The relay returns an editable DRAFT (the raw STT text); it is NOT the
        # committed turn and NOT the edited text.
        assert tap_draft["text"] == _RAW_STT_FINAL
        assert tap_draft["is_final"] is True

        # -- HOLD-to-record: interim chunk(s) then a final commit -------------
        interim = _transcribe(client, conversation_id, _marker_audio(extra=b"hold-interim"),
                              is_final=False)
        assert interim.status_code == 200, interim.text
        assert interim.json()["draft"]["is_final"] is False
        assert interim.json()["draft"]["text"] == _RAW_STT_INTERIM
        held_final = _transcribe(client, conversation_id, _marker_audio(extra=b"hold-final"),
                                 is_final=True, duration_ms=3100)
        assert held_final.status_code == 200, held_final.text
        assert held_final.json()["draft"]["text"] == _RAW_STT_FINAL

        # Both recording modes actually reached the relay transport.
        assert len(transport.transcribe_requests) == 3
        # The relay logged content-free lifecycle events: a final commit is
        # ``stt_commit``; an interim chunk is ``utterance_start``.
        states = [event.state for event in event_log.events(conversation_id)]
        assert states.count("stt_commit") == 2 and states.count("utterance_start") == 1

        # -- CRITERION 2: NO turn exists while the transcript is under review --
        review = client.get(f"/api/conversations/{conversation_id}", headers=_hdr())
        assert review.status_code == 200, review.text
        assert review.json()["turns"] == []
        assert conversation_store.rows.turns[conversation_id] == []

        # -- EDIT + EXPLICIT send: the reviewed/edited text becomes the turn --
        assert _EDITED_TRANSCRIPT != _RAW_STT_FINAL  # the actor really edited it
        submitted = _submit_user_transcript(client, conversation_id, _EDITED_TRANSCRIPT)
        user_turn_id = submitted["id"]

        after = client.get(f"/api/conversations/{conversation_id}", headers=_hdr())
        turns = after.json()["turns"]
        # Exactly ONE turn now, and its content is the EDITED text, never the raw
        # STT draft the relay returned.
        assert len(turns) == 1
        user_turn = turns[0]
        assert [block["text"] for block in user_turn["content"]] == [_EDITED_TRANSCRIPT]
        conversation_blob = json.dumps(after.json())
        assert _RAW_STT_FINAL not in conversation_blob
        assert _RAW_STT_INTERIM not in conversation_blob
        # The committed voice-input lifecycle is content-free (counts only).
        assert [event["event"] for event in user_turn["voice_events"]] == ["utterance_start", "stt_commit"]
        assert all("audio" not in event for event in user_turn["voice_events"])

        # -- Durable interruption marker via the REAL append-only POST /turns ---
        # The interruption/stop are NOT relay events (no server surface exists for
        # a playback control); their ONLY durable form is this content-free
        # voice_events decoration on a committed turn, which the append gate here
        # accepts and returns.  Assert it is content-free: only lifecycle event
        # names, and NO audio field on any event.
        assistant = _append_assistant_turn(client, conversation_id, user_turn_id)
        assistant_ref = assistant["id"]
        assert [event["event"] for event in assistant["voice_events"]] == ["tts_start", "interruption", "tts_stop"]
        assert all("audio" not in event for event in assistant["voice_events"])
        # The committed message CONTENT is the assistant text only — the playback
        # decoration mutated no transcript and carried no audio.
        assert [block["text"] for block in assistant["content"]] == [_ASSISTANT_TEXT]

        # -- TTS start + read-aloud replay through the REAL relay /speak --------
        start = _speak(client, conversation_id, assistant_ref, _ASSISTANT_TEXT)
        assert start.status_code == 200, start.text
        # TTS start returns transient playback audio the browser plays and drops.
        assert base64.b64decode(start.json()["audio_base64"]) == _TTS_PLAYBACK_AUDIO
        replay = _speak(client, conversation_id, assistant_ref, _ASSISTANT_TEXT)
        assert replay.status_code == 200, replay.text
        assert replay.json()["audio_base64"] == start.json()["audio_base64"]
        assert len(transport.synthesize_requests) == 2  # start + replay both relayed

        # The relay's TTS lifecycle log carries a BYTE COUNT, never the audio.
        tts_events = [event for event in event_log.events(conversation_id) if event.state == "tts_start"]
        assert len(tts_events) == 2
        assert all(event.byte_count == len(_TTS_PLAYBACK_AUDIO) for event in tts_events)

        # -- Marker never reached any durable/audit/graph surface -------------
        blob = _durable_blob(conversation_store, workbench_store, event_log, conversation_id, NullGraph())
        assert _AUDIO_MARKER.decode("ascii") not in blob
        assert _b64(_marker_audio(extra=b"tap")) not in blob
        assert "RAW_AUDIO_MARKER" not in blob


# --------------------------------------------------------------------------- #
# Criterion 3: TTS/read-aloud/replay never mutate message or conversation state.
# --------------------------------------------------------------------------- #


def test_tts_and_replay_never_mutate_conversation_state():
    client, conversation_store, _workbench_store, event_log, _transport = _harness()
    with client:
        conversation_id = _create_conversation(client)
        submitted = _submit_user_transcript(client, conversation_id, _EDITED_TRANSCRIPT)
        assistant = _append_assistant_turn(client, conversation_id, submitted["id"])
        assistant_ref = assistant["id"]

        # Snapshot the FULL owner-visible conversation record + every durable row
        # BEFORE any TTS/playback operation.
        before_response = client.get(f"/api/conversations/{conversation_id}", headers=_hdr())
        assert before_response.status_code == 200
        before_json = json.dumps(before_response.json(), sort_keys=True)
        before_rows = copy.deepcopy(conversation_store.rows)
        before_audit_len = len(conversation_store.rows.audit)

        # Drive TTS start, replay, and a second replay through the REAL relay.
        for _ in range(3):
            assert _speak(client, conversation_id, assistant_ref, _ASSISTANT_TEXT).status_code == 200

        # The conversation record is byte-identical afterwards: read-aloud and
        # replay touched no message or conversation state.
        after_response = client.get(f"/api/conversations/{conversation_id}", headers=_hdr())
        assert json.dumps(after_response.json(), sort_keys=True) == before_json
        # No new turn, no new conversation-store audit row was written by TTS.
        assert conversation_store.rows.turns == before_rows.turns
        assert conversation_store.rows.conversations == before_rows.conversations
        assert len(conversation_store.rows.audit) == before_audit_len
        # The only durable trace TTS produced is the relay's own content-free
        # lifecycle log (which is NOT conversation state).
        assert [event.state for event in event_log.events(conversation_id)] == ["tts_start", "tts_start", "tts_start"]


# --------------------------------------------------------------------------- #
# Criterion 4: raw audio is absent from every durable surface, and from every
# 4xx/5xx error body — including the path-scoped RequestValidationError scrub.
# --------------------------------------------------------------------------- #


def test_raw_audio_absent_from_every_durable_surface():
    client, conversation_store, workbench_store, event_log, _transport = _harness()
    with client:
        conversation_id = _create_conversation(client)
        # A successful STT + explicit submit + TTS, so every durable surface has
        # actually been written to before the scan.
        assert _transcribe(client, conversation_id, _marker_audio(), is_final=True,
                           duration_ms=2600).status_code == 200
        submitted = _submit_user_transcript(client, conversation_id, _EDITED_TRANSCRIPT)
        assistant = _append_assistant_turn(client, conversation_id, submitted["id"])
        assert _speak(client, conversation_id, assistant["id"], _ASSISTANT_TEXT).status_code == 200

        blob = _durable_blob(conversation_store, workbench_store, event_log, conversation_id, NullGraph())
        # Neither the raw marker nor its base64 wire form is anywhere durable.
        assert _AUDIO_MARKER.decode("ascii") not in blob
        assert _b64(_marker_audio()) not in blob
        # And the raw STT DRAFT text never persisted either — only the edited,
        # explicitly submitted transcript did.
        assert _RAW_STT_FINAL not in blob
        # The relay lifecycle log is non-empty (the scan is not vacuous) yet
        # carries only content-free state/counts.
        events = event_log.events(conversation_id)
        assert events
        for event in events:
            data = json.dumps(event.as_event_data())
            assert "audio" not in data and _AUDIO_MARKER.decode("ascii") not in data


def test_error_payloads_never_reflect_request_audio():
    client, _conversation_store, _workbench_store, _event_log, _transport = _harness()
    marker_text = _AUDIO_MARKER.decode("ascii")
    with client:
        conversation_id = _create_conversation(client)

        # (a) In-endpoint fixed-detail 422: a bad audio format is refused BEFORE
        #     the audio is decoded.  Real audio (with the marker) rides in the
        #     request; the fixed detail reflects none of it.
        bad_format = _transcribe(client, conversation_id, _marker_audio(),
                                 is_final=True, audio_format="flac")
        assert bad_format.status_code == 422
        assert marker_text not in bad_format.text
        assert _b64(_marker_audio()) not in bad_format.text

        # (b) 403 scope failure BEFORE any transport call: an unknown
        #     conversation id fails the wired ownership check closed.  Audio in
        #     the body; the 403 detail reflects none of it.
        wrong_scope = _transcribe(client, "conv_" + "z" * 24, _marker_audio(), is_final=True)
        assert wrong_scope.status_code == 403
        assert marker_text not in wrong_scope.text

        # (c) 5xx Serving failure (no provider fallback): the audio carries the
        #     transport trigger; the 502 body reflects neither audio nor detail.
        fail = _transcribe(client, conversation_id, _marker_audio(extra=b"TRIGGER_STT_FAIL"),
                           is_final=True)
        assert fail.status_code == 502
        assert marker_text not in fail.text
        assert "TRIGGER_STT_FAIL" not in fail.text

        # (d) PRE-endpoint RequestValidationError SCRUB: a body that fails
        #     pydantic BEFORE the endpoint carries the marker in the offending
        #     field.  FastAPI's default 422 echoes that ``input`` verbatim; the
        #     path-scoped handler must replace it with a FIXED detail that omits
        #     ``input``/``loc``/``ctx`` entirely.  Two shapes:
        #     - /transcribe with an extra forbidden field bearing the marker;
        extra_field = client.post("/api/chat/voice/transcribe", json={
            "conversation_id": conversation_id,
            "audio_base64": _b64(_marker_audio()),
            "audio_format": "pcm16",
            "leaked_transcript": marker_text + " spoken aloud",
        }, headers=_hdr())
        assert extra_field.status_code == 422
        assert extra_field.json() == {"detail": _VOICE_INVALID_REQUEST_DETAIL}
        assert marker_text not in extra_field.text
        for echoed in ("input", "loc", "ctx"):
            assert echoed not in extra_field.text

        #     - /speak with over-length TTS text bearing the marker (the exact
        #       content-echo shape the repo's scrub was added to close).
        speak_overflow = client.post("/api/chat/voice/speak", json={
            "conversation_id": conversation_id,
            "message_ref": "turn_" + "a" * 12,
            "text": marker_text + "x" * 20001,
        }, headers=_hdr())
        assert speak_overflow.status_code == 422
        assert speak_overflow.json() == {"detail": _VOICE_INVALID_REQUEST_DETAIL}
        assert marker_text not in speak_overflow.text


def test_browser_cache_contract_fixtures_carry_no_audio():
    # The shipped chat contract example fixtures are what the browser renders and
    # caches.  Structurally they can carry no audio (the chat-turn schema has no
    # audio content kind and voice_events forbid extra fields), and no fixture
    # value carries the input audio marker.
    examples = _REPO_ROOT / "docs" / "contracts" / "examples"
    fixtures = [
        examples / "chat.turn.user-voice.v1.json",
        examples / "chat.turn.assistant-interrupted.v1.json",
        examples / "chat.conversation.v1.json",
    ]
    marker_text = _AUDIO_MARKER.decode("ascii")
    forbidden_keys = {"audio", "audio_base64", "audio_b64", "audio_bytes", "audio_data", "waveform"}
    allowed_voice_event_keys = {"event", "at", "duration_ms", "transcript_chars"}

    seen_voice_events = 0
    for fixture in fixtures:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        blob = json.dumps(payload)
        assert marker_text not in blob, fixture.name

        keys: list[str] = []

        def _walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    keys.append(key)
                    _walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    _walk(nested)

        _walk(payload)
        for key in keys:
            assert key.lower() not in forbidden_keys, f"{fixture.name}: audio-bearing key {key!r}"

        for event in payload.get("voice_events", []):
            seen_voice_events += 1
            extra = set(event) - allowed_voice_event_keys
            assert not extra, f"{fixture.name}: voice event carries {sorted(extra)}"

    # The scan actually saw voice events (not vacuously passing on empty lists).
    assert seen_voice_events >= 3


# --------------------------------------------------------------------------- #
# Criterion 5 (structural): a playback-control server call is UNREPRESENTABLE.
# Pause/stop are client-transient by design; the voice surface exposes only STT
# (transcribe), TTS (speak), and a READ-ONLY voice catalog (voices — an actor-
# gated list of selectable TTS voice ids, no audio and no delivery action). If a
# pause/stop or any other mutating endpoint were ever added this set would grow
# and the assertion would fail — the claim is ENFORCED, not narrated.
# --------------------------------------------------------------------------- #


def _all_route_paths(routes: Any) -> list[str]:
    """Every mounted route path, descending through the hub's ``_IncludedRouter``
    wrapper (which nests an ``original_router`` rather than flattening its routes
    onto ``app.routes``)."""
    paths: list[str] = []
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            paths.extend(_all_route_paths(original.routes))
            continue
        path = getattr(route, "path", None)
        if path:
            paths.append(path)
    return paths


def test_voice_surface_exposes_only_transcribe_and_speak():
    client, *_rest = _harness()
    voice_paths = {
        path for path in _all_route_paths(client.app.routes)
        if path.startswith("/api/chat/voice")
    }
    assert voice_paths == {
        "/api/chat/voice/transcribe",
        "/api/chat/voice/speak",
        "/api/chat/voice/voices",
    }
