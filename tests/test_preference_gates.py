"""Policy-operation gate qualification (preferences-configuration:T004.2/T004.3/T004).

Every load-bearing claim is proven through the ACTUAL wired entrypoint -- the
`/api/policy-operations/*` router built by ``create_app`` over an injected
:class:`PolicyGateService` -- not a hand-built object the runtime never invokes.
The service-level tests that remain (concurrency, lock-disable detection) exercise
the exact composed spine the router calls.

Criteria -> tests
  T004.2#1 preview != perform ...... test_preview_is_distinct_from_perform_and_mutates_nothing
  T004.2#2 approval fail-closed .... test_apply_without_approval_fails_closed_and_preserves_value,
                                     test_changed_payload_approval_fails_closed,
                                     test_expired_approval_fails_closed,
                                     test_cross_actor_approval_fails_closed,
                                     test_cross_project_approval_fails_closed,
                                     test_a_consumed_approval_cannot_be_replayed_on_a_fresh_run
  T004.2#3 hub-local atomic/no lease test_hub_local_commit_is_atomic_and_needs_no_bridge_lease
  T004.2#4 external read-only ...... test_external_provider_operation_is_read_only_unless_declared
  T004.2#5 effective-after-receipt . test_effective_value_changes_only_after_a_successful_receipt
  T004.3#1 receipt shape ........... test_receipt_carries_id_type_hash_scope_outcome_and_timestamps
  T004.3#2 fail preserves value .... test_failed_hub_local_commit_preserves_value_unambiguously,
                                     test_unknown_external_outcome_reconciles_exactly_once
  T004.3#3 retry keeps identity .... test_reconciliation_retry_reuses_identity_and_consumes_no_new_approval
  T004.3#4 browser-safe redaction .. test_status_apis_scrub_the_full_proven_leak_corpus
  T004#1   no change before success  test_policy_form_cannot_change_config_before_the_operation_succeeds
  T004#2   replay/expiry/cross ..... (shared with T004.2#2)
  T004#4   never touches Serving/State test_default_profile_selection_stays_hub_local_and_never_mutates_serving
  closure  closed bodies ........... test_closed_body_refuses_undeclared_fields, test_request_refuses_bad_scope_or_kind
  atomicity concurrency ............ test_concurrent_apply_of_one_identity_commits_exactly_once,
                                     test_disabling_the_receipt_lock_breaks_exactly_once_then_restores
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.contracts import validate_operation_receipt
from workbench.graph import NullGraph
from workbench.preference_gates import (
    ExternalPolicyDeclaration,
    HUB_POLICY_PROVIDER,
    PolicyGateError,
    PolicyGateService,
    PolicyOperationRequest,
)
from workbench.store import (
    MemoryOperationApprovalStore,
    MemoryOperationReceiptStore,
    MemoryPreferenceStore,
    MemoryStore,
    OperationOutcome,
    UnknownOutcomeError,
)

_ROOT = Path(__file__).resolve().parents[1]
_RETENTION = "policy.transcript_retention_max_days"
_ROUTE_PROFILE = "policy.route_allowlist_profile"
_PROJECT_TEMPLATE = "project.workflow_template"
_OWNER = {"X-Workbench-Actor": "operator"}
_OTHER = {"X-Workbench-Actor": "reviewer"}


def _catalog() -> dict:
    return json.loads(
        (_ROOT / "docs" / "contracts" / "examples" / "settings-descriptor.v1.json").read_text(
            encoding="utf-8"
        )
    )


def _settings() -> Settings:
    return Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://serving", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )


def _client(service: PolicyGateService | None) -> TestClient:
    return TestClient(create_app(
        settings=_settings(), store=MemoryStore(), graph=NullGraph(), policy_gate_service=service,
    ))


def _retention_set(value: int = 120, op_version: int = 1) -> dict:
    return {
        "setting_id": _RETENTION, "scope": "policy", "operation": "preference.set",
        "op_version": op_version, "value": value,
    }


def _grant_for(service: PolicyGateService, body: dict, actor: str, grant_id: str, *, ttl_seconds: int = 300) -> None:
    """Mint the out-of-band human approval bound to exactly this operation."""
    request = PolicyOperationRequest(
        setting_id=body["setting_id"], scope=body["scope"], operation=body["operation"],
        op_version=body["op_version"], value=body.get("value"), project_id=body.get("project_id"),
        provider=body.get("provider", HUB_POLICY_PROVIDER),
    )
    binding = service.approval_binding(request, actor)
    service.approvals.grant(
        grant_id, binding["action"], binding["payload_hash"], binding["actor"], binding["scope_key"],
        ttl_seconds=ttl_seconds,
    )


def _stored_version(service: PolicyGateService, setting_id: str, scope: str = "policy", scope_key: str = "policy") -> int:
    return service.preferences.current_version(scope, scope_key, setting_id)


# ---------------------------------------------------------------------------
# T004.2 #1 -- previewing/requesting is distinct from performing
# ---------------------------------------------------------------------------


def test_preview_is_distinct_from_perform_and_mutates_nothing():
    service = PolicyGateService(_catalog())
    client = _client(service)
    body = _retention_set()

    preview = client.post("/api/policy-operations/preview", headers=_OWNER, json=body)
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["requires_approval"] is True
    # A preview shares the applied operation's canonical digest (an approval bound
    # to it commits to exactly this effect)...
    binding = client.post("/api/policy-operations/approval-binding", headers=_OWNER, json=body).json()
    assert payload["preview"]["digest"] == binding["payload_hash"]
    # ...and touches no store: no value committed, no receipt recorded.
    assert _stored_version(service, _RETENTION) == 0
    assert service.receipt(payload["idempotency_key"]) is None


# ---------------------------------------------------------------------------
# T004.2 #2 / T004 #2 -- replay / expiry / changed / cross-actor / cross-project
# approvals fail closed BEFORE any effect
# ---------------------------------------------------------------------------


def test_apply_without_approval_fails_closed_and_preserves_value():
    service = PolicyGateService(_catalog())
    client = _client(service)
    resp = client.post("/api/policy-operations/apply", headers=_OWNER, json={**_retention_set(), "grant_id": "absent"})
    assert resp.status_code == 200
    receipt = resp.json()["receipt"]
    assert receipt["status"] == "denied"
    assert receipt["error"]["code"] == "approval.invalid"
    assert _stored_version(service, _RETENTION) == 0  # prior value untouched


def test_changed_payload_approval_fails_closed():
    # An approval binds the FULL payload hash; applying a DIFFERENT value with a
    # grant bound to another value is a payload-hash mismatch -> fail closed.
    service = PolicyGateService(_catalog())
    client = _client(service)
    _grant_for(service, _retention_set(value=120), "operator", "g")
    resp = client.post(
        "/api/policy-operations/apply", headers=_OWNER, json={**_retention_set(value=90), "grant_id": "g"},
    )
    receipt = resp.json()["receipt"]
    assert receipt["status"] == "denied" and receipt["error"]["code"] == "approval.invalid"
    assert _stored_version(service, _RETENTION) == 0


def test_expired_approval_fails_closed():
    service = PolicyGateService(_catalog())
    client = _client(service)
    _grant_for(service, _retention_set(), "operator", "g", ttl_seconds=-1)
    resp = client.post("/api/policy-operations/apply", headers=_OWNER, json={**_retention_set(), "grant_id": "g"})
    receipt = resp.json()["receipt"]
    assert receipt["status"] == "denied" and receipt["error"]["code"] == "approval.invalid"
    assert _stored_version(service, _RETENTION) == 0


def test_cross_actor_approval_fails_closed():
    # A grant bound to 'operator' cannot be consumed by 'reviewer'.
    service = PolicyGateService(_catalog())
    client = _client(service)
    _grant_for(service, _retention_set(), "operator", "g")
    resp = client.post("/api/policy-operations/apply", headers=_OTHER, json={**_retention_set(), "grant_id": "g"})
    receipt = resp.json()["receipt"]
    assert receipt["status"] == "denied" and receipt["error"]["code"] == "approval.invalid"
    assert _stored_version(service, _RETENTION) == 0


def test_cross_project_approval_fails_closed():
    # A grant bound to project 'proj-a' cannot commit a change scoped to 'proj-b'.
    service = PolicyGateService(_catalog())
    client = _client(service)
    body_a = {
        "setting_id": _PROJECT_TEMPLATE, "scope": "project", "operation": "preference.set",
        "op_version": 1, "value": "workflow.delivery-standard", "project_id": "proj-a",
    }
    _grant_for(service, body_a, "operator", "g")
    body_b = {**body_a, "project_id": "proj-b"}
    resp = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body_b, "grant_id": "g"})
    receipt = resp.json()["receipt"]
    assert receipt["status"] == "denied" and receipt["error"]["code"] == "approval.invalid"
    assert _stored_version(service, _PROJECT_TEMPLATE, "project", "proj-b") == 0
    assert _stored_version(service, _PROJECT_TEMPLATE, "project", "proj-a") == 0


def test_a_consumed_approval_cannot_be_replayed_on_a_fresh_run():
    # The one-time consumption outlives the idempotency store: after a success, a
    # NEW run (fresh receipt store, shared approval store) cannot reuse the grant.
    catalog = _catalog()
    approvals = MemoryOperationApprovalStore()
    preferences = MemoryPreferenceStore(catalog)
    service = PolicyGateService(catalog, preference_store=preferences, approval_store=approvals)
    _grant_for(service, _retention_set(), "operator", "g")
    first, replayed = service.apply(PolicyOperationRequest(**_retention_set()), actor="operator", grant_id="g")
    assert first["status"] == "succeeded" and replayed is False

    fresh_run = PolicyGateService(
        catalog, preference_store=preferences, approval_store=approvals,
        receipt_store=MemoryOperationReceiptStore(),
    )
    second, _ = fresh_run.apply(PolicyOperationRequest(**_retention_set()), actor="operator", grant_id="g")
    assert second["status"] == "denied" and second["error"]["code"] == "approval.invalid"
    # The committed value from the first success is intact (one effect only).
    assert preferences.get("policy", "policy", _RETENTION).value == 120


# ---------------------------------------------------------------------------
# T004.2 #3 -- hub-local changes commit atomically, never need a bridge lease
# ---------------------------------------------------------------------------


def test_hub_local_commit_is_atomic_and_needs_no_bridge_lease():
    service = PolicyGateService(_catalog())
    client = _client(service)
    body = _retention_set(value=120)
    _grant_for(service, body, "operator", "g")
    resp = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"})
    receipt = resp.json()["receipt"]
    assert receipt["status"] == "succeeded"
    assert service.preferences.get("policy", "policy", _RETENTION).value == 120
    # Structural proof there is no bridge/worktree-lease path in the hub-local
    # commit: the gate module never imports or invokes a lease/preflight primitive
    # (the docstring names "worktree lease" only to explain it is NOT consulted).
    source = (_ROOT / "workbench" / "preference_gates.py").read_text(encoding="utf-8")
    for forbidden in ("OperationLeaseState", "preflight_operation_command", "lease_authority", "from .bridge"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# T004.2 #4 / T004 #4 -- external-provider policy is read-only unless declared
# ---------------------------------------------------------------------------


def _serving_route_body() -> dict:
    return {
        "setting_id": _ROUTE_PROFILE, "scope": "policy", "operation": "preference.set",
        "op_version": 1, "value": "sha256:" + "c" * 64, "provider": "anvil-serving",
    }


def test_external_provider_operation_is_read_only_unless_declared():
    # Undeclared: a Serving route/profile policy change returns a truthful
    # read-only result and never mutates the hub's stored value.
    service = PolicyGateService(_catalog())
    client = _client(service)
    body = _serving_route_body()
    _grant_for(service, body, "operator", "g")
    resp = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"})
    receipt = resp.json()["receipt"]
    assert receipt["status"] == "denied"
    assert receipt["error"]["code"] == "policy.external_read_only"
    assert _stored_version(service, _ROUTE_PROFILE) == 0

    # Declared: the owning provider explicitly allows the exact operation, so it
    # dispatches to that provider's adapter (here a hermetic success).
    def adapter(op):
        return OperationOutcome("succeeded", external_ref={"route": "serving_route1"})

    declared = PolicyGateService(
        _catalog(),
        external_declarations=(ExternalPolicyDeclaration("anvil-serving", _ROUTE_PROFILE, "preference.set", adapter),),
    )
    client2 = _client(declared)
    _grant_for(declared, body, "operator", "g2")
    ok = client2.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g2"}).json()["receipt"]
    assert ok["status"] == "succeeded"


# ---------------------------------------------------------------------------
# T004.2 #5 / T004 #1 -- effective config changes ONLY after a success receipt
# ---------------------------------------------------------------------------


def test_effective_value_changes_only_after_a_successful_receipt():
    service = PolicyGateService(_catalog())
    client = _client(service)
    body = _retention_set(value=200)  # in-bounds (max 365)

    # Preview and a denied apply both leave the value at its default (unset).
    client.post("/api/policy-operations/preview", headers=_OWNER, json=body)
    denied = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "nope"}).json()
    assert denied["receipt"]["status"] == "denied"
    assert _stored_version(service, _RETENTION) == 0

    # Only the matching successful receipt flips the effective value.
    _grant_for(service, body, "operator", "g")
    ok = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"}).json()
    assert ok["receipt"]["status"] == "succeeded"
    assert service.preferences.get("policy", "policy", _RETENTION).value == 200


def test_policy_form_cannot_change_config_before_the_operation_succeeds():
    # T004 #1 restated end to end: the value is default until (and only until) the
    # owner-specific operation records a success.
    service = PolicyGateService(_catalog())
    client = _client(service)
    body = _retention_set(value=120)
    _grant_for(service, body, "operator", "g")
    before = client.post("/api/policy-operations/preview", headers=_OWNER, json=body).json()
    assert before["hub_local"] is True
    assert _stored_version(service, _RETENTION) == 0
    client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"})
    assert service.preferences.get("policy", "policy", _RETENTION).value == 120


# ---------------------------------------------------------------------------
# T004.3 #1 -- receipt shape (id, type, payload hash, scope, outcome, timestamps)
# ---------------------------------------------------------------------------


def test_receipt_carries_id_type_hash_scope_outcome_and_timestamps():
    service = PolicyGateService(_catalog())
    client = _client(service)
    body = _retention_set(value=120)
    _grant_for(service, body, "operator", "g")
    receipt = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"}).json()["receipt"]

    validate_operation_receipt(receipt)  # schema-valid, redacted, closed field set
    assert receipt["receipt_id"].startswith("rcpt_")           # receipt ID
    assert receipt["operation"]["id"] == _RETENTION            # operation type
    binding = client.post("/api/policy-operations/approval-binding", headers=_OWNER, json=body).json()
    assert receipt["operation"]["operation_digest"] == binding["payload_hash"]  # payload hash
    assert receipt["external_ref"]["scope"] == "policy"        # actor/project scope
    assert receipt["status"] == "succeeded"                    # outcome
    for stamp in (receipt["started_at"], receipt["finished_at"]):  # safe RFC3339 timestamps
        assert stamp.endswith("Z") and "T" in stamp


# ---------------------------------------------------------------------------
# T004.3 #2 -- a failure preserves the prior value; one reconciliation for unknown
# ---------------------------------------------------------------------------


def test_failed_hub_local_commit_preserves_value_unambiguously():
    # A stale version (the stored value moved) is an unambiguous failure that
    # preserves the prior value and stays retriable -- never a fabricated success.
    service = PolicyGateService(_catalog())
    service.preferences.seed_authority_value("policy", _RETENTION, 90)  # stored version -> 1
    client = _client(service)
    body = _retention_set(value=120, op_version=1)  # expects version 0, but it is 1
    _grant_for(service, body, "operator", "g")
    receipt = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"}).json()["receipt"]
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "policy.stale_version"
    assert receipt["error"]["retryable"] is True
    assert service.preferences.get("policy", "policy", _RETENTION).value == 90  # preserved
    # A failed attempt is not persisted under its key -> it stays retriable.
    key = service._idempotency_key("policy", service._build_operation(PolicyOperationRequest(**body)))
    assert service.receipt(key) is None


def _unknown_adapter(summary: str, external_ref: dict):
    def adapter(op):
        raise UnknownOutcomeError(summary, external_ref=external_ref, reason="provider_failure")

    return adapter


def test_unknown_external_outcome_reconciles_exactly_once():
    body = _serving_route_body()
    declared = PolicyGateService(
        _catalog(),
        external_declarations=(
            ExternalPolicyDeclaration(
                "anvil-serving", _ROUTE_PROFILE, "preference.set",
                _unknown_adapter("serving apply outcome unknown", {"route": "serving_r1"}),
            ),
        ),
    )
    client = _client(declared)
    _grant_for(declared, body, "operator", "g")
    receipt = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"}).json()["receipt"]
    assert receipt["status"] == "reconciliation_required"
    recs = client.get("/api/policy-operations/reconciliations", headers=_OWNER).json()["reconciliations"]
    assert len(recs) == 1 and recs[0]["reason"] == "provider_failure"
    # The hub's stored value is untouched: an unknown external effect is never
    # optimistically committed hub-side.
    assert _stored_version(declared, _ROUTE_PROFILE) == 0


# ---------------------------------------------------------------------------
# T004.3 #3 -- reconciliation retries reuse the original identity, no new approval
# ---------------------------------------------------------------------------


def test_reconciliation_retry_reuses_identity_and_consumes_no_new_approval():
    body = _serving_route_body()
    declared = PolicyGateService(
        _catalog(),
        external_declarations=(
            ExternalPolicyDeclaration(
                "anvil-serving", _ROUTE_PROFILE, "preference.set",
                _unknown_adapter("serving apply outcome unknown", {"route": "serving_r1"}),
            ),
        ),
    )
    _grant_for(declared, body, "operator", "g")
    request = PolicyOperationRequest(**body)
    first, replayed_first = declared.apply(request, actor="operator", grant_id="g")
    assert first["status"] == "reconciliation_required" and replayed_first is False
    consumed_at = declared.approvals.grants["g"].consumed_at
    assert consumed_at is not None  # consumed exactly once

    # A retry of the SAME operation identity replays the stored reconciliation
    # receipt; it does not run the executor, mint a reconciliation, or re-consume.
    second, replayed_second = declared.apply(request, actor="operator", grant_id="g")
    assert replayed_second is True
    assert second["receipt_id"] == first["receipt_id"]
    assert len(declared.reconciliations()) == 1
    assert declared.approvals.grants["g"].consumed_at == consumed_at


# ---------------------------------------------------------------------------
# T004.3 #4 -- browser-safe status APIs expose no credential / path / raw output
# ---------------------------------------------------------------------------

#: The proven-leak corpus (shared with the security-contract lens): every shape
#: an adversarial gate proved slips a raw channel, INCLUDING the dotless
#: single-label host:port ``serving:8443`` the shared redact_config_text now
#: catches. None may survive into a served receipt or reconciliation record.
_LEAK_CORPUS = [
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
    "-----BEGIN RSA PRIVATE KEY-----",
    "serving:8443",
    "internal.db.corp:9200",
    "100.64.0.5:8443",
    "/etc/anvil/secrets.yaml",
    "postgresql://user:pw@db:5432/anvil",
    "sk-proj-ABCDEFGH12345678",
    "ghp_ABCDEFGHIJKLMNOP0123456789",
    "C:" + chr(92) + "Users" + chr(92) + "admin",
    "serving.tail1234.ts.net",
]


def test_status_apis_scrub_the_full_proven_leak_corpus():
    corpus_summary = "provider push failed: " + " ".join(_LEAK_CORPUS)
    body = _serving_route_body()
    declared = PolicyGateService(
        _catalog(),
        external_declarations=(
            ExternalPolicyDeclaration(
                "anvil-serving", _ROUTE_PROFILE, "preference.set",
                # external_ref values seed the corpus items whose SHAPE the opaque
                # token map admits; the free-text summary carries the whole corpus.
                _unknown_adapter(corpus_summary, {"akia": "AKIAIOSFODNN7EXAMPLE", "host": "serving:8443"}),
            ),
        ),
    )
    client = _client(declared)
    _grant_for(declared, body, "operator", "g")
    key = declared._idempotency_key("policy", declared._build_operation(PolicyOperationRequest(**body)))

    apply_body = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"}).text
    recs_body = client.get("/api/policy-operations/reconciliations", headers=_OWNER).text
    receipt_body = client.get("/api/policy-operations/receipts/" + key, headers=_OWNER).text

    for served in (apply_body, recs_body, receipt_body):
        for marker in _LEAK_CORPUS:
            assert marker not in served, f"leaked {marker!r} into a served policy status body"
    # Confirm the scrub actually ran (the record exists and was redacted), rather
    # than the corpus silently vanishing because the record was empty.
    assert "reconciliation" in recs_body or "safe_summary" in recs_body
    assert declared.reconciliations()[0]["reason"] == "provider_failure"


# ---------------------------------------------------------------------------
# T004 #4 -- a default-profile selection stays hub-local; never mutates Serving
# ---------------------------------------------------------------------------


def test_default_profile_selection_stays_hub_local_and_never_mutates_serving():
    # Selecting the project default capability profile is a hub-local preference
    # write; it commits to the hub store only and reaches no Serving/State path.
    service = PolicyGateService(_catalog())
    client = _client(service)
    body = {
        "setting_id": "project.default_capability_profile", "scope": "project",
        "operation": "preference.set", "op_version": 1,
        "value": "sha256:" + "d" * 64, "project_id": "proj-a",
    }
    _grant_for(service, body, "operator", "g")
    receipt = client.post("/api/policy-operations/apply", headers=_OWNER, json={**body, "grant_id": "g"}).json()["receipt"]
    assert receipt["status"] == "succeeded"
    assert service.preferences.get("project", "proj-a", "project.default_capability_profile").value == "sha256:" + "d" * 64
    # Structural: the gate holds no Serving/State client and issues no route/policy
    # mutation -- it composes only the preference/approval/receipt spine.
    source = (_ROOT / "workbench" / "preference_gates.py").read_text(encoding="utf-8")
    for forbidden in ("router", "AnvilRouter", "route_decisions", "StateReader", "state.db", "apply_acceptance"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# Closure -- closed request bodies refuse undeclared fields / invalid enums
# ---------------------------------------------------------------------------


def test_closed_body_refuses_undeclared_fields():
    service = PolicyGateService(_catalog())
    client = _client(service)
    # A smuggled command/path field cannot ride in past the closed typed body.
    resp = client.post(
        "/api/policy-operations/preview", headers=_OWNER,
        json={**_retention_set(), "command": "rm -rf /"},
    )
    assert resp.status_code == 422


def test_request_refuses_bad_scope_or_kind():
    # The typed request refuses a deployment scope and an unknown operation kind
    # before anything is built or hashed.
    with pytest.raises(PolicyGateError):
        PolicyOperationRequest(setting_id="deployment.state_read_location", scope="deployment",
                               operation="preference.set", op_version=1, value="x")
    with pytest.raises(PolicyGateError):
        PolicyOperationRequest(setting_id=_RETENTION, scope="policy", operation="preference.wipe", op_version=1)
    with pytest.raises(PolicyGateError):
        PolicyOperationRequest(setting_id=_PROJECT_TEMPLATE, scope="project",
                               operation="preference.set", op_version=1, value="workflow.x")  # no project_id


def test_unconfigured_gate_fails_closed_with_503():
    client = _client(None)
    assert client.post("/api/policy-operations/preview", headers=_OWNER, json=_retention_set()).status_code == 503
    assert client.post(
        "/api/policy-operations/apply", headers=_OWNER, json={**_retention_set(), "grant_id": "g"}
    ).status_code == 503
    assert client.get("/api/policy-operations/reconciliations", headers=_OWNER).status_code == 503


# ---------------------------------------------------------------------------
# Atomicity -- exactly-once commit under concurrency, and lock-disable detection
# ---------------------------------------------------------------------------


def test_concurrent_apply_of_one_identity_commits_exactly_once():
    # Two threads apply the SAME operation identity at once; the idempotent receipt
    # store serializes them so the effect commits exactly once and the loser
    # replays the same receipt (one committed write, never two).
    service = PolicyGateService(_catalog())
    _grant_for(service, _retention_set(), "operator", "g")
    request = PolicyOperationRequest(**_retention_set())
    results: list[tuple[dict, bool]] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(service.apply(request, actor="operator", grant_id="g"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one first-execution and one replay, both the same succeeded receipt.
    assert [replayed for _, replayed in results].count(True) == 1
    assert {receipt["receipt_id"] for receipt, _ in results} == {results[0][0]["receipt_id"]}
    assert all(receipt["status"] == "succeeded" for receipt, _ in results)
    assert service.preferences.get("policy", "policy", _RETENTION).write_version == 1  # committed once


def _raced_execution_count(*, serialized: bool) -> int:
    """Run two threads through record_attempt on ONE key with a slow executor.

    Returns how many times the executor actually ran.  This isolates the exact
    guard the gate composes: the receipt store's lock-serialized check-execute-
    store.  With the lock disabled and both threads forced to interleave inside
    the executor, the guard is absent and the effect runs twice.
    """
    from workbench.models import OperationRef

    receipts = MemoryOperationReceiptStore()
    if not serialized:
        receipts._lock = contextlib.nullcontext()  # disable serialization LOCALLY
    ref = OperationRef(
        provider="anvil-preferences", id="policy.transcript_retention_max_days",
        contract_version="1.0.0", operation_digest="sha256:" + "a" * 64,
    )
    runs = {"n": 0}
    entered = threading.Barrier(2)

    def executor() -> OperationOutcome:
        runs["n"] += 1
        # Force both threads to pass the existence check before either stores.
        with contextlib.suppress(threading.BrokenBarrierError):
            entered.wait(timeout=1.0)
        return OperationOutcome("succeeded", external_ref={"scope": "policy"})

    def worker():
        receipts.record_attempt(
            run_id="r", command_id="c", operation=ref,
            idempotency_key="policyop:policy:one", executor=executor,
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return runs["n"]


def test_disabling_the_receipt_lock_lets_the_effect_run_twice_then_restores():
    # Detection: with the exactly-once lock DISABLED both racers execute the
    # effect (>1 run); with it enabled the guard serializes them to exactly one.
    # The disabled lock is a local instance override in a fresh store, never a
    # change to the shared guard -- the serialized branch proves it is intact.
    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        assert _raced_execution_count(serialized=False) == 2   # guard absent -> double effect
        assert _raced_execution_count(serialized=True) == 1    # guard present -> exactly once
    finally:
        sys.setswitchinterval(switch)
