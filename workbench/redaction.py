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

