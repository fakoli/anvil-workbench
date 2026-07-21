"""Secret-safe transcript and evidence redaction.

Workbench keeps full *redacted* transcripts.  This module deliberately runs
before content enters the hub database or the Neo4j projection.
"""
from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|ghp|gho|github_pat|hf|akia)[_-][A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}\b"),
)


def redact_text(value: str) -> str:
    """Return text with recognizable credentials replaced by one stable marker."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_value(value):
    """Recursively redact JSON-compatible transcript/evidence content."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    return value


#: Configuration/health prose is a strictly wider redaction domain than a
#: transcript: an integration descriptor's readable owner/remediation text must
#: never carry a raw endpoint URL or a local filesystem path either, because
#: both would leak deployment topology to the browser.  These patterns extend
#: :func:`redact_text` (which scrubs only recognizable credentials) with:
#:
#: * ``scheme://…`` URLs (``https``, ``bolt``, ``postgresql``, …) — the raw
#:   endpoint of any integration;
#: * Windows drive paths (``C:\\…`` / ``C:/…``);
#: * POSIX absolute paths (a whitespace- or line-anchored ``/…`` token).
#:
#: Deliberately NOT matched: version tokens such as ``schema/v1`` (no leading
#: slash, so the POSIX pattern cannot reach them) — those are pattern-validated
#: at the descriptor edge instead of scrubbed.
_URL_PATTERN = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://\S+")
_WINDOWS_PATH_PATTERN = re.compile(r"\b[A-Za-z]:[\\/]\S*")
_POSIX_PATH_PATTERN = re.compile(r"(?:(?<=\s)|^)/[^\s]+")

_REDACTED_URL = "[REDACTED-URL]"
_REDACTED_PATH = "[REDACTED-PATH]"


def redact_config_text(value: str) -> str:
    """Scrub credentials, raw URLs, and local paths from configuration prose.

    Runs the credential scrub of :func:`redact_text` first, then removes any
    ``scheme://…`` URL and any Windows or POSIX absolute path.  This is the
    last-hop scrub for observational configuration/health descriptors: readable
    owner and remediation text stays, but a deployment endpoint or filesystem
    path can never ride out to the browser inside it.
    """
    redacted = redact_text(value)
    redacted = _URL_PATTERN.sub(_REDACTED_URL, redacted)
    redacted = _WINDOWS_PATH_PATTERN.sub(_REDACTED_PATH, redacted)
    redacted = _POSIX_PATH_PATTERN.sub(_REDACTED_PATH, redacted)
    return redacted

