"""Bounded PRD-content read adapter over the pinned State read set.

This adapter turns the pinned ``state.prd.read_content`` descriptor from
:mod:`workbench.state_manifest` into a validated, bounded, digest-attributed
read of one PRD's human-readable content.  It is the only supported way for
Workbench to obtain that content: the State CLI is the transport, the
``workbench-prd-content/v1`` contract is the payload shape, and the content
digest is the hub's idempotent publication key.  The body is untrusted task
data for safe rendering only; it grants no capability.

Fail-closed rules:

* The adapter takes the immutable :class:`PinnedStateReadOperations` set in
  its constructor and uses only the pinned ``state.prd.read_content``
  descriptor.  No method accepts a caller-selected operation id, so a
  browser- or model-chosen operation cannot reach the State CLI through this
  surface.
* The request is scoped: the caller names exactly one ``prd_id`` (validated
  against the pinned input schema before the runner is ever invoked) plus an
  optional ``expected_revision``.  Only the typed inputs the pinned input
  schema declares are transmitted; ``expected_revision`` is a post-read check
  against the returned ``prd.revision``, never an extra CLI input.
* A payload is validated in order -- contract schema (closed objects, bounded
  prose), pinned output schema, provider identity, scoped-PRD equality,
  expected-revision freshness, UTF-8 encodability, then the reference
  validator (:func:`workbench.contracts.validate_prd_content`: digest
  recompute, the 64 KiB UTF-8 byte bound, truncation/total_bytes coherence).
  Any failure raises :class:`PrdContentError` before a result exists; there
  is no partial :class:`PublishablePrdContent`.
* A response naming a PRD other than the requested one is refused outright:
  this surface returns one scoped PRD's content, never a whole-project dump.

Like discovery and the snapshot adapter, this adapter is validated
hermetically against fixtures shaped like
``docs/contracts/examples/anvil-state.prd-content.v1.json``; live
qualification stays gated on the upstream State CLI advertising the operation
catalog from ``anvil describe`` (fakoli/anvil#178).  It is deliberately not
wired into the live bridge poll loop yet.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .contracts import ContractValidationError, validate_prd_content
from .state_manifest import (
    PRD_READ_CONTENT_OPERATION_ID,
    PinnedStateOperation,
    PinnedStateReadOperations,
)


class PrdContentError(RuntimeError):
    """The bounded PRD-content read cannot be trusted for publication."""


PRD_CONTENT_SCHEMA_VERSION = "workbench-prd-content/v1"

#: The request-side scope bound; identical to the contract's prd_id pattern so
#: an invalid identifier is refused before any CLI transport is invoked.
_PRD_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_PRD_CONTENT_CONTRACT_SCHEMA = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "prd-content.v1.schema.json"
)
_prd_content_contract_validator_cache: Draft202012Validator | None = None

#: Object paths in the contract schema that must stay closed so a payload
#: cannot smuggle unbounded content in an undeclared field.
_CLOSED_OBJECT_PATHS: tuple[tuple[str, ...], ...] = (
    (),
    ("properties", "prd"),
    ("properties", "content"),
    ("properties", "redaction"),
)

#: Bounded prose fields whose maxLength must survive schema drift.  The body
#: bound here is the code-point ceiling; the normative 64 KiB UTF-8 byte bound
#: is enforced by :func:`workbench.contracts.validate_prd_content`.
_BOUNDED_PROSE_PATHS: tuple[tuple[str, ...], ...] = (
    ("properties", "generated_at"),
    ("properties", "prd", "properties", "title"),
    ("properties", "content", "properties", "body"),
)


def _schema_node(schema: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = schema
    for name in path:
        node = node.get(name) if isinstance(node, Mapping) else None
    return node


def _prd_content_contract_validator() -> Draft202012Validator:
    """Load the prd-content contract schema once; refuse a drifted or open schema."""
    global _prd_content_contract_validator_cache
    if _prd_content_contract_validator_cache is None:
        try:
            schema = json.loads(_PRD_CONTENT_CONTRACT_SCHEMA.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrdContentError(
                "prd-content contract schema is unavailable; refusing to publish PRD content"
            ) from exc
        for path in _CLOSED_OBJECT_PATHS:
            node = _schema_node(schema, path)
            if not isinstance(node, Mapping) or node.get("additionalProperties") is not False:
                raise PrdContentError(
                    "prd-content contract schema no longer closes its objects "
                    f"(additionalProperties must be false at {'/'.join(path) or '<root>'}); "
                    "refusing to publish PRD content"
                )
        for path in _BOUNDED_PROSE_PATHS:
            node = _schema_node(schema, path)
            if not isinstance(node, Mapping) or not isinstance(node.get("maxLength"), int):
                raise PrdContentError(
                    "prd-content contract schema no longer bounds its prose "
                    f"(maxLength required at {'/'.join(path)}); refusing to publish PRD content"
                )
        _prd_content_contract_validator_cache = Draft202012Validator(schema, format_checker=FormatChecker())
    return _prd_content_contract_validator_cache


def _reset_prd_content_contract_validator_cache() -> None:
    """Test hook: force the next validation to reload the on-disk schema."""
    global _prd_content_contract_validator_cache
    _prd_content_contract_validator_cache = None


@dataclass(frozen=True)
class PublishablePrdContent:
    """One fully validated bounded read; ``content_digest`` is the publication key.

    ``body_bytes``, ``total_bytes``, and ``truncated`` are the explicit bound
    metadata: ``body_bytes`` is the exact UTF-8 size of the returned body,
    ``total_bytes`` the size of the full source document, and ``truncated``
    whether the body is a bounded prefix of it.
    """

    provider: str
    schema_version: str
    content_digest: str
    operation_id: str
    operation_digest: str
    prd_id: str
    prd_revision: int
    prd_title: str
    prd_status: str
    content_format: str
    body_bytes: int
    total_bytes: int
    truncated: bool
    payload_json: str

    @property
    def payload(self) -> dict[str, Any]:
        """A fresh parsed copy; mutating it cannot alter the validated read."""
        return json.loads(self.payload_json)

    @property
    def body(self) -> str:
        """The bounded, untrusted body text (safe-rendering data only)."""
        return self.payload["content"]["body"]


def _json_document(raw: str) -> dict[str, Any]:
    """Parse a JSON document even when the State CLI emits a status line first.

    Mirrors ``StateReader._json_document`` in :mod:`workbench.bridge`.
    """
    decoder = json.JSONDecoder()
    for position, character in enumerate(raw):
        if character not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(raw[position:])
        except json.JSONDecodeError:
            continue
        if raw[position + end :].strip():
            continue
        if isinstance(value, dict):
            return value
    raise PrdContentError("State PRD-content command must return one JSON object")


def _payload_from_document(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap the State CLI envelope, if present, and return the payload object."""
    if "ok" in document or "data" in document:
        if document.get("ok") is not True:
            raise PrdContentError("State PRD-content envelope did not report ok")
        data = document.get("data")
        if not isinstance(data, Mapping):
            raise PrdContentError("State PRD-content envelope has no data object")
        return data
    return document


def _validated_request_inputs(
    operation: PinnedStateOperation, prd_id: Any, expected_revision: Any,
) -> dict[str, Any]:
    """Refuse an out-of-scope request before any CLI transport is invoked."""
    if not isinstance(prd_id, str) or _PRD_ID_PATTERN.fullmatch(prd_id) is None:
        raise PrdContentError(
            f"requested prd_id is not a valid scoped PRD identifier: {str(prd_id)[:100]!r}"
        )
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise PrdContentError(
            f"expected_revision must be an integer >= 1: {str(expected_revision)[:100]!r}"
        )
    # Only the typed inputs the pinned input schema declares may reach the
    # transport; expected_revision is deliberately NOT among them (the catalog
    # input schema takes prd_id only), so it stays a post-read freshness check.
    inputs = {"prd_id": prd_id}
    try:
        Draft202012Validator(operation.input_schema).validate(inputs)
    except ValidationError as exc:
        raise PrdContentError(
            f"request does not match the pinned input schema: {exc.message[:500]}"
        ) from exc
    return inputs


def validate_prd_content_payload(
    payload: Mapping[str, Any],
    operation: PinnedStateOperation,
    requested_prd_id: str,
    expected_revision: int | None = None,
) -> PublishablePrdContent:
    """Fail-closed validate one bounded PRD-content payload against the pinned descriptor.

    Order matters: the contract schema rejects schema drift, unbounded prose,
    and smuggled fields first; the pinned output schema then binds the payload
    to the descriptor the manifest advertised; provider identity and scoped-PRD
    equality bind it to the exact requested read; the expected-revision check
    refuses a stale source; the encoding check refuses a body that is not valid
    UTF-8; and the reference validator finally checks what schemas cannot --
    digest recompute, the 64 KiB byte bound, and truncation coherence.  Nothing
    is returned unless every check passes.
    """
    if operation.operation_id != PRD_READ_CONTENT_OPERATION_ID:
        raise PrdContentError(
            f"PRD-content adapter only executes {PRD_READ_CONTENT_OPERATION_ID}, "
            f"got {operation.operation_id}"
        )
    materialized = dict(payload)
    try:
        _prd_content_contract_validator().validate(materialized)
    except ValidationError as exc:
        raise PrdContentError(
            f"State PRD-content payload does not conform to the {PRD_CONTENT_SCHEMA_VERSION} "
            f"contract: {exc.message[:500]}"
        ) from exc
    try:
        Draft202012Validator(operation.output_schema).validate(materialized)
    except ValidationError as exc:
        raise PrdContentError(
            f"State PRD-content payload does not match the pinned output schema: {exc.message[:500]}"
        ) from exc
    if materialized.get("provider") != operation.provider:
        raise PrdContentError(
            f"State PRD-content names an unexpected provider: {materialized.get('provider')!r}"
        )
    prd = materialized["prd"]
    if prd.get("prd_id") != requested_prd_id:
        raise PrdContentError(
            "State PRD-content returned a PRD other than the requested scope "
            f"(requested {requested_prd_id!r}, got {str(prd.get('prd_id'))[:100]!r}); "
            "refusing an out-of-scope content dump"
        )
    revision = prd["revision"]
    if expected_revision is not None and revision != expected_revision:
        raise PrdContentError(
            f"State PRD-content revision is stale or unexpected: expected revision "
            f"{expected_revision}, got {revision}"
        )
    body = materialized["content"]["body"]
    try:
        body_bytes = len(body.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PrdContentError(
            "State PRD-content body is not valid UTF-8; refusing to publish"
        ) from exc
    try:
        validate_prd_content(materialized)
    except ContractValidationError as exc:
        raise PrdContentError(f"State PRD-content failed reference validation: {exc}") from exc
    except UnicodeEncodeError as exc:
        raise PrdContentError(
            "State PRD-content payload is not valid UTF-8; refusing to publish"
        ) from exc
    return PublishablePrdContent(
        provider=str(materialized["provider"]),
        schema_version=str(materialized["schema_version"]),
        content_digest=str(materialized["content_digest"]),
        operation_id=operation.operation_id,
        operation_digest=operation.operation_digest,
        prd_id=str(prd["prd_id"]),
        prd_revision=int(revision),
        prd_title=str(prd["title"]),
        prd_status=str(prd["status"]),
        content_format=str(materialized["content"]["format"]),
        body_bytes=body_bytes,
        total_bytes=int(materialized["content"]["total_bytes"]),
        truncated=bool(materialized["content"]["truncated"]),
        payload_json=json.dumps(
            materialized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ),
    )


class PrdContentAdapter:
    """Execute the pinned bounded PRD-content read and validate it for publication.

    The runner is injectable for hermetic tests; the default runner executes
    the bridge-configured content command argv (never a hardcoded path) with
    the validated ``prd_id`` appended as the final argument -- the identifier
    pattern admits no whitespace or shell metacharacters.  The runner always
    receives the pinned descriptor plus the schema-validated typed inputs, so
    the transport can attest exactly which advertised operation it is
    executing on which scoped PRD -- there is no parameter anywhere on this
    class through which a caller could name a different operation.
    """

    def __init__(
        self,
        pinned: PinnedStateReadOperations,
        content_command: str,
        runner: Callable[[PinnedStateOperation, Sequence[str]], str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        if not content_command.strip():
            raise PrdContentError("State PRD-content command is not configured")
        self._operation = pinned.descriptor(PRD_READ_CONTENT_OPERATION_ID)
        self._content_command = content_command
        self._runner = runner if runner is not None else self._run_content
        self._cwd = cwd

    def fetch(
        self, prd_id: str, expected_revision: int | None = None,
    ) -> PublishablePrdContent:
        """Run the pinned read for one scoped PRD and return the validated result."""
        inputs = _validated_request_inputs(self._operation, prd_id, expected_revision)
        args = shlex.split(self._content_command, posix=os.name != "nt") + [inputs["prd_id"]]
        document = _json_document(self._runner(self._operation, args))
        return validate_prd_content_payload(
            _payload_from_document(document), self._operation, prd_id, expected_revision,
        )

    def _run_content(self, operation: PinnedStateOperation, args: Sequence[str]) -> str:
        completed = subprocess.run(
            list(args), cwd=self._cwd, capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:500]
            raise PrdContentError(
                f"State PRD-content command failed ({operation.bridge_adapter}): {detail}"
            )
        return completed.stdout
