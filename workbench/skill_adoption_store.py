"""Owner skill-digest adoption ledger (reviewed-tools-plugins: T008).

Extracted verbatim from ``workbench.store``; re-exported there for backward
compatibility.
"""
from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime

from .models import now_utc
from .redaction import redact_config_text
from .store_base import StoreError


# ---------------------------------------------------------------------------
# Owner skill-digest adoption ledger (reviewed-tools-plugins: T008)
# ---------------------------------------------------------------------------


class SkillAdoptionStoreError(StoreError):
    """A skill acknowledgment violated its safe-metadata or digest contract."""


_ADOPTION_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_ADOPTION_CONTENT_SHA = re.compile(r"^[a-f0-9]{64}$")
_ADOPTION_MAX_DESCRIPTION = 500


@dataclass(frozen=True)
class SkillAdoptionRecord:
    """A durable owner acknowledgment that a skill was reviewed AT ONE digest.

    Carries the skill id, the exact acknowledged ``sha256:`` digest, and SAFE
    metadata only -- a short scrubbed description and the bare content hash.
    There is deliberately NO ``instructions``/body field and NO local ``path``
    field: a skill body or filesystem path is not representable in the record,
    so neither can ever enter the acknowledgment ledger or a browser projection
    built from it (T008: records carry digest + safe metadata only).
    """

    skill_id: str
    digest: str
    description: str
    content_sha256: str
    acknowledged_by: str
    acknowledged_at: datetime

    def metadata(self) -> dict[str, str]:
        """Digest + safe metadata projection -- never a body or a path."""
        return {
            "skill_id": self.skill_id,
            "digest": self.digest,
            "description": self.description,
            "content_sha256": self.content_sha256,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at.isoformat(),
        }


@dataclass
class SkillAdoptionRows:
    """The persisted acknowledgment container, keyed by skill id.

    Exactly one acknowledgment per skill id pins the exact digest the owner
    reviewed; a fresh acknowledgment of a different digest replaces it.  Handing
    the same rows to a fresh :class:`MemorySkillAdoptionStore` simulates a hub
    restart over the same ledger.
    """

    records: dict[str, SkillAdoptionRecord] = field(default_factory=dict)


class MemorySkillAdoptionStore:
    """Hermetic owner acknowledgment ledger for reviewed skill digests (T008).

    One acknowledgment record per skill id pins the EXACT digest the owner
    reviewed.  Acknowledging one digest never implicitly acknowledges a later
    change: a query for a different digest reports ``digest_changed`` (a prior
    acknowledgment exists at another digest) or ``unacknowledged`` (no
    acknowledgment at all), so a changed skill body always requires a fresh
    acknowledgment.  Every stored record and every projection carries the digest
    and SAFE metadata only -- never a skill body or a local path; a description
    is scrubbed through :func:`~workbench.redaction.redact_config_text` and
    bounded before it is stored, so even a mis-supplied description cannot ferry
    a path or a credential into the ledger.
    """

    def __init__(self, rows: SkillAdoptionRows | None = None) -> None:
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else SkillAdoptionRows()

    def acknowledge(
        self,
        skill_id: str,
        digest: str,
        *,
        description: str = "",
        content_sha256: str = "",
        acknowledged_by: str = "operator",
    ) -> SkillAdoptionRecord:
        """Acknowledge one skill at one exact reviewed digest, storing safe metadata."""
        with self._lock:
            skill_id = str(skill_id)
            digest = str(digest)
            if not skill_id:
                raise SkillAdoptionStoreError("a skill acknowledgment requires a skill id")
            if not _ADOPTION_DIGEST.fullmatch(digest):
                raise SkillAdoptionStoreError("a skill acknowledgment requires a sha256: digest")
            content_sha256 = str(content_sha256)
            if content_sha256 and not _ADOPTION_CONTENT_SHA.fullmatch(content_sha256):
                raise SkillAdoptionStoreError("a skill acknowledgment content hash must be a bare sha256 hex")
            record = SkillAdoptionRecord(
                skill_id=skill_id,
                digest=digest,
                # Scrub + bound the description so a body/path/credential shape can
                # never enter the ledger even through a mis-supplied field.
                description=redact_config_text(str(description)).strip()[:_ADOPTION_MAX_DESCRIPTION],
                content_sha256=content_sha256,
                acknowledged_by=str(acknowledged_by),
                acknowledged_at=now_utc(),
            )
            self.rows.records[skill_id] = record
            return record

    def acknowledgment_status(self, skill_id: str, digest: str) -> str:
        """Return ``acknowledged`` / ``digest_changed`` / ``unacknowledged``.

        ``digest_changed`` means a prior acknowledgment exists for the skill but
        at a DIFFERENT digest (a re-acknowledgment is required); ``unacknowledged``
        means no acknowledgment exists for the skill at all.
        """
        with self._lock:
            record = self.rows.records.get(str(skill_id))
            if record is None:
                return "unacknowledged"
            if not secrets.compare_digest(record.digest, str(digest)):
                return "digest_changed"
            return "acknowledged"

    def is_acknowledged(self, skill_id: str, digest: str) -> bool:
        return self.acknowledgment_status(skill_id, digest) == "acknowledged"

    def get(self, skill_id: str) -> dict[str, str] | None:
        """The safe-metadata projection for one skill's acknowledgment, or None."""
        with self._lock:
            record = self.rows.records.get(str(skill_id))
            return record.metadata() if record is not None else None

    def list_acknowledgments(self) -> list[dict[str, str]]:
        """Every acknowledgment's safe-metadata projection, id-sorted."""
        with self._lock:
            return [self.rows.records[key].metadata() for key in sorted(self.rows.records)]
