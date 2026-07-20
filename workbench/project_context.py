"""Derived, explicitly non-canonical project-context projection.

This module turns a validated :class:`~workbench.state_snapshot_adapter.PublishableSnapshot`
into a bounded *display* projection of one project's PRD, feature, and task
summaries.  It is the read-model the browser and model-facing context render
from: readable titles and typed source attribution, keyed by
``(project_id, source_kind, scoped_id, source_digest)``.

Authority boundary (AGENTS.md): Anvil State remains canonical for project
lifecycle.  This projection is a *display* derivative and never a source of
authority, so every serialized summary and the projection itself carry an
explicit ``non_canonical: true`` / ``canonical: false`` marker — a reader can
never mistake a rendered summary for canonical State.  Consuming a summary
grants no claim, lease, evidence, or effect.

What the shapes deliberately cannot carry (fail-closed by construction):

* No State storage detail.  These frozen values have a fixed field set (the
  dataclass is the ``additionalProperties: false`` closure); it never opens,
  copies, or mutates canonical State storage and no field is named for, or
  shaped to hold, a State database file, a State workspace directory, a local
  filesystem path, or any database-engine sibling.  Identifier fields are
  pattern-validated to the snapshot's slash-free grammars, so an id can never
  smuggle a path separator.
* No credential.  No field carries a token, secret, api key, or provider
  endpoint/URL.
* No raw executable provider payload.  No field carries an operation schema,
  argv, command string, bridge adapter name, or route — only readable prose and
  typed source attribution (revision + snapshot digest).

Every prose field is ``content_trust = "untrusted_task_data"``: readable for
display, never a control instruction.  The projection is derived from an
already-validated snapshot; it re-opens no State storage and invents no field
the snapshot lacks (feature summaries carry only what the snapshot's task
``feature_id`` references and owning-PRD revision actually establish).

The projection is a hub-side display derivative, not a contract-digest-bearing
resource: it pins and echoes the source snapshot's ``snapshot_digest`` as its
source attribution rather than minting a new authority digest of its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .redaction import redact_text
from .state_snapshot_adapter import PublishableSnapshot

PROJECT_CONTEXT_SCHEMA_VERSION = "workbench-project-context/v1"

#: The four source-record kinds this projection can attribute a summary to.
SOURCE_KINDS = frozenset({"prd", "plan", "feature", "task"})

#: Every readable prose field is untrusted task data, never a control instruction.
CONTENT_TRUST = "untrusted_task_data"

MAX_TITLE_CHARS = 500
MAX_PROJECT_NAME_CHARS = 200
MAX_TARGET_VERSION_CHARS = 100

_PROJECT_ID = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_PRD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TASK_SCOPED_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}:T[0-9]{3}(\.[0-9]{1,3})?$")
_FEATURE_ID = re.compile(r"^[a-zA-Z0-9._:-]{1,128}$")
_STATUS_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class ProjectContextError(ValueError):
    """A project-context projection would violate its display/attribution contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProjectContextError(message)


def _bounded_prose(value: Any, limit: int, label: str) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= limit, f"{label} must be bounded readable text")
    # Defense-in-depth for the last display hop before the browser/model
    # context: even though the snapshot is already redacted upstream, scrub any
    # secret this display read-model might otherwise pass through verbatim.
    return redact_text(value)


def _revision(value: Any, label: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 1, f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class PrdSummary:
    """One readable PRD display summary attributed to its source revision/digest."""

    project_id: str
    scoped_id: str
    title: str
    status: str
    source_revision: int
    source_digest: str
    target_version: str | None = None
    content_trust: str = CONTENT_TRUST
    source_kind: str = "prd"
    non_canonical: bool = True

    def __post_init__(self) -> None:
        _require(bool(_PROJECT_ID.match(str(self.project_id))), "prd summary project_id is invalid")
        _require(bool(_PRD_ID.match(str(self.scoped_id))), "prd summary scoped_id is invalid")
        object.__setattr__(self, "title", _bounded_prose(self.title, MAX_TITLE_CHARS, "prd summary title"))
        _require(bool(_STATUS_TOKEN.match(str(self.status))), "prd summary status is invalid")
        _revision(self.source_revision, "prd summary source_revision")
        _require(bool(_DIGEST.match(str(self.source_digest))), "prd summary source_digest is invalid")
        if self.target_version is not None:
            object.__setattr__(self, "target_version", _bounded_prose(self.target_version, MAX_TARGET_VERSION_CHARS, "prd summary target_version"))
        _require(self.content_trust == CONTENT_TRUST, "prd summary prose is always untrusted task data")
        _require(self.source_kind == "prd", "prd summary source_kind must be 'prd'")
        _require(self.non_canonical is True, "a display summary is always non-canonical")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.project_id, self.source_kind, self.scoped_id, self.source_digest)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "content_trust": self.content_trust,
            "non_canonical": self.non_canonical,
            "project_id": self.project_id,
            "scoped_id": self.scoped_id,
            "source_digest": self.source_digest,
            "source_kind": self.source_kind,
            "source_revision": self.source_revision,
            "status": self.status,
            "title": self.title,
        }
        if self.target_version is not None:
            data["target_version"] = self.target_version
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PrdSummary":
        allowed = {
            "content_trust", "non_canonical", "project_id", "scoped_id", "source_digest",
            "source_kind", "source_revision", "status", "title", "target_version",
        }
        _reject_unknown(data, allowed, "prd summary")
        return cls(
            project_id=str(data["project_id"]),
            scoped_id=str(data["scoped_id"]),
            title=str(data["title"]),
            status=str(data["status"]),
            source_revision=data["source_revision"],
            source_digest=str(data["source_digest"]),
            target_version=str(data["target_version"]) if data.get("target_version") is not None else None,
            content_trust=str(data.get("content_trust", CONTENT_TRUST)),
            source_kind=str(data.get("source_kind", "prd")),
            non_canonical=bool(data.get("non_canonical", True)),
        )


@dataclass(frozen=True)
class FeatureSummary:
    """A feature grouping derived from task ``feature_id`` references.

    The snapshot carries no first-class feature record, so this summary invents
    nothing: it echoes the readable ``feature_id`` as its scoped identifier,
    counts the member tasks, and attributes the owning PRD's revision plus the
    source snapshot digest.  It has no free title because the snapshot has none.
    """

    project_id: str
    scoped_id: str
    owning_prd_id: str
    task_count: int
    source_revision: int
    source_digest: str
    source_kind: str = "feature"
    non_canonical: bool = True

    def __post_init__(self) -> None:
        _require(bool(_PROJECT_ID.match(str(self.project_id))), "feature summary project_id is invalid")
        _require(bool(_FEATURE_ID.match(str(self.scoped_id))), "feature summary scoped_id is invalid")
        _require(bool(_PRD_ID.match(str(self.owning_prd_id))), "feature summary owning_prd_id is invalid")
        _require(
            isinstance(self.task_count, int) and not isinstance(self.task_count, bool) and self.task_count >= 1,
            "feature summary task_count must be a positive integer",
        )
        _revision(self.source_revision, "feature summary source_revision")
        _require(bool(_DIGEST.match(str(self.source_digest))), "feature summary source_digest is invalid")
        _require(self.source_kind == "feature", "feature summary source_kind must be 'feature'")
        _require(self.non_canonical is True, "a display summary is always non-canonical")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.project_id, self.source_kind, self.scoped_id, self.source_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "non_canonical": self.non_canonical,
            "owning_prd_id": self.owning_prd_id,
            "project_id": self.project_id,
            "scoped_id": self.scoped_id,
            "source_digest": self.source_digest,
            "source_kind": self.source_kind,
            "source_revision": self.source_revision,
            "task_count": self.task_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FeatureSummary":
        allowed = {
            "non_canonical", "owning_prd_id", "project_id", "scoped_id",
            "source_digest", "source_kind", "source_revision", "task_count",
        }
        _reject_unknown(data, allowed, "feature summary")
        return cls(
            project_id=str(data["project_id"]),
            scoped_id=str(data["scoped_id"]),
            owning_prd_id=str(data["owning_prd_id"]),
            task_count=data["task_count"],
            source_revision=data["source_revision"],
            source_digest=str(data["source_digest"]),
            source_kind=str(data.get("source_kind", "feature")),
            non_canonical=bool(data.get("non_canonical", True)),
        )


@dataclass(frozen=True)
class TaskSummary:
    """One readable task display summary attributed to its owning PRD revision/digest."""

    project_id: str
    scoped_id: str
    title: str
    status: str
    owning_prd_id: str
    source_revision: int
    source_digest: str
    priority: str | None = None
    feature_id: str | None = None
    content_trust: str = CONTENT_TRUST
    source_kind: str = "task"
    non_canonical: bool = True

    def __post_init__(self) -> None:
        _require(bool(_PROJECT_ID.match(str(self.project_id))), "task summary project_id is invalid")
        _require(bool(_TASK_SCOPED_ID.match(str(self.scoped_id))), "task summary scoped_id is invalid")
        object.__setattr__(self, "title", _bounded_prose(self.title, MAX_TITLE_CHARS, "task summary title"))
        _require(bool(_STATUS_TOKEN.match(str(self.status))), "task summary status is invalid")
        _require(bool(_PRD_ID.match(str(self.owning_prd_id))), "task summary owning_prd_id is invalid")
        _require(
            self.scoped_id.split(":", 1)[0] == self.owning_prd_id,
            "task summary scoped_id must be owned by owning_prd_id",
        )
        _revision(self.source_revision, "task summary source_revision")
        _require(bool(_DIGEST.match(str(self.source_digest))), "task summary source_digest is invalid")
        if self.priority is not None:
            _require(bool(_STATUS_TOKEN.match(str(self.priority))), "task summary priority is invalid")
        if self.feature_id is not None:
            _require(bool(_FEATURE_ID.match(str(self.feature_id))), "task summary feature_id is invalid")
        _require(self.content_trust == CONTENT_TRUST, "task summary prose is always untrusted task data")
        _require(self.source_kind == "task", "task summary source_kind must be 'task'")
        _require(self.non_canonical is True, "a display summary is always non-canonical")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.project_id, self.source_kind, self.scoped_id, self.source_digest)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "content_trust": self.content_trust,
            "non_canonical": self.non_canonical,
            "owning_prd_id": self.owning_prd_id,
            "project_id": self.project_id,
            "scoped_id": self.scoped_id,
            "source_digest": self.source_digest,
            "source_kind": self.source_kind,
            "source_revision": self.source_revision,
            "status": self.status,
            "title": self.title,
        }
        if self.priority is not None:
            data["priority"] = self.priority
        if self.feature_id is not None:
            data["feature_id"] = self.feature_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSummary":
        allowed = {
            "content_trust", "non_canonical", "owning_prd_id", "project_id", "scoped_id",
            "source_digest", "source_kind", "source_revision", "status", "title",
            "priority", "feature_id",
        }
        _reject_unknown(data, allowed, "task summary")
        return cls(
            project_id=str(data["project_id"]),
            scoped_id=str(data["scoped_id"]),
            title=str(data["title"]),
            status=str(data["status"]),
            owning_prd_id=str(data["owning_prd_id"]),
            source_revision=data["source_revision"],
            source_digest=str(data["source_digest"]),
            priority=str(data["priority"]) if data.get("priority") is not None else None,
            feature_id=str(data["feature_id"]) if data.get("feature_id") is not None else None,
            content_trust=str(data.get("content_trust", CONTENT_TRUST)),
            source_kind=str(data.get("source_kind", "task")),
            non_canonical=bool(data.get("non_canonical", True)),
        )


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    _require(isinstance(data, Mapping), f"{label} must be an object")
    unknown = set(data) - allowed
    _require(not unknown, f"{label} carries undeclared fields: {sorted(unknown)}")


@dataclass(frozen=True)
class ProjectContextProjection:
    """A bounded, explicitly non-canonical display projection of one project.

    Composed of typed display summaries keyed by
    ``(project_id, source_kind, scoped_id, source_digest)``.  The projection and
    every summary carry an explicit non-canonical marker; ``canonical`` is
    always ``False`` because Anvil State, not this read model, is canonical.
    """

    schema_version: str
    source_provider: str
    source_schema_version: str
    source_digest: str
    project_id: str
    project_name: str
    prds: tuple[PrdSummary, ...]
    features: tuple[FeatureSummary, ...]
    tasks: tuple[TaskSummary, ...]
    canonical: bool = False
    non_canonical: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "prds", tuple(self.prds))
        object.__setattr__(self, "features", tuple(self.features))
        object.__setattr__(self, "tasks", tuple(self.tasks))
        _require(self.schema_version == PROJECT_CONTEXT_SCHEMA_VERSION, "projection schema_version is unexpected")
        _require(isinstance(self.source_provider, str) and bool(self.source_provider), "projection source_provider is invalid")
        _require(
            isinstance(self.source_schema_version, str) and bool(self.source_schema_version),
            "projection source_schema_version is invalid",
        )
        _require(bool(_DIGEST.match(str(self.source_digest))), "projection source_digest is invalid")
        _require(bool(_PROJECT_ID.match(str(self.project_id))), "projection project_id is invalid")
        object.__setattr__(self, "project_name", _bounded_prose(self.project_name, MAX_PROJECT_NAME_CHARS, "projection project_name"))
        # The load-bearing authority invariant: this read model is NEVER canonical.
        _require(self.canonical is False, "a project-context projection is never canonical")
        _require(self.non_canonical is True, "a project-context projection must mark itself non-canonical")
        for summary in (*self.prds, *self.features, *self.tasks):
            _require(
                isinstance(summary, (PrdSummary, FeatureSummary, TaskSummary)),
                "projection summaries must be typed display summaries",
            )
            _require(
                summary.project_id == self.project_id,
                "every summary must be owned by the projection's project",
            )
            _require(
                summary.source_digest == self.source_digest,
                "every summary must be attributed to the projection's source digest",
            )

    @property
    def summary_keys(self) -> tuple[tuple[str, str, str, str], ...]:
        return tuple(summary.key for summary in (*self.prds, *self.features, *self.tasks))

    def as_dict(self) -> dict[str, Any]:
        """Deterministic display serialization; round-trips via :meth:`from_dict`."""
        return {
            "canonical": self.canonical,
            "non_canonical": self.non_canonical,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "schema_version": self.schema_version,
            "source_digest": self.source_digest,
            "source_provider": self.source_provider,
            "source_schema_version": self.source_schema_version,
            "prds": [summary.as_dict() for summary in self.prds],
            "features": [summary.as_dict() for summary in self.features],
            "tasks": [summary.as_dict() for summary in self.tasks],
        }

    #: Back-compat / intent alias: this projection is display-only.
    to_display_dict = as_dict

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectContextProjection":
        allowed = {
            "canonical", "non_canonical", "project_id", "project_name", "schema_version",
            "source_digest", "source_provider", "source_schema_version", "prds", "features", "tasks",
        }
        _reject_unknown(data, allowed, "project-context projection")
        for field_name in ("prds", "features", "tasks"):
            _require(isinstance(data.get(field_name), list), f"projection {field_name} must be a list")
        return cls(
            schema_version=str(data["schema_version"]),
            source_provider=str(data["source_provider"]),
            source_schema_version=str(data["source_schema_version"]),
            source_digest=str(data["source_digest"]),
            project_id=str(data["project_id"]),
            project_name=str(data["project_name"]),
            prds=tuple(PrdSummary.from_dict(item) for item in data["prds"]),
            features=tuple(FeatureSummary.from_dict(item) for item in data["features"]),
            tasks=tuple(TaskSummary.from_dict(item) for item in data["tasks"]),
            canonical=bool(data.get("canonical", False)),
            non_canonical=bool(data.get("non_canonical", True)),
        )

    @classmethod
    def from_snapshot(cls, snapshot: PublishableSnapshot) -> "ProjectContextProjection":
        """Derive the display projection from one validated publishable snapshot.

        Reads only the already-validated snapshot payload — it re-opens no State
        storage and invents no field the snapshot lacks.  Task and feature
        summaries inherit the revision of their owning PRD (resolved through the
        snapshot's typed task references), and every summary shares the source
        snapshot digest as its attribution.
        """
        _require(isinstance(snapshot, PublishableSnapshot), "from_snapshot requires a PublishableSnapshot")
        payload = snapshot.payload
        source_digest = snapshot.snapshot_digest
        project_id = snapshot.project_id
        project = payload["project"]

        prd_revisions: dict[str, int] = {}
        prds: list[PrdSummary] = []
        for prd in payload["prds"]:
            prd_id = str(prd["prd_id"])
            revision = int(prd["revision"])
            prd_revisions[prd_id] = revision
            prds.append(
                PrdSummary(
                    project_id=project_id,
                    scoped_id=prd_id,
                    title=str(prd["title"]),
                    status=str(prd["status"]),
                    source_revision=revision,
                    source_digest=source_digest,
                    target_version=str(prd["target_version"]) if prd.get("target_version") is not None else None,
                )
            )

        tasks: list[TaskSummary] = []
        # feature_id -> (owning prd_id, member task count)
        feature_owner: dict[str, str] = {}
        feature_counts: dict[str, int] = {}
        for task in payload["tasks"]:
            owning_prd_id = str(task["ref"]["prd_id"])
            _require(
                owning_prd_id in prd_revisions,
                f"snapshot task references a PRD absent from the snapshot: {owning_prd_id}",
            )
            feature_id = str(task["feature_id"]) if task.get("feature_id") is not None else None
            tasks.append(
                TaskSummary(
                    project_id=project_id,
                    scoped_id=str(task["scoped_id"]),
                    title=str(task["title"]),
                    status=str(task["status"]),
                    owning_prd_id=owning_prd_id,
                    source_revision=prd_revisions[owning_prd_id],
                    source_digest=source_digest,
                    priority=str(task["priority"]) if task.get("priority") is not None else None,
                    feature_id=feature_id,
                )
            )
            if feature_id is not None:
                existing = feature_owner.setdefault(feature_id, owning_prd_id)
                _require(
                    existing == owning_prd_id,
                    f"feature {feature_id} spans more than one owning PRD; cannot attribute a revision",
                )
                feature_counts[feature_id] = feature_counts.get(feature_id, 0) + 1

        features = [
            FeatureSummary(
                project_id=project_id,
                scoped_id=feature_id,
                owning_prd_id=feature_owner[feature_id],
                task_count=feature_counts[feature_id],
                source_revision=prd_revisions[feature_owner[feature_id]],
                source_digest=source_digest,
            )
            for feature_id in sorted(feature_owner)
        ]

        return cls(
            schema_version=PROJECT_CONTEXT_SCHEMA_VERSION,
            source_provider=str(payload["provider"]),
            source_schema_version=str(payload["schema_version"]),
            source_digest=source_digest,
            project_id=project_id,
            project_name=str(project["name"]),
            prds=tuple(prds),
            features=tuple(features),
            tasks=tuple(tasks),
        )
