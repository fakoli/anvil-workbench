"""Approved plugin discovery and the isolated install lifecycle (reviewed-tools-plugins T002/T003).

These tests bind the T002/T003 acceptance criteria to concrete proofs over
:mod:`workbench.plugin_host`, built on the merged RTP:T001 plugin contracts:

* T002 criterion 1 — discovery is limited to exact approved entries; an
  arbitrary source is never accepted and an unknown/drifted/not-enabled entry
  fails closed on its own typed reason (``test_discovery_*``).
* T002 criterion 2 / T003 — installation resolves ONLY the plugin's own
  credential references into opaque handles; a Workbench/bridge/provider/other-
  plugin credential is structurally unreachable and a scope mismatch fails closed
  before any host effect (``test_credential_*``).
* T002 criterion 3 — replay, digest drift, host failure, and an unknown outcome
  each fail closed for their claimed reason, with reconciliation where the effect
  is in-flight (``test_lifecycle_*``, ``test_concurrent_*``).
* T003 criterion 3 — every human-readable receipt field is scrubbed; the
  adversarial credential/endpoint/path corpus never survives into a persisted or
  served receipt (``test_redaction_*``).
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from pathlib import Path

import pytest

from workbench.contracts import approval_payload_digest, contract_digest
from workbench.plugin_host import (
    CredentialBroker,
    CredentialHandle,
    HostInstallOutcome,
    MemoryPluginHostStore,
    PluginCatalogSource,
    PluginDiscovery,
    PluginHostError,
    PluginHostFailure,
    PluginHostService,
    _safe_receipt_summary,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "contracts" / "examples"

# The reviewed catalog's deploy-notifier is the host_owned-credential plugin; its
# pinned digest is the trust anchor these tests resolve against.
NOTIFIER_ID = "deploy-notifier"
NOTIFIER_DIGEST = "sha256:5474ca8eb2d41d767772c8a5ba33a1e90f5cb57017c4c8ab6487bd8ee6ba8dbb"
VIEWER_ID = "anvil-tasks-viewer"
VIEWER_DIGEST = "sha256:4ae65e4cfc645dc1adf8a742e6485946c1961819b87039ffa0d93ea88253b4fd"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _catalog() -> dict:
    return _load("plugin.catalog.v1.json")


def _capability() -> dict:
    return _load("plugin.capability.v1.json")


def _discovery() -> PluginDiscovery:
    return PluginDiscovery(_catalog(), _capability())


def _broker() -> CredentialBroker:
    # The connector host owns exactly the reference the notifier declares.
    return CredentialBroker({"anvil-connector-host": ["deploy-channel-ref"]})


def make_install_request(
    *,
    plugin_id: str = NOTIFIER_ID,
    plugin_digest: str = NOTIFIER_DIGEST,
    target_version: str = "1.0.0",
    request_id: str = "plugreq_installnotifier01",
) -> dict:
    """Build a schema-valid install request with a correct approval + digest."""
    subject = {
        "kind": "install",
        "plugin_id": plugin_id,
        "plugin_digest": plugin_digest,
        "target_version": target_version,
    }
    request = {
        "schema_version": "workbench-plugin-request/v1",
        "request_id": request_id,
        "request_digest": "sha256:" + "0" * 64,
        "kind": "install",
        "actor": {"actor_id": "operator-01", "kind": "operator"},
        "plugin": {"plugin_id": plugin_id, "plugin_digest": plugin_digest},
        "lifecycle": {"target_version": target_version},
        "approval": {
            "grant_id": "approval_installnotifier01",
            "action": "install_plugin",
            "payload_hash": approval_payload_digest(subject),
        },
        "preview_ref": {"preview_id": "plugprev_installnotifier01"},
        "created_at": "2026-07-20T12:00:00Z",
    }
    request["request_digest"] = contract_digest("plugin-request", request)
    return request


def _installed_runner(summary: str = "Installed deploy-notifier 1.0.0."):
    calls: list[tuple] = []

    def runner(discovered, handles):
        calls.append((discovered.plugin_id, tuple(h.ref for h in handles)))
        return HostInstallOutcome(status="installed", output={"installed": True}, summary=summary)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# --------------------------------------------------------------------------- #
# T002 criterion 1: discovery limited to exact approved entries.
# --------------------------------------------------------------------------- #


def test_discovery_resolves_the_exact_approved_entry() -> None:
    discovered = _discovery().resolve(NOTIFIER_ID, NOTIFIER_DIGEST)
    assert discovered.plugin_id == NOTIFIER_ID and discovered.plugin_digest == NOTIFIER_DIGEST


def test_discovery_resolves_an_enabled_tool() -> None:
    discovered = _discovery().resolve(VIEWER_ID, VIEWER_DIGEST, tool_id="tasks.list")
    assert discovered.tool is not None and discovered.tool["tool_id"] == "tasks.list"


def test_discovery_rejects_an_unknown_plugin_on_its_claimed_reason() -> None:
    with pytest.raises(PluginHostError) as exc:
        _discovery().resolve("no-such-plugin", NOTIFIER_DIGEST)
    assert exc.value.code == "unknown_plugin"


def test_discovery_fails_closed_on_digest_drift() -> None:
    # The id exists but the pinned digest differs from the reviewed catalog: the
    # refusal names the digest reason, never silently serving the current digest.
    with pytest.raises(PluginHostError) as exc:
        _discovery().resolve(NOTIFIER_ID, "sha256:" + "a" * 64)
    assert exc.value.code == "digest_drift"


def test_discovery_refuses_a_reviewed_but_not_enabled_plugin() -> None:
    # A catalog that reviews a plugin the capability profile does not enable: the
    # plugin is present at its digest but discovery still refuses it.
    catalog = _catalog()
    capability = _capability()
    # Drop deploy-notifier from the enable-only profile, keep it in the catalog.
    capability["plugins"] = [p for p in capability["plugins"] if p["plugin_id"] != NOTIFIER_ID]
    capability["digest"] = contract_digest("plugin-capability", capability)
    discovery = PluginDiscovery(catalog, capability)
    with pytest.raises(PluginHostError) as exc:
        discovery.resolve(NOTIFIER_ID, NOTIFIER_DIGEST)
    assert exc.value.code == "not_enabled"


def test_discovery_refuses_an_unknown_or_not_enabled_tool() -> None:
    discovery = _discovery()
    with pytest.raises(PluginHostError) as exc:
        discovery.resolve(VIEWER_ID, VIEWER_DIGEST, tool_id="tasks.delete")
    assert exc.value.code == "unknown_tool"
    # issues.read exists in the catalog AND is enabled; a tool present but not in
    # enabled_tools would be tool_not_enabled -- construct that case.
    capability = _capability()
    for entry in capability["plugins"]:
        if entry["plugin_id"] == VIEWER_ID:
            entry["enabled_tools"] = ["tasks.list"]  # drop issues.read from the profile
    capability["digest"] = contract_digest("plugin-capability", capability)
    discovery2 = PluginDiscovery(_catalog(), capability)
    with pytest.raises(PluginHostError) as exc2:
        discovery2.resolve(VIEWER_ID, VIEWER_DIGEST, tool_id="issues.read")
    assert exc2.value.code == "tool_not_enabled"


def test_discovery_input_is_ids_only_never_an_arbitrary_source() -> None:
    # The resolve signature accepts ids/digests only -- there is no source/path/
    # url parameter, so a caller cannot request an arbitrary origin. Only the
    # operator-configured local file is ever loaded (from_sources).
    import inspect

    params = set(inspect.signature(PluginDiscovery.resolve).parameters)
    assert params == {"self", "plugin_id", "plugin_digest", "tool_id"}


def test_discovery_rejects_an_undeclared_transport() -> None:
    with pytest.raises(PluginHostError) as exc:
        PluginCatalogSource("http", "https://evil.example/catalog.json")
    assert exc.value.code == "unsupported_transport"


def test_discovery_fails_closed_on_a_drifted_catalog_document() -> None:
    catalog = _catalog()
    catalog["plugins"][0]["title"] = "tampered"  # digest no longer recomputes
    with pytest.raises(Exception):
        PluginDiscovery(catalog, _capability())


def test_from_sources_loads_operator_reviewed_local_files(tmp_path: Path) -> None:
    cat_path = tmp_path / "catalog.json"
    cap_path = tmp_path / "capability.json"
    cat_path.write_text(json.dumps(_catalog()), encoding="utf-8")
    cap_path.write_text(json.dumps(_capability()), encoding="utf-8")
    discovery = PluginDiscovery.from_sources(
        PluginCatalogSource("local_json", str(cat_path)),
        PluginCatalogSource("local_json", str(cap_path)),
    )
    assert {p["plugin_id"] for p in discovery.published()} == {VIEWER_ID, NOTIFIER_ID}


# --------------------------------------------------------------------------- #
# T002 criterion 2 / T003: credential isolation.
# --------------------------------------------------------------------------- #


def test_credential_handle_has_no_value_field() -> None:
    # A credential value is not representable: the handle carries a reference and
    # an opaque token only, so no secret can be serialized or cross the boundary.
    import dataclasses

    fields = {f.name for f in dataclasses.fields(CredentialHandle)}
    assert fields == {"owner_host", "ref", "handle"}
    for forbidden in ("value", "secret", "token_value", "material", "password", "key"):
        assert forbidden not in fields


def test_credential_broker_resolves_only_the_plugins_own_refs() -> None:
    plugin = next(p for p in _catalog()["plugins"] if p["id"] == NOTIFIER_ID)
    handles = _broker().resolve(plugin)
    assert [h.ref for h in handles] == ["deploy-channel-ref"]
    assert all(h.owner_host == "anvil-connector-host" and h.handle for h in handles)


def test_credential_broker_cannot_reach_workbench_or_other_plugin_secrets() -> None:
    # The broker holds a Workbench secret and another plugin's ref under other
    # hosts. The notifier's entry names only its own host/ref, so those are
    # structurally unreachable -- resolve returns only the declared ref.
    broker = CredentialBroker(
        {
            "anvil-connector-host": ["deploy-channel-ref"],
            "workbench-hub": ["workbench-db-password", "github-token"],
            "other-plugin-host": ["someone-elses-ref"],
        }
    )
    plugin = next(p for p in _catalog()["plugins"] if p["id"] == NOTIFIER_ID)
    handles = broker.resolve(plugin)
    refs = {h.ref for h in handles}
    hosts = {h.owner_host for h in handles}
    assert refs == {"deploy-channel-ref"}
    assert hosts == {"anvil-connector-host"}
    assert "workbench-db-password" not in refs and "github-token" not in refs


def test_credential_scope_mismatch_fails_closed_before_dispatch() -> None:
    # The host does NOT own the ref the plugin declares: resolution fails closed
    # on its typed reason, and (below) the install never reaches the host runner.
    broker = CredentialBroker({"anvil-connector-host": ["some-other-ref"]})
    plugin = next(p for p in _catalog()["plugins"] if p["id"] == NOTIFIER_ID)
    with pytest.raises(PluginHostError) as exc:
        broker.resolve(plugin)
    assert exc.value.code == "credential_unavailable"


def test_credential_unknown_host_fails_closed() -> None:
    broker = CredentialBroker({"some-other-host": ["deploy-channel-ref"]})
    plugin = next(p for p in _catalog()["plugins"] if p["id"] == NOTIFIER_ID)
    with pytest.raises(PluginHostError) as exc:
        broker.resolve(plugin)
    assert exc.value.code == "unknown_host"


def test_credential_none_requirement_resolves_no_handles() -> None:
    plugin = next(p for p in _catalog()["plugins"] if p["id"] == VIEWER_ID)
    assert _broker().resolve(plugin) == ()


def test_install_blocks_on_credential_mismatch_before_running_host() -> None:
    broker = CredentialBroker({"anvil-connector-host": ["some-other-ref"]})
    runner = _installed_runner()
    store = MemoryPluginHostStore()
    receipt = store.install(make_install_request(), _discovery(), broker, runner)
    assert receipt["status"] == "denied"
    assert receipt["error"]["code"] == "credential_unavailable"
    assert runner.calls == []  # the host was never dispatched
    # A denied preflight persists nothing, so the request stays retriable.
    assert store.rows.receipts == {}


# --------------------------------------------------------------------------- #
# T002 criterion 3: lifecycle fail-closed on replay/drift/failure/unknown.
# --------------------------------------------------------------------------- #


def test_lifecycle_install_accepted_reports_credentials_by_reference_only() -> None:
    store = MemoryPluginHostStore()
    runner = _installed_runner()
    receipt = store.install(make_install_request(), _discovery(), _broker(), runner)
    assert receipt["status"] == "accepted"
    assert receipt["effect"] == "plugin_lifecycle"
    assert receipt["credential_use"] == {
        "requirement": "host_owned",
        "owner_host": "anvil-connector-host",
        "credential_refs": ["deploy-channel-ref"],
    }
    # No value field anywhere in the credential report.
    assert set(receipt["credential_use"]) == {"requirement", "owner_host", "credential_refs"}
    assert runner.calls == [(NOTIFIER_ID, ("deploy-channel-ref",))]


def test_lifecycle_replay_returns_prior_receipt_without_rerunning_host() -> None:
    store = MemoryPluginHostStore()
    runner = _installed_runner()
    request = make_install_request()
    first = store.install(request, _discovery(), _broker(), runner)
    second = store.install(request, _discovery(), _broker(), runner)
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"  # idempotent replay
    assert second["result"] == first["result"]
    assert len(runner.calls) == 1  # the host effect ran exactly once
    assert len(store.rows.receipts) == 1


def test_lifecycle_digest_drift_fails_closed() -> None:
    # A request pinning a digest the reviewed catalog no longer carries is denied
    # on the digest reason, and the host is never run.
    store = MemoryPluginHostStore()
    runner = _installed_runner()
    request = make_install_request(plugin_digest="sha256:" + "b" * 64)
    receipt = store.install(request, _discovery(), _broker(), runner)
    assert receipt["status"] == "denied" and receipt["error"]["code"] == "digest_drift"
    assert runner.calls == []
    assert store.rows.receipts == {}


def test_lifecycle_host_failure_denies_and_stays_retriable() -> None:
    store = MemoryPluginHostStore()

    def failer(discovered, handles):
        raise PluginHostFailure(detail="host crashed")

    request = make_install_request()
    receipt = store.install(request, _discovery(), _broker(), failer)
    assert receipt["status"] == "denied"
    assert receipt["error"]["code"] == "host_failure" and receipt["error"]["retryable"] is True
    # Nothing persisted: a later identical request re-attempts (retriable).
    assert store.rows.receipts == {}
    ok = store.install(request, _discovery(), _broker(), _installed_runner())
    assert ok["status"] == "accepted"


def test_lifecycle_unknown_outcome_reconciles_and_persists() -> None:
    store = MemoryPluginHostStore()

    def unknown(discovered, handles):
        return HostInstallOutcome(status="unknown", summary="in-flight; outcome unconfirmed")

    request = make_install_request()
    receipt = store.install(request, _discovery(), _broker(), unknown)
    assert receipt["status"] == "reconcile"
    assert receipt["reconciliation"]["code"] == "install_outcome_unknown"
    # An unknown outcome persists, so a replay reconciles rather than re-attempting
    # a possibly-live effect.
    replay = store.install(request, _discovery(), _broker(), _installed_runner())
    assert replay["status"] == "reconcile"
    assert len(store.rows.receipts) == 1


def test_lifecycle_refuses_a_non_install_kind() -> None:
    store = MemoryPluginHostStore()
    request = make_install_request()
    request["kind"] = "tool_call"  # break the kind; digest no longer matches either
    with pytest.raises(PluginHostError) as exc:
        store.install(request, _discovery(), _broker(), _installed_runner())
    assert exc.value.code in {"invalid_request", "unsupported_kind"}


def test_receipt_lookup_returns_a_persisted_receipt_or_none() -> None:
    store = MemoryPluginHostStore()
    request = make_install_request()
    store.install(request, _discovery(), _broker(), _installed_runner())
    stored = store.receipt(request["request_digest"])
    assert stored is not None and stored["status"] == "accepted"
    assert store.receipt("sha256:" + "0" * 64) is None


# --------------------------------------------------------------------------- #
# Concurrency: contested check->act resolves to exactly one host effect.
# --------------------------------------------------------------------------- #


def test_concurrent_identical_installs_run_the_host_exactly_once() -> None:
    store = MemoryPluginHostStore()
    executions: list[str] = []
    exec_lock = threading.Lock()

    def runner(discovered, handles):
        with exec_lock:
            executions.append("ran")
        return HostInstallOutcome(status="installed", output={"installed": True})

    request = make_install_request()
    discovery = _discovery()
    broker = _broker()

    # Force aggressive thread switching so the race is real: without the store's
    # lock two threads both observe "no receipt" and both run the host effect.
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}

    def race(name: str) -> None:
        barrier.wait()
        results[name] = store.install(request, discovery, broker, runner)

    try:
        threads = [threading.Thread(target=race, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(previous)

    assert len(executions) == 1  # the host effect ran exactly once
    assert len(store.rows.receipts) == 1
    statuses = sorted(r["status"] for r in results.values())
    assert statuses == ["accepted", "duplicate"]  # one first, one idempotent replay


# --------------------------------------------------------------------------- #
# T003 criterion 3: every human-readable receipt field is scrubbed.
# --------------------------------------------------------------------------- #

# The adversarial corpus the gate seeds: each item is a distinctive raw marker
# that must NOT survive into a persisted/served receipt.
_CORPUS = {
    "akia": "AKIAIOSFODNN7EXAMPLE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
    "pem": "-----BEGIN RSA PRIVATE KEY-----",
    "ipport": "10.0.0.5:5432",
    "dottedhost": "internal.db.corp:9200",
    "etcpath": "/etc/anvil/secrets.yaml",
    "dburl": "postgresql://user:pw@db:5432/anvil",
    "skproj": "sk-proj-ABCDEFGH12345678",
    "ghp": "ghp_ABCDEFGHIJKLMNOP0123456789",
    "cpath": "C:" + chr(92) + "Users" + chr(92) + "admin",
    "tailnet": "serving.tail1234.ts.net",
}
_CORPUS_TEXT = "install failed: " + " ".join(_CORPUS.values())


def _assert_no_corpus(blob: str) -> None:
    for name, marker in _CORPUS.items():
        assert marker not in blob, f"corpus item {name!r} leaked: {marker!r}"


def test_redaction_reconcile_summary_is_scrubbed_in_the_persisted_receipt() -> None:
    store = MemoryPluginHostStore()

    def unknown(discovered, handles):
        return HostInstallOutcome(status="unknown", summary=_CORPUS_TEXT)

    request = make_install_request()
    receipt = store.install(request, _discovery(), _broker(), unknown)
    assert receipt["status"] == "reconcile"
    _assert_no_corpus(json.dumps(receipt))
    # And the durably persisted copy is equally clean.
    _assert_no_corpus(json.dumps(store.receipt(request["request_digest"])))


def test_redaction_host_failure_summary_is_scrubbed() -> None:
    store = MemoryPluginHostStore()

    def failer(discovered, handles):
        raise PluginHostFailure(detail=_CORPUS_TEXT)

    receipt = store.install(make_install_request(), _discovery(), _broker(), failer)
    assert receipt["status"] == "denied"
    _assert_no_corpus(json.dumps(receipt))


def test_redaction_accepted_output_summary_is_scrubbed() -> None:
    store = MemoryPluginHostStore()
    receipt = store.install(
        make_install_request(), _discovery(), _broker(), _installed_runner(summary=_CORPUS_TEXT)
    )
    assert receipt["status"] == "accepted"
    _assert_no_corpus(json.dumps(receipt))


def test_redaction_falls_back_when_a_residual_shape_would_trip_the_backstop() -> None:
    # A key=value shape the config scrub leaves intact still trips the receipt
    # safeText backstop, so the fixed safe fallback is used -- never the raw text.
    summary = _safe_receipt_summary("attempt=3 outcome=maybe")
    assert "attempt=3" not in summary
    assert summary  # a receipt is always emittable


def test_redaction_status_is_declared_on_every_receipt() -> None:
    store = MemoryPluginHostStore()
    receipt = store.install(make_install_request(), _discovery(), _broker(), _installed_runner())
    assert receipt["redaction"] == {"status": "redacted"}


# --------------------------------------------------------------------------- #
# Service facade + operator-declared loading.
# --------------------------------------------------------------------------- #


def test_service_from_files_builds_from_operator_reviewed_paths(tmp_path: Path) -> None:
    cat_path = tmp_path / "catalog.json"
    cap_path = tmp_path / "capability.json"
    cat_path.write_text(json.dumps(_catalog()), encoding="utf-8")
    cap_path.write_text(json.dumps(_capability()), encoding="utf-8")
    service = PluginHostService.from_files(str(cat_path), str(cap_path))
    assert {p["plugin_id"] for p in service.list_plugins()} == {VIEWER_ID, NOTIFIER_ID}
    assert service.get_plugin("no-such") is None


def test_service_projection_reports_credentials_by_reference_only() -> None:
    service = PluginHostService(_discovery())
    notifier = service.get_plugin(NOTIFIER_ID)
    assert notifier is not None
    cred = notifier["credential"]
    assert cred["requirement"] == "host_owned"
    assert cred["credential_refs"] == ["deploy-channel-ref"]
    # Structurally no value field anywhere in the projected plugin.
    blob = json.dumps(notifier).lower()
    for marker in ("value", "\"secret\"", "password", "api_key", "token"):
        assert marker not in blob, f"projection leaked {marker!r}"
