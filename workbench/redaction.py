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
#: never carry a secret, a raw endpoint URL, or a local filesystem path either,
#: because any of them would leak a credential or deployment topology to the
#: browser.  :func:`redact_config_text` removes all four declared classes
#: (T003.1 criterion 2):
#:
#: 1. **Credentials/secrets** beyond the transcript credential scrub — a bare
#:    AWS access key (``AKIA…`` with no separator), a compound-named key/secret/
#:    token/password assignment (``aws_secret_access_key=…``), a JWT
#:    (``eyJ…``), and a PEM private-key / certificate block.
#: 2. **Sensitive raw URLs / endpoints** — ``scheme://…`` URLs, a
#:    protocol-relative ``//host``, a bare IPv4 (optionally ``:port``), a
#:    ``host:port`` pair, a Tailscale ``*.ts.net`` host, and a DB
#:    connection-string ``Server=/Host=/User Id=`` field.
#: 3. **Local paths** — Windows drive paths (``C:\\…`` / ``C:/…``), UNC paths
#:    (``\\\\server\\share``), home paths (``~/…``), absolute POSIX paths
#:    regardless of the preceding delimiter (``path=/etc/…``, ``file:/var/…``,
#:    ``(/opt/…``), and relative/bare paths ending in a sensitive extension
#:    (``deploy/.env``, ``certs/server.pem``, ``prod.env``, ``backup.pem``).
#:
#: Deliberately NOT matched: version tokens such as ``schema/v1`` — the absolute
#: POSIX pattern requires the leading ``/`` to be preceded by a non-alphanumeric
#: delimiter, so a slash inside a word (``…-health/v1``) can never be reached;
#: those are pattern-validated at the descriptor edge instead of scrubbed.  This
#: keeps legitimate remediation prose (safe env-var names, ``retrieval/lineage``)
#: readable while closing every credential/endpoint/path shape.

# --- Credential/secret shapes the transcript credential scrub does not catch.
_CONFIG_SECRET_PATTERNS = (
    # AWS access key id: literal AKIA + 16 uppercase alphanumerics, *no*
    # separator (the transcript scrub demands a ``[_-]`` after the prefix, which
    # a real key never has).
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # A key/secret/token/password assignment whose identifier carries extra word
    # characters (``aws_secret_access_key=…``, ``client-secret: …``) — the
    # transcript scrub only recognizes a bare keyword, so a compound name slips
    # past it.
    re.compile(
        r"(?i)\b[a-z0-9_.\-]*(?:key|secret|token|password|passwd|pwd|credential)"
        r"[a-z0-9_.\-]*\s*[:=]\s*[^\s,;]+"
    ),
    # JWT: three base64url segments (header.payload[.signature]).
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?"),
    # PEM private-key / certificate block (may span lines).
    re.compile(r"-----BEGIN[A-Z0-9 ]*-----.*?-----END[A-Z0-9 ]*-----", re.DOTALL),
    # A stray PEM boundary marker even without its matching end.
    re.compile(r"-----(?:BEGIN|END)[A-Z0-9 ]*-----"),
)

# --- Sensitive raw URLs / endpoints.  Ordered so a scheme URL is consumed
# before the bare host/ip/path patterns can nibble at its tail.
_CONFIG_URL_PATTERNS = (
    # scheme://… (https, bolt, postgresql, …).
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://\S+"),
    # Protocol-relative //host — matched regardless of the preceding delimiter,
    # but never the ``//`` inside a real ``scheme://`` (guarded by the colon).
    re.compile(r"(?<![:/\w])//[A-Za-z0-9._~-]+(?:[:/][^\s]*)?"),
    # A DB connection-string host/user field (``Server=…``, ``Host=…``, ``Data
    # Source=…``, ``User Id=…``).  ``Password=…`` is a credential, caught above.
    re.compile(r"(?i)\b(?:server|host|data\s+source|datasource|user\s+id|uid|user)\s*=\s*[^;\s]+"),
    # A dotted hostname with an explicit port (``db.tail1234.ts.net:7687``,
    # ``100.64.0.5:8443``).
    re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]{2,}:\d{1,5}\b"),
    # A bare IPv4 address (optionally already ported above).
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    # A bare Tailscale tailnet host (``serving.tail1234.ts.net``).
    re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+ts\.net\b"),
    # A scheme-less SINGLE-LABEL host:port (``serving:8443``, ``neo4j:7687`` — a
    # tailnet compose service name).  The dotted host:port pattern above requires
    # a dot, so a dotless label:port slipped every free-text channel (proven by
    # three lanes: RTP, AMP, PTD).  Lowercase-anchored on purpose: an uppercase-T
    # ISO timestamp (``…T09:15``) and a scoped id (``release-alpha:T001``) start
    # the fractional/id part with ``T``/a digit-less token, not ``[a-z]`` before a
    # colon-then-digits, and a ``sha256:``+hex digest has no 2–5 digit run ending
    # on a word boundary — so none of them is over-redacted.
    re.compile(r"\b[a-z][a-z0-9-]*:\d{2,5}\b"),
)

# --- Local paths.
_CONFIG_PATH_PATTERNS = (
    # Windows drive path: C:\… or C:/…
    re.compile(r"\b[A-Za-z]:[\\/][^\s]*"),
    # UNC path: \\server\share…
    re.compile(r"\\\\[^\s\\]+(?:\\[^\s]*)?"),
    # Home path: ~/… or ~\…
    re.compile(r"~[\\/][^\s]*"),
    # Absolute POSIX path: a leading ``/`` NOT preceded by an alphanumeric, so
    # ``path=/etc/…``, ``file:/var/…`` and ``(/opt/…`` all match while a version
    # token like ``…-health/v1`` (slash after a letter) never does.
    re.compile(r"(?<![A-Za-z0-9])/[^\s]+"),
    # A relative or bare path ending in a sensitive extension.
    re.compile(
        r"(?i)\b[\w.\-/\\]*\.(?:env|pem|key|crt|cer|der|pfx|p12|jks|keystore|kdbx|ppk)\b"
    ),
)

_REDACTED_URL = "[REDACTED-URL]"
_REDACTED_PATH = "[REDACTED-PATH]"


def redact_config_text(value: str) -> str:
    """Scrub credentials, secrets, sensitive raw URLs, and local paths from prose.

    Runs the credential scrub of :func:`redact_text` first, then removes the
    extra credential/secret shapes, every sensitive raw URL/endpoint, and every
    local path (see the pattern groups above for the exact declared coverage).
    Readable owner and remediation text stays intact — a safe env-var name, a
    ``retrieval/lineage`` token, or a plain sentence is untouched — but a
    deployment secret, endpoint, or filesystem path can never ride out to the
    browser inside configuration/health prose.
    """
    redacted = redact_text(value)
    for pattern in _CONFIG_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    for pattern in _CONFIG_URL_PATTERNS:
        redacted = pattern.sub(_REDACTED_URL, redacted)
    for pattern in _CONFIG_PATH_PATTERNS:
        redacted = pattern.sub(_REDACTED_PATH, redacted)
    return redacted


def scrub_config_payload(value):
    """Recursively apply :func:`redact_config_text` to every string in a payload.

    This is the API last-hop guarantee for the observational system-health
    surface.  Whatever a descriptor or posture source returns, the serialized
    boundary itself is scrubbed here, so even a rogue, duck-typed service whose
    ``as_dict()`` bypassed construction-time scrubbing cannot emit a secret, a
    raw endpoint, or a local path through the router.  Non-string scalars (the
    booleans, the content digest, the RFC 3339 timestamp) pass through unchanged
    — the path/URL patterns are delimiter-anchored, so a ``sha256:…`` digest and
    a ``…/v1`` schema version survive intact.
    """
    if isinstance(value, str):
        return redact_config_text(value)
    if isinstance(value, dict):
        return {key: scrub_config_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_config_payload(item) for item in value]
    return value


#: A base64 media/audio payload the conversation transfer must never carry
#: (chat-first-voice:T012).  The chat contracts already make audio structurally
#: unrepresentable in a turn (a ``ContentBlock`` is text, a ``VoiceEvent`` is
#: metadata), so raw/synth audio can only appear if a caller smuggled it into a
#: content-text string.  This closes that free-text edge: a ``data:audio/…``
#: (or any ``data:…;base64,…``) media URI is stripped, mirroring the voice
#: relay's no-raw-audio / no-transcript-draft discipline.  Anchored on the
#: ``data:<type>/<subtype>;base64,`` shape so a bare ``sha256:``/version token
#: or an ordinary colon in prose is never over-matched.
_MEDIA_DATA_URI_PATTERN = re.compile(
    r"(?i)\bdata:[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*(?:;[a-z0-9.+=-]+)*;base64,[A-Za-z0-9+/=]+"
)
_REDACTED_AUDIO = "[REDACTED-AUDIO]"


def redact_conversation_text(value: str) -> str:
    """Scrub a conversation content string for redacted export (T012).

    The strongest single value-scan: the full configuration scrub
    (:func:`redact_config_text` — every credential/secret shape, sensitive raw
    URL/endpoint including a dotless ``serving:8443``, and local path) PLUS the
    structural no-audio discipline (a ``data:…;base64,…`` media/audio blob is
    stripped).  Applied per STRING value (never over a JSON blob), so a numeric
    field can never be mistaken for a secret and a safe ``sha256:``/``…/v1``
    token survives intact.
    """
    return _MEDIA_DATA_URI_PATTERN.sub(_REDACTED_AUDIO, redact_config_text(value))


def scrub_conversation_payload(value):
    """Recursively apply :func:`redact_conversation_text` to every string value.

    The API last-hop guarantee for the redacted conversation export/import
    surface (T012), exactly mirroring :func:`scrub_config_payload` but with the
    added no-audio media scrub, so even a rogue or duck-typed projection that
    bypassed construction-time scrubbing cannot emit a secret, endpoint, path,
    or raw audio blob through the router.  Non-string scalars pass through
    unchanged.
    """
    if isinstance(value, str):
        return redact_conversation_text(value)
    if isinstance(value, dict):
        return {key: scrub_conversation_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_conversation_payload(item) for item in value]
    return value

