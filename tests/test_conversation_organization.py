"""Conversation ORGANIZATION metadata: pin, tags, folder, filterable search.

Each test group maps to an acceptance criterion of chat-first-voice:T011:

1. Organization metadata (pin/tags/folder) is ABSENT from every assembled
   Serving request and turn-content path — it lives on the conversation record
   only, never in a built request or a turn's content.
2. Filters return only the owning actor's matching conversations; a cross-actor
   filter probe (actor B filtering by actor A's tag/folder) leaks nothing and
   is no existence oracle.
3. Pin, tag, and folder mutations are audited as safe lifecycle metadata
   WITHOUT content: the audit stream carries the lifecycle change ``kind`` over
   the content-free ``ConversationAudit`` shape and no title/message text.

Plus the bounding surface: unsafe tags/folders fail closed, the tag set is
bounded, and the store's reentrant lock serializes concurrent tag mutations so
no add is lost.

Hermetic: no socket is opened; the Serving transport in the criterion-1 request
path is the pure ``build_bounded_request`` assembler over a validated route
selection.
"""
from __future__ import annotations

import json
import sys
import threading

import pytest

from workbench.chat_routes import discover_chat_routes, validate_chat_route_selection
from workbench.chat_stream import build_bounded_request
from workbench.conversation_models import (
    MAX_TAGS,
    ContentBlock,
    ConversationActor,
    ConversationAudit,
    TurnAudit,
    TurnLineage,
    TurnRedaction,
)
from workbench.conversation_store import (
    ConversationStoreError,
    MemoryConversationStore,
    UnknownConversationError,
)
from workbench.conversation_models import RetentionPolicy

ALICE = ConversationActor("operator_alice")
BOB = ConversationActor("operator_bob")
REDACTED = TurnRedaction("redacted", "workbench.default")
KEY = b"org-test-content-hash-key-A-0001"

# Distinctive organization-value markers: if any of these strings ever appears
# in a Serving request or a turn's content, criterion 1 is violated.
TAG_MARKER = "orgtagmarker"
FOLDER_MARKER = "orgfoldermarker"

_ROUTE_CONFIG = {
    "route_id": "chat.heavy",
    "display_name": "Heavy chat",
    "serving_contract_version": "1.2.0",
    "route_digest": "sha256:" + "b" * 64,
    "model_profile": "chat-heavy",
    "controls": ["temperature_milli", "max_output_tokens", "reasoning_effort"],
}


def retention() -> RetentionPolicy:
    return RetentionPolicy("workbench.default-90d", "retained_redacted", "retained_redacted")


def store_with_conversation(actor: ConversationActor = ALICE, title: str = "Kickoff chat"):
    store = MemoryConversationStore(content_hash_key=KEY)
    conversation = store.create_conversation(actor, retention(), title=title)
    return store, conversation


def append_root(store, actor, conversation_id, text="hello"):
    return store.append_turn(
        actor, conversation_id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", text),),
    )


# --- Criterion 1: organization metadata never enters the Serving/turn path ---


def test_organization_metadata_is_absent_from_the_assembled_serving_request():
    store, conversation = store_with_conversation()
    store.pin_conversation(ALICE, conversation.id)
    store.add_conversation_tag(ALICE, conversation.id, TAG_MARKER)
    tagged = store.set_conversation_folder(ALICE, conversation.id, FOLDER_MARKER)
    # The record really does carry the organization metadata...
    assert tagged.pinned and tagged.tags == (TAG_MARKER,) and tagged.folder == FOLDER_MARKER

    # ...yet the bounded Serving request is assembled from the validated route
    # selection plus the prompt ONLY (build_bounded_request takes no
    # conversation), so no pin/tag/folder value can reach Serving.
    discovered = discover_chat_routes([dict(_ROUTE_CONFIG)])
    selection = validate_chat_route_selection("chat.heavy", {"max_output_tokens": 256}, discovered)
    request = build_bounded_request(selection, "please summarize the plan")

    blob = json.dumps(request)
    assert TAG_MARKER not in blob and FOLDER_MARKER not in blob
    # The request carries only the validated route + controls + prompt.
    assert set(request) <= {"model", "route_id", "input", "stream", "max_output_tokens",
                            "temperature", "reasoning"}
    assert request["input"] == "please summarize the plan"


def test_organization_metadata_is_absent_from_persisted_turn_content():
    store, conversation = store_with_conversation()
    store.pin_conversation(ALICE, conversation.id)
    store.add_conversation_tag(ALICE, conversation.id, TAG_MARKER)
    store.set_conversation_folder(ALICE, conversation.id, FOLDER_MARKER)
    append_root(store, ALICE, conversation.id, text="a normal user message")

    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    for turn in turns:
        for block in turn.content:
            assert TAG_MARKER not in block.text and FOLDER_MARKER not in block.text
    # Turns structurally have no pin/tag/folder field at all.
    turn_fields = {field for turn in turns for field in turn.__dataclass_fields__}
    assert "pinned" not in turn_fields and "tags" not in turn_fields and "folder" not in turn_fields


# --- Criterion 2: filters are actor-scoped; cross-actor probes leak nothing ---


def test_filters_return_only_the_owning_actors_matching_conversations():
    store = MemoryConversationStore(content_hash_key=KEY)
    a_pinned = store.create_conversation(ALICE, retention(), title="Alice pinned")
    a_plain = store.create_conversation(ALICE, retention(), title="Alice plain")
    store.create_conversation(BOB, retention(), title="Bob plain")
    store.pin_conversation(ALICE, a_pinned.id)
    store.add_conversation_tag(ALICE, a_pinned.id, "roadmap")
    store.set_conversation_folder(ALICE, a_pinned.id, "planning")

    # Alice's own filters resolve within her rows only.
    assert [c.id for c in store.list_conversations(ALICE, pinned=True)] == [a_pinned.id]
    assert [c.id for c in store.list_conversations(ALICE, tag="roadmap")] == [a_pinned.id]
    assert [c.id for c in store.list_conversations(ALICE, folder="planning")] == [a_pinned.id]
    assert {c.id for c in store.list_conversations(ALICE, pinned=False)} == {a_plain.id}
    # Pinned sorts ahead of the rest.
    assert [c.id for c in store.list_conversations(ALICE)] == [a_pinned.id, a_plain.id]

    # Search combines a title match with the organization filters, still scoped.
    assert [c.id for c in store.search_conversations(ALICE, "alice", tag="roadmap")] == [a_pinned.id]
    assert store.search_conversations(ALICE, "alice", folder="planning")[0].id == a_pinned.id


def test_cross_actor_filter_probe_leaks_nothing_and_is_no_oracle():
    store = MemoryConversationStore(content_hash_key=KEY)
    a_secret = store.create_conversation(ALICE, retention(), title="Alice roadmap")
    store.pin_conversation(ALICE, a_secret.id)
    store.add_conversation_tag(ALICE, a_secret.id, "roadmap")
    store.set_conversation_folder(ALICE, a_secret.id, "planning")

    # Bob filtering by Alice's exact tag/folder/pin returns EMPTY — no foreign
    # conversation surfaces, and a matching filter is indistinguishable from a
    # non-matching one (both empty), so it is no existence oracle.
    assert store.list_conversations(BOB, pinned=True) == []
    assert store.list_conversations(BOB, tag="roadmap") == []
    assert store.list_conversations(BOB, folder="planning") == []
    assert store.list_conversations(BOB, tag="does-not-exist") == []
    assert store.search_conversations(BOB, "roadmap", tag="roadmap") == []

    # And Bob cannot mutate Alice's organization metadata: the same indistinct
    # "unknown conversation" a missing id raises.
    for probe in (
        lambda: store.pin_conversation(BOB, a_secret.id),
        lambda: store.add_conversation_tag(BOB, a_secret.id, "hijack"),
        lambda: store.set_conversation_folder(BOB, a_secret.id, "hijack"),
    ):
        with pytest.raises(UnknownConversationError, match="unknown conversation"):
            probe()
    # Alice's metadata is untouched by the probes.
    refreshed = store.get_conversation(ALICE, a_secret.id)
    assert refreshed.pinned and refreshed.tags == ("roadmap",) and refreshed.folder == "planning"


# --- Criterion 3: mutations are audited as content-free lifecycle metadata ---


def test_organization_mutations_are_audited_content_free_with_lifecycle_kind():
    store, conversation = store_with_conversation(ALICE, title="Secret project title")
    append_root(store, ALICE, conversation.id, "secret message body")
    store.pin_conversation(ALICE, conversation.id)
    store.add_conversation_tag(ALICE, conversation.id, "roadmap")
    store.set_conversation_folder(ALICE, conversation.id, "planning")
    store.unpin_conversation(ALICE, conversation.id)
    store.remove_conversation_tag(ALICE, conversation.id, "roadmap")
    store.clear_conversation_folder(ALICE, conversation.id)

    events = store.list_audit(limit=50)
    kinds = [event.kind for event in events]
    # Every organization lifecycle change is recorded under a distinct kind.
    for expected in (
        "conversation.pinned", "conversation.tagged", "conversation.foldered",
        "conversation.unpinned", "conversation.untagged", "conversation.unfoldered",
    ):
        assert expected in kinds

    # Every audit record is a content-free shape, and no title/message content
    # (nor the safe labels) appears anywhere in the audit stream.
    for event in events:
        assert isinstance(event.record, (ConversationAudit, TurnAudit))
    dumped = repr(events).lower()
    assert "secret" not in dumped
    assert "message body" not in dumped
    assert "project title" not in dumped


# --- Bounding surface: safe tokens, bounded set, lock discipline ------------


def test_unsafe_or_over_count_organization_labels_fail_closed():
    store, conversation = store_with_conversation()
    for bad in ("Not A Token", "has space", "UPPER", "path/like", "at@sign", ""):
        with pytest.raises(ConversationStoreError):
            store.add_conversation_tag(ALICE, conversation.id, bad)
        with pytest.raises(ConversationStoreError):
            store.set_conversation_folder(ALICE, conversation.id, bad)

    # The tag set is bounded: the 33rd distinct tag is refused.
    for index in range(MAX_TAGS):
        store.add_conversation_tag(ALICE, conversation.id, f"tag-{index:04d}")
    with pytest.raises(ConversationStoreError, match="more than"):
        store.add_conversation_tag(ALICE, conversation.id, "one-too-many")
    assert len(store.get_conversation(ALICE, conversation.id).tags) == MAX_TAGS


def test_adding_a_tag_is_deduplicated_and_removing_is_idempotent():
    store, conversation = store_with_conversation()
    store.add_conversation_tag(ALICE, conversation.id, "roadmap")
    store.add_conversation_tag(ALICE, conversation.id, "roadmap")  # duplicate
    assert store.get_conversation(ALICE, conversation.id).tags == ("roadmap",)
    store.remove_conversation_tag(ALICE, conversation.id, "absent")  # idempotent no-op
    assert store.get_conversation(ALICE, conversation.id).tags == ("roadmap",)
    store.remove_conversation_tag(ALICE, conversation.id, "roadmap")
    assert store.get_conversation(ALICE, conversation.id).tags == ()


def test_concurrent_tag_adds_are_serialized_and_none_are_lost():
    store, conversation = store_with_conversation()
    # Force aggressive thread switching so a lost-update race would be real:
    # without the store lock the read-modify-write of the tag tuple would drop
    # concurrent adds; with the reentrant lock the union is exact.
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    tags = [f"tag-{index:04d}" for index in range(MAX_TAGS)]
    barrier = threading.Barrier(len(tags))

    def add(tag: str) -> None:
        barrier.wait()
        store.add_conversation_tag(ALICE, conversation.id, tag)

    try:
        threads = [threading.Thread(target=add, args=(tag,)) for tag in tags]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous_interval)

    assert store.get_conversation(ALICE, conversation.id).tags == tuple(sorted(tags))


def test_deleting_a_conversation_drops_its_organization_metadata_from_the_tombstone():
    store, conversation = store_with_conversation()
    store.pin_conversation(ALICE, conversation.id)
    store.add_conversation_tag(ALICE, conversation.id, "roadmap")
    store.set_conversation_folder(ALICE, conversation.id, "planning")
    store.delete_conversation(ALICE, conversation.id, "purge_content_keep_tombstone")

    tombstone = store.get_conversation(ALICE, conversation.id)
    assert tombstone.status == "deleted"
    assert not tombstone.pinned and tombstone.tags == () and tombstone.folder is None


# --- Thin API endpoints: mutations, filtered list, and actor scoping --------

from fastapi.testclient import TestClient  # noqa: E402

from workbench.api import create_app  # noqa: E402
from workbench.config import Settings  # noqa: E402
from workbench.graph import NullGraph  # noqa: E402
from workbench.store import MemoryStore  # noqa: E402

_OWNER = "operator"
_OTHER = "reviewer"


def _api_client() -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner=_OWNER, approvers=frozenset({_OWNER, _OTHER}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
        chat_content_hash_key="api-org-test-content-hash-key-32b",
    )
    conversation_store = MemoryConversationStore(content_hash_key=b"api-org-test-content-hash-key-32b")
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(),
        conversation_store=conversation_store,
    ))


def _headers(actor: str) -> dict[str, str]:
    return {"X-Workbench-Actor": actor}


def _create(client: TestClient, actor: str, title: str) -> str:
    response = client.post("/api/conversations", json={"title": title}, headers=_headers(actor))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_api_organization_mutations_return_the_updated_projection():
    with _api_client() as client:
        conversation_id = _create(client, _OWNER, "Kickoff")
        pinned = client.post(f"/api/conversations/{conversation_id}/pin", headers=_headers(_OWNER))
        assert pinned.status_code == 200 and pinned.json()["pinned"] is True
        tagged = client.post(
            f"/api/conversations/{conversation_id}/tags", json={"tag": "roadmap"}, headers=_headers(_OWNER),
        )
        assert tagged.json()["tags"] == ["roadmap"]
        foldered = client.post(
            f"/api/conversations/{conversation_id}/folder", json={"folder": "planning"}, headers=_headers(_OWNER),
        )
        assert foldered.json()["folder"] == "planning"
        # An unsafe tag is refused at the edge (422), never reaching the store.
        bad = client.post(
            f"/api/conversations/{conversation_id}/tags", json={"tag": "Not A Token"}, headers=_headers(_OWNER),
        )
        assert bad.status_code == 422


def test_api_filtered_list_is_actor_scoped_and_cross_actor_probes_return_nothing():
    with _api_client() as client:
        owner_conv = _create(client, _OWNER, "Owner roadmap")
        _create(client, _OWNER, "Owner plain")
        client.post(f"/api/conversations/{owner_conv}/pin", headers=_headers(_OWNER))
        client.post(f"/api/conversations/{owner_conv}/tags", json={"tag": "roadmap"}, headers=_headers(_OWNER))
        client.post(f"/api/conversations/{owner_conv}/folder", json={"folder": "planning"}, headers=_headers(_OWNER))

        pinned = client.get("/api/conversations", params={"pinned": True}, headers=_headers(_OWNER))
        assert [c["id"] for c in pinned.json()["conversations"]] == [owner_conv]
        tagged = client.get("/api/conversations", params={"tag": "roadmap"}, headers=_headers(_OWNER))
        assert [c["id"] for c in tagged.json()["conversations"]] == [owner_conv]

        # Another actor filtering by the owner's exact tag/folder/pin sees nothing.
        for params in ({"pinned": True}, {"tag": "roadmap"}, {"folder": "planning"}):
            probe = client.get("/api/conversations", params=params, headers=_headers(_OTHER))
            assert probe.json()["conversations"] == []

        # And cannot mutate the owner's conversation: byte-equal 404 of a missing id.
        foreign = client.post(f"/api/conversations/{owner_conv}/pin", headers=_headers(_OTHER))
        missing = client.post("/api/conversations/conv_does_not_exist/pin", headers=_headers(_OTHER))
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json() == {"detail": "unknown conversation"}
