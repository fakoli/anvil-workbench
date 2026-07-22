"""Advanced-model-playground supervision surfaces (T006 / T009 / T010).

Three actor-private, hub-durable surfaces that hang off the merged Advanced-mode
runtime (``advanced_runtime`` / ``advanced_routes`` / ``contracts``) and reuse its
digest-drift → repair spine and the export/redaction infrastructure rather than
re-implementing any of it:

* **presets (T006)** — a named, digest-pinned Advanced selection.  Selecting a
  preset whose pinned route/tool/profile digest has DRIFTED opens REPAIR MODE
  (the deterministic ``contracts._advanced_preset_drift`` set) and NEVER silently
  substitutes a route or tool.  Presets export through a closed, size-bounded,
  redaction-enveloped serialization.

* **comparison (T006)** — a FACTUAL side-by-side of 2–4 attempts.  Labels are
  factual integer counters; a ranking (a winner) is representable ONLY alongside a
  declared, ``non_qualification`` evaluation criterion
  (``contracts.validate_advanced_comparison``), so no winner is inferred.

* **templates (T009)** — a named, digest-pinned instruction template whose full
  body + declared substitutions are visible PRE-SEND and recorded as DECLARED
  instructions (never a covert injected prompt).  A drifted or removed template
  digest opens REPAIR MODE.

* **ratings (T010)** — an actor-local preference over a route.  A rating cannot be
  recorded without naming a DECLARED criterion; aggregates carry the
  ``non_qualification`` label and are structurally absent from every
  delivery-evidence / qualification surface; ratings export ONLY inside the
  redaction envelope.

Every store is actor-scoped BY CONSTRUCTION: a foreign or missing id raises the
SAME :class:`AdvancedPlaygroundNotFound`, and a list ranges only over the acting
actor, so a cross-actor probe is never an existence oracle.  Like the other
supervision models this service is not wired into the live bridge poll loop; the
hub app leaves it ``None`` (fail-closed 503) until injected.
"""
from __future__ import annotations

import copy
import json
import re
import threading
from typing import Any, Callable, Mapping

from .redaction import redact_config_text
from .contracts import (
    ContractValidationError,
    _advanced_preset_drift,
    _advanced_template_drift,
    validate_advanced_comparison,
    validate_advanced_preset,
    validate_advanced_template,
)
from .models import new_id, now_utc, opaque_scope_ref, require_pref_audit_key

#: The pinned export/envelope schema versions.  An envelope declaring any other
#: version is an unknown/unsupported envelope and is REJECTED, not coerced — this
#: is what makes the redaction envelope REQUIRED: a bare item list without the
#: versioned envelope can never be interpreted as an export.
PRESET_EXPORT_SCHEMA_VERSION = "workbench-advanced-preset-export/v1"
TEMPLATE_EXPORT_SCHEMA_VERSION = "workbench-advanced-template-export/v1"
RATING_EXPORT_SCHEMA_VERSION = "workbench-advanced-rating-export/v1"

#: Every export body is bounded: a serialized envelope larger than this is
#: refused rather than emitted, and the per-actor item cap keeps it well under.
MAX_EXPORT_BYTES = 262_144  # 256 KiB
MAX_ITEMS_PER_ACTOR = 200

#: The FIXED, non-leaking detail for an unknown id lookup, so a foreign vs. a
#: missing id are indistinguishable (never a cross-actor existence oracle).
UNKNOWN_ITEM_DETAIL = "unknown advanced playground item"

#: R018: the reviewed, DECLARED evaluation criteria a rating or a comparison
#: ranking may name.  A rating that names no declared criterion — or names one
#: outside this closed set — is refused, so there is no free-text ungrounded
#: rating.  Every criterion is non-qualification by construction.
DECLARED_RATING_CRITERIA: dict[str, str] = {
    "instruction_following": "Instruction following",
    "response_quality": "Response quality",
    "latency": "Latency",
    "conciseness": "Conciseness",
    "format_adherence": "Format adherence",
}

MIN_RATING_SCORE = 1
MAX_RATING_SCORE = 5

_ROUTE_ID_MAX = 128
_ROUTE_ID_PATTERN_HEAD = "abcdefghijklmnopqrstuvwxyz"


class AdvancedPlaygroundError(ValueError):
    """An advanced-playground request is malformed or violates a declared rule.

    Raised BEFORE any effect, so a rejected request mutates nothing.  The API maps
    it to a typed 422.
    """


class AdvancedPlaygroundNotFound(AdvancedPlaygroundError):
    """A named preset / template / rating does not exist for the acting actor.

    Raised IDENTICALLY for a missing id and for another actor's id, so a
    cross-actor probe cannot learn whether the id exists.  The API maps it to a
    fixed 404 body.
    """


def _require_route_id(route_id: Any) -> str:
    if not isinstance(route_id, str) or not route_id:
        raise AdvancedPlaygroundError("a route id is required")
    if len(route_id) > _ROUTE_ID_MAX or route_id[0] not in _ROUTE_ID_PATTERN_HEAD:
        raise AdvancedPlaygroundError("route id is not a valid route reference")
    for ch in route_id:
        if not (ch.isdigit() or ch in _ROUTE_ID_PATTERN_HEAD or ch in "._-"):
            raise AdvancedPlaygroundError("route id is not a valid route reference")
    return route_id


def _bounded(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Return the envelope only if its serialized form is within the size bound."""
    serialized = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(serialized) > MAX_EXPORT_BYTES:
        raise AdvancedPlaygroundError(
            f"advanced playground export exceeds the {MAX_EXPORT_BYTES}-byte bound"
        )
    return dict(envelope)


#: Header/authorization/short-bearer shapes the config-text scrub leaves behind
#: (its ``Bearer`` rule demands an 8+ char token and it has no ``authorization``
#: keyword), reused to close the "header" class on an exported free-text field
#: (a preset name, a template body, a rating note).  The config scrub already
#: closes the credential / path / URL / host classes; this is the header
#: last-mile so an export passes the secret/path/header scan in full.
#:
#: The authorization rule consumes the header VALUE THROUGH an optional
#: ``Bearer <token>``: the ``(?:bearer\s+)?`` lets ``[^\s,;]*`` reach the real
#: token rather than stopping at the literal ``Bearer`` label (which would leave a
#: short token like ``xyz`` behind — the token value, not just the label, must
#: die).  It stays anchored to the token (stops at the next space/segment
#: boundary), so it does not over-redact the whole line.  ``(?:proxy-)?`` covers
#: ``proxy-authorization`` and the standalone-``Bearer`` rule catches a bare
#: ``Bearer <token>`` that no ``authorization`` keyword precedes.  Covers
#: ``authorization:``, ``authorization: Bearer``, ``authorization:Bearer`` (no
#: space), and ``proxy-authorization:``.
_EXPORT_HEADER_PATTERNS = (
    re.compile(r"(?i)\b(?:proxy-)?authorization\b\s*[:=]?\s*(?:bearer\s+)?[^\s,;]*"),
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(r"(?i)\bx-(?:api-)?(?:key|auth|token)[a-z0-9-]*\b\s*[:=]?\s*[^\s,;]*"),
)


def _redact_export_text(text: str) -> str:
    scrubbed = redact_config_text(text)
    for pattern in _EXPORT_HEADER_PATTERNS:
        scrubbed = pattern.sub("[REDACTED]", scrubbed)
    return scrubbed


def _scrub_export(value: Any) -> Any:
    """Recursively scrub every free-text STRING in an exported item.

    Scans string VALUES (never the JSON blob), so a numeric counter or a
    ``sha256:`` digest is untouched while a credential / path / URL / host /
    header shape in an operator-authored free-text field is neutralized.
    """
    if isinstance(value, str):
        return _redact_export_text(value)
    if isinstance(value, dict):
        return {key: _scrub_export(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_export(item) for item in value]
    return value


def _strip_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the embedded raw ``actor`` identity from an exported record.

    The export envelope already carries a SAFE OPAQUE ``actor_ref``; the per-item
    ``actor`` block would otherwise leak the raw authoring actor id into the
    portable export.  Everything else (digest-pinned content, prose labels) is
    preserved so the export stays re-importable.
    """
    projected = dict(copy.deepcopy(record))
    projected.pop("actor", None)
    return projected


# --------------------------------------------------------------------------- #
# Presets (T006)
# --------------------------------------------------------------------------- #


class AdvancedPresetStore:
    """An actor-private durable store of digest-pinned Advanced presets.

    Save validates the preset against the current live digests (a preset stored
    ready must not already be drifting).  :meth:`resolve` recomputes drift against
    the SERVER-DERIVED live digests at SELECTION time and, on any drift, returns a
    REPAIR-mode result naming exactly the drifted references — it never returns a
    substitute route or tool.  Export ranges only over the actor's presets and
    wraps them in a closed, size-bounded, redaction-enveloped serialization.

    ``live_digests_provider`` is the server's authority for the CURRENT advanced
    route/profile/tool/response-schema registry — a callable returning
    ``{ref_kind: {id: digest}}``.  Resolve derives readiness ONLY from this
    server-side source and NEVER from a client-supplied override, so a browser can
    neither spoof a drifted pin to "ready" nor produce false drift by omitting a
    tool/response-schema digest the server actually knows.  When the provider is
    absent (the surface is not wired to a registry) resolve reports an explicit
    ``unverifiable`` status rather than defaulting a missing digest to drift.
    """

    def __init__(
        self, *, audit_key: bytes,
        live_digests_provider: "Callable[[], Mapping[str, Mapping[str, str]]] | None" = None,
    ) -> None:
        self._audit_key = require_pref_audit_key(audit_key)
        self._lock = threading.RLock()
        # actor -> {preset_id -> preset record}
        self._by_actor: dict[str, dict[str, dict[str, Any]]] = {}
        self._live_digests_provider = live_digests_provider

    def save(
        self, actor: str, preset: Mapping[str, Any], live_digests: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any]:
        """Validate and persist a preset for ``actor``; return the stored record.

        The preset must be schema-valid, tamper-consistent (its ``preset_digest``
        recomputes), and NOT already drifting — a preset is saved ``ready`` so a
        later drift is detected as a change from that baseline.
        """
        record = copy.deepcopy(dict(preset))
        try:
            validate_advanced_preset(record, live_digests)
        except ContractValidationError as exc:
            raise AdvancedPlaygroundError(f"preset is not valid: {exc}") from exc
        repair = record.get("repair", {})
        if not isinstance(repair, Mapping) or repair.get("status") != "ready":
            raise AdvancedPlaygroundError("a preset must be saved in the ready (undrifted) state")
        preset_id = str(record.get("preset_id"))
        with self._lock:
            presets = self._by_actor.setdefault(actor, {})
            if preset_id not in presets and len(presets) >= MAX_ITEMS_PER_ACTOR:
                raise AdvancedPlaygroundError("preset limit reached for this actor")
            presets[preset_id] = record
        return copy.deepcopy(record)

    def list(self, actor: str) -> list[dict[str, Any]]:
        with self._lock:
            presets = self._by_actor.get(actor, {})
            return [copy.deepcopy(presets[pid]) for pid in sorted(presets)]

    def get(self, actor: str, preset_id: str) -> dict[str, Any]:
        with self._lock:
            presets = self._by_actor.get(actor, {})
            record = presets.get(preset_id)
            if record is None:
                raise AdvancedPlaygroundNotFound(UNKNOWN_ITEM_DETAIL)
            return copy.deepcopy(record)

    def _server_live_digests(self) -> dict[str, Mapping[str, str]] | None:
        """The server's OWN current live digests, or ``None`` if none is wired.

        Derived only from the injected registry provider — never from a caller /
        client, so readiness cannot be spoofed from the browser.
        """
        if self._live_digests_provider is None:
            return None
        live = self._live_digests_provider()
        if not isinstance(live, Mapping):
            return None
        return {
            str(kind): value
            for kind, value in live.items()
            if isinstance(value, Mapping)
        }

    def resolve(self, actor: str, preset_id: str) -> dict[str, Any]:
        """Resolve a preset for selection against the SERVER-DERIVED live digests.

        The live digests come exclusively from the server's own registry provider,
        never from the caller, so a client cannot mark a drifted pin "ready".

        * ``{"status": "ready", "preset": <record>}`` when every pinned digest the
          server can verify still matches.
        * ``{"status": "repair_required", "preset_id", "drifted_refs": [...]}`` when
          a ref whose kind the server DOES know has genuinely drifted — NEVER a
          substituted route or tool.  The drifted set is the deterministic
          ``contracts._advanced_preset_drift`` map, filtered to verifiable kinds.
        * ``{"status": "unverifiable", "preset_id", "unverifiable_refs"?}`` when a
          referenced digest's kind is NOT in the server registry (the surface is
          not fully wired).  A missing digest is reported as unverifiable, never
          defaulted to drift and never asserted ready.
        """
        record = self.get(actor, preset_id)
        server_live = self._server_live_digests()
        if server_live is None:
            # No registry is wired: readiness cannot be verified server-side. Do
            # not assert ready, do not fabricate drift, do not substitute.
            return {
                "status": "unverifiable",
                "preset_id": preset_id,
                "reason": "live_digests_unavailable",
            }
        drift = _advanced_preset_drift(record, server_live)
        # A ref-kind the server registry carries at all is VERIFIABLE; a kind it
        # does not carry is unverifiable (surface not fully configured), so its
        # missing digest is reported as such rather than defaulted to drift.
        verifiable_kinds = set(server_live)
        real_drift = {ref: pinned for ref, pinned in drift.items() if ref[0] in verifiable_kinds}
        unverifiable = {ref: pinned for ref, pinned in drift.items() if ref[0] not in verifiable_kinds}
        if real_drift:
            drifted_refs = [
                {"ref_kind": ref_kind, "id": ref_id, "pinned_digest": pinned}
                for (ref_kind, ref_id), pinned in sorted(real_drift.items())
            ]
            return {
                "status": "repair_required",
                "preset_id": preset_id,
                "drifted_refs": drifted_refs,
            }
        if unverifiable:
            unverifiable_refs = [
                {"ref_kind": ref_kind, "id": ref_id, "pinned_digest": pinned}
                for (ref_kind, ref_id), pinned in sorted(unverifiable.items())
            ]
            return {
                "status": "unverifiable",
                "preset_id": preset_id,
                "reason": "live_digests_unavailable",
                "unverifiable_refs": unverifiable_refs,
            }
        return {"status": "ready", "preset": record}

    def delete(self, actor: str, preset_id: str) -> None:
        with self._lock:
            presets = self._by_actor.get(actor, {})
            if preset_id not in presets:
                raise AdvancedPlaygroundNotFound(UNKNOWN_ITEM_DETAIL)
            del presets[preset_id]

    def export(self, actor: str) -> dict[str, Any]:
        """A CLOSED, size-bounded, redaction-enveloped export of the actor's presets.

        The presets are already digest-pinned, prose-only records (no credential,
        endpoint, path, or raw prompt is representable), and the envelope records a
        SAFE OPAQUE actor reference, never the raw actor identity.  The router
        scrubs the serialized body at the last hop.
        """
        presets = [_scrub_export(_strip_identity(record)) for record in self.list(actor)]
        envelope = {
            "schema_version": PRESET_EXPORT_SCHEMA_VERSION,
            "source": {"scope": "personal", "actor_ref": opaque_scope_ref("actor", actor, key=self._audit_key)},
            "presets": presets,
        }
        return _bounded(envelope)


_PRESET_EXPORT_KEYS = frozenset({"schema_version", "source", "presets"})


def validate_preset_export_envelope(envelope: Any) -> dict[str, Any]:
    """Return a preset export only if it is a CLOSED, versioned, enveloped export.

    Fail closed on a non-object, an unknown top-level key, a wrong/absent
    ``schema_version`` (a bare preset list without the redaction envelope is NOT a
    valid export), or a non-list ``presets``.
    """
    return _validate_export_envelope(
        envelope, PRESET_EXPORT_SCHEMA_VERSION, _PRESET_EXPORT_KEYS, "presets", "preset"
    )


# --------------------------------------------------------------------------- #
# Templates (T009)
# --------------------------------------------------------------------------- #


class AdvancedTemplateStore:
    """An actor-private durable store of digest-pinned instruction templates.

    A template's ``body`` text and declared ``substitutions`` are DECLARED,
    inspectable instructions.  :meth:`resolve` recomputes drift against the live
    template digests at SELECTION time; a drifted or REMOVED template opens REPAIR
    mode, never a silent substitution.  :meth:`declared_instructions` renders the
    full text + the declared bindings for pre-send display and records them as
    declared run instructions — a value bound to an undeclared name is refused, so
    no hidden binding can shadow a declared one.
    """

    def __init__(self, *, audit_key: bytes) -> None:
        self._audit_key = require_pref_audit_key(audit_key)
        self._lock = threading.RLock()
        self._by_actor: dict[str, dict[str, dict[str, Any]]] = {}

    def save(self, actor: str, template: Mapping[str, Any]) -> dict[str, Any]:
        record = copy.deepcopy(dict(template))
        try:
            validate_advanced_template(record)
        except ContractValidationError as exc:
            raise AdvancedPlaygroundError(f"template is not valid: {exc}") from exc
        template_id = str(record.get("template_id"))
        with self._lock:
            templates = self._by_actor.setdefault(actor, {})
            if template_id not in templates and len(templates) >= MAX_ITEMS_PER_ACTOR:
                raise AdvancedPlaygroundError("template limit reached for this actor")
            templates[template_id] = record
        return copy.deepcopy(record)

    def list(self, actor: str) -> list[dict[str, Any]]:
        with self._lock:
            templates = self._by_actor.get(actor, {})
            return [copy.deepcopy(templates[tid]) for tid in sorted(templates)]

    def get(self, actor: str, template_id: str) -> dict[str, Any]:
        with self._lock:
            templates = self._by_actor.get(actor, {})
            record = templates.get(template_id)
            if record is None:
                raise AdvancedPlaygroundNotFound(UNKNOWN_ITEM_DETAIL)
            return copy.deepcopy(record)

    def live_digests(self, actor: str) -> dict[str, str]:
        """The actor's currently-stored (template_id -> template_digest) map."""
        with self._lock:
            templates = self._by_actor.get(actor, {})
            return {tid: str(rec.get("template_digest")) for tid, rec in templates.items()}

    def delete(self, actor: str, template_id: str) -> None:
        with self._lock:
            templates = self._by_actor.get(actor, {})
            if template_id not in templates:
                raise AdvancedPlaygroundNotFound(UNKNOWN_ITEM_DETAIL)
            del templates[template_id]

    def resolve(
        self, actor: str, template_id: str, pinned_digest: str, live_digests: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a pinned template reference against the CURRENT live digests.

        ``pinned_digest`` is the digest a preset (or a prior selection) pinned.  A
        template that is REMOVED (no live record) or whose live digest DIFFERS from
        the pin opens REPAIR mode; otherwise the stored, undrifted template is
        returned for declared-instruction rendering.  Never a silent substitution.
        """
        live = dict(live_digests) if live_digests is not None else self.live_digests(actor)
        current = live.get(template_id)
        if current is None or current != pinned_digest:
            return {
                "status": "repair_required",
                "template_id": template_id,
                "drifted_refs": [
                    {"ref_kind": "template", "id": template_id, "pinned_digest": pinned_digest}
                ],
                "reason": "removed" if current is None else "digest_drift",
            }
        return {"status": "ready", "template": self.get(actor, template_id)}

    def declared_instructions(
        self, actor: str, template_id: str, bindings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render the template as DECLARED, pre-send-visible run instructions.

        Returns the full rendered text plus the ordered declared substitution
        bindings so the operator sees exactly what will be sent — the template text
        becomes declared, inspectable instructions, never a covert injected prompt.
        A value bound to a name the template does not declare is refused.
        """
        record = self.get(actor, template_id)
        declared_names = [
            str(entry.get("name"))
            for entry in record.get("substitutions", [])
            if isinstance(entry, Mapping)
        ]
        supplied = dict(bindings or {})
        undeclared = sorted(set(supplied) - set(declared_names))
        if undeclared:
            raise AdvancedPlaygroundError(
                f"substitution names not declared by the template: {undeclared}"
            )
        text = str(record.get("body", {}).get("text", ""))
        resolved = text
        binding_view: list[dict[str, Any]] = []
        for name in declared_names:
            value = supplied.get(name, "")
            resolved = resolved.replace("{{" + name + "}}", str(value))
            binding_view.append({"name": name, "value": str(value)})
        return {
            "content_trust": "untrusted_task_data",
            "provenance": "declared",
            "template_id": template_id,
            "template_digest": str(record.get("template_digest")),
            "text": resolved,
            "substitutions": binding_view,
        }

    def export(self, actor: str) -> dict[str, Any]:
        templates = [_scrub_export(_strip_identity(record)) for record in self.list(actor)]
        envelope = {
            "schema_version": TEMPLATE_EXPORT_SCHEMA_VERSION,
            "source": {"scope": "personal", "actor_ref": opaque_scope_ref("actor", actor, key=self._audit_key)},
            "templates": templates,
        }
        return _bounded(envelope)


_TEMPLATE_EXPORT_KEYS = frozenset({"schema_version", "source", "templates"})


def validate_template_export_envelope(envelope: Any) -> dict[str, Any]:
    """Return a template export only if it is a CLOSED, versioned, enveloped export."""
    return _validate_export_envelope(
        envelope, TEMPLATE_EXPORT_SCHEMA_VERSION, _TEMPLATE_EXPORT_KEYS, "templates", "template"
    )


# --------------------------------------------------------------------------- #
# Ratings (T010)
# --------------------------------------------------------------------------- #


class AdvancedRatingStore:
    """An actor-private durable store of declared-criterion route preferences.

    A rating MUST name a criterion from :data:`DECLARED_RATING_CRITERIA`; a rating
    with no criterion, or an undeclared one, is refused (no free-text ungrounded
    rating).  Aggregates are per (route, criterion) with a ``non_qualification``
    label; they are structurally absent from every delivery/qualification surface.
    Export ranges only over the actor's ratings and REQUIRES the redaction
    envelope; a raw actor identity never appears.
    """

    def __init__(self, *, audit_key: bytes) -> None:
        self._audit_key = require_pref_audit_key(audit_key)
        self._lock = threading.RLock()
        # actor -> list of rating records
        self._by_actor: dict[str, list[dict[str, Any]]] = {}

    def record(
        self, actor: str, *, route_id: str, criterion_id: str, score: int, note: str | None = None,
    ) -> dict[str, Any]:
        """Record an actor-local, non-qualification rating; return the stored record.

        Refuses a rating that names no declared criterion or an out-of-range score.
        """
        route = _require_route_id(route_id)
        if not criterion_id or criterion_id not in DECLARED_RATING_CRITERIA:
            raise AdvancedPlaygroundError(
                "a rating must name a declared evaluation criterion"
            )
        if not isinstance(score, int) or isinstance(score, bool):
            raise AdvancedPlaygroundError("a rating score must be an integer")
        if not (MIN_RATING_SCORE <= score <= MAX_RATING_SCORE):
            raise AdvancedPlaygroundError(
                f"a rating score must be within [{MIN_RATING_SCORE}, {MAX_RATING_SCORE}]"
            )
        record = {
            "rating_id": new_id("advrating"),
            "route_id": route,
            "criterion_id": criterion_id,
            "criterion_label": {
                "content_trust": "untrusted_task_data",
                "text": DECLARED_RATING_CRITERIA[criterion_id],
            },
            "score": score,
            "non_qualification": True,
            "created_at": now_utc().isoformat(),
        }
        if note is not None:
            record["note"] = {"content_trust": "untrusted_task_data", "text": str(note)[:200]}
        with self._lock:
            ratings = self._by_actor.setdefault(actor, [])
            if len(ratings) >= MAX_ITEMS_PER_ACTOR:
                raise AdvancedPlaygroundError("rating limit reached for this actor")
            ratings.append(record)
        return copy.deepcopy(record)

    def list(self, actor: str) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(r) for r in self._by_actor.get(actor, [])]

    def aggregates(self, actor: str) -> dict[str, Any]:
        """Per (route, criterion) factual aggregates, each non-qualification labelled.

        Every aggregate carries ``non_qualification: true`` and a normative label,
        so a rating aggregate can never be read as model qualification or delivery
        evidence.  The average is an INTEGER milli-score (no float in a
        digest/JSON-safe surface).
        """
        with self._lock:
            ratings = list(self._by_actor.get(actor, []))
        buckets: dict[tuple[str, str], list[int]] = {}
        for r in ratings:
            buckets.setdefault((str(r["route_id"]), str(r["criterion_id"])), []).append(int(r["score"]))
        rows: list[dict[str, Any]] = []
        for (route_id, criterion_id), scores in sorted(buckets.items()):
            total = sum(scores)
            count = len(scores)
            rows.append({
                "route_id": route_id,
                "criterion_id": criterion_id,
                "criterion_label": {
                    "content_trust": "untrusted_task_data",
                    "text": DECLARED_RATING_CRITERIA.get(criterion_id, criterion_id),
                },
                "count": count,
                "score_total": total,
                "average_score_milli": round(total * 1000 / count),
                "score_min": min(scores),
                "score_max": max(scores),
                "non_qualification": True,
            })
        return {
            "non_qualification": True,
            "disclaimer": {
                "content_trust": "untrusted_task_data",
                "text": "Informal preference evidence only — never model qualification or delivery evidence.",
            },
            "aggregates": rows,
        }

    def export(self, actor: str) -> dict[str, Any]:
        ratings = [_scrub_export(record) for record in self.list(actor)]
        envelope = {
            "schema_version": RATING_EXPORT_SCHEMA_VERSION,
            "non_qualification": True,
            "source": {"scope": "personal", "actor_ref": opaque_scope_ref("actor", actor, key=self._audit_key)},
            "ratings": ratings,
        }
        return _bounded(envelope)


_RATING_EXPORT_KEYS = frozenset({"schema_version", "non_qualification", "source", "ratings"})


def validate_rating_export_envelope(envelope: Any) -> dict[str, Any]:
    """Return a rating export only if it is a CLOSED, versioned, enveloped export.

    A bare rating list WITHOUT the redaction envelope (schema_version + opaque
    source) is REJECTED — a rating never appears in an export lacking the envelope.
    """
    validated = _validate_export_envelope(
        envelope, RATING_EXPORT_SCHEMA_VERSION, _RATING_EXPORT_KEYS, "ratings", "rating"
    )
    if validated.get("non_qualification") is not True:
        raise AdvancedPlaygroundError(
            "a rating export must carry the non_qualification label"
        )
    return validated


# --------------------------------------------------------------------------- #
# Comparison (T006) — factual build over the contract validator
# --------------------------------------------------------------------------- #


def build_comparison(comparison: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a FACTUAL comparison record.

    Delegates the closed, factual, criterion-bound rules to
    :func:`contracts.validate_advanced_comparison` (a ranking/winner requires a
    declared ``non_qualification`` criterion), so this surface can never emit an
    inferred winner or a merged/synthesized answer.
    """
    record = copy.deepcopy(dict(comparison))
    try:
        validate_advanced_comparison(record)
    except ContractValidationError as exc:
        raise AdvancedPlaygroundError(f"comparison is not valid: {exc}") from exc
    return record


def _validate_export_envelope(
    envelope: Any, schema_version: str, allowed_keys: frozenset[str], list_key: str, label: str,
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise AdvancedPlaygroundError(f"an advanced {label} export must be an object")
    extra = set(envelope) - allowed_keys
    if extra:
        raise AdvancedPlaygroundError(
            f"advanced {label} export has unsupported extension keys: {sorted(extra)}"
        )
    if envelope.get("schema_version") != schema_version:
        raise AdvancedPlaygroundError(
            f"advanced {label} export declares an unknown or unsupported schema_version"
        )
    items = envelope.get(list_key)
    if not isinstance(items, list):
        raise AdvancedPlaygroundError(f"advanced {label} export {list_key} must be a list")
    return dict(envelope)
