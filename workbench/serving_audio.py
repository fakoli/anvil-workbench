"""Hub-side reader for the Anvil-Serving-managed TTS voice catalog.

The request/response voice RELAY (STT dictation + read-aloud TTS) no longer lives
here: it now goes through Anvil Serving's unified audio gateway (anvil-serving#280)
via :class:`workbench.voice.ServingVoiceTransport` over the router base URL, so the
hub reaches ONLY the router -- never a raw STT/TTS serve.  The interim
``DarkServingAudioTransport`` that spoke the two raw Dark serves' wire protocols
(multipart STT + raw-PCM TTS) has been retired now that #280 supersedes it.

What REMAINS here is the Voice-tab voice PICKER's catalog read.  The #280 gateway
deliberately does NOT expose a ``/audio/voices`` endpoint, so the selectable-voice
list is still sourced from the TTS serve's own voices endpoint
(``ANVIL_VOICE_VOICES_URL``, e.g. kokoro ``/v1/audio/voices``).  This is a small,
enumerable metadata read -- not an audio relay -- and the browser never talks to
the serve directly: the hub fetches, bounds, scrubs, and forwards only voice ids.

Security discipline: any non-200, transport error, oversize body, or parse failure
fails closed as a fixed, non-leaking :class:`workbench.voice.VoiceServingError`
(the upstream body is dropped, never surfaced); every forwarded id/name is
credential/path-scrubbed and length-bounded; redirects are refused (SSRF
hardening); and there is no provider fallback.
"""
from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .redaction import redact_text
from .voice import VoiceServingError

#: Default timeout for the bounded voice-catalog GET.
_DEFAULT_TIMEOUT_S = 30.0

#: Bounds on the TTS serve's voice-catalog answer.  The catalog is a small,
#: enumerable list of voice ids; a larger body or an over-long id is a misbehaving
#: (or spoofed) serve and is dropped.  The id is the ONLY value forwarded to the
#: browser -- scrubbed, so a serve cannot smuggle a secret/path through a label.
_MAX_VOICES_JSON_BYTES = 262_144
_MAX_VOICE_CATALOG_ENTRIES = 500
_MAX_VOICE_ID_CHARS = 128
_MAX_VOICE_NAME_CHARS = 128


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every HTTP redirect on the catalog hop.

    Belt-and-braces SSRF hardening: a compromised or spoofed serve must not be
    able to re-aim the GET at another host via a 3xx.  Returning ``None`` means the
    redirect is never followed; urllib then raises an ``HTTPError`` for the 3xx,
    which :func:`fetch_voice_catalog` settles closed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
        return None


#: A module-level, redirect-free opener for the catalog hop.  Combined with the
#: explicit 200-only check below, any non-200 (including an unfollowed redirect)
#: fails closed.
_AUDIO_OPENER = build_opener(_NoRedirectHandler())


def _urlopen(request, timeout):  # noqa: ANN001
    """Open one request through the redirect-free opener (test seam)."""
    return _AUDIO_OPENER.open(request, timeout=timeout)


def _coerce_voice_entry(entry: Any) -> dict[str, str] | None:
    """Map one upstream catalog entry to a bounded, scrubbed ``{id, name}``.

    Accepts either a mapping (``{"id": ..., "name": ...}``) or a bare string id.
    Every value is scrubbed with the transcript credential/path redaction and
    length-bounded; an entry without a usable id is dropped (returns ``None``).
    """
    if isinstance(entry, str):
        voice_id, name = entry, ""
    elif isinstance(entry, Mapping):
        raw_id = entry.get("id")
        voice_id = raw_id if isinstance(raw_id, str) else ""
        raw_name = entry.get("name")
        name = raw_name if isinstance(raw_name, str) else ""
    else:
        return None
    voice_id = redact_text(voice_id).strip()[:_MAX_VOICE_ID_CHARS]
    if not voice_id:
        return None
    name = redact_text(name).strip()[:_MAX_VOICE_NAME_CHARS]
    result = {"id": voice_id}
    if name:
        result["name"] = name
    return result


def fetch_voice_catalog(url: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> list[dict[str, str]]:
    """Enumerate the operator-declared TTS serve's voices; fail closed if unusable.

    One bounded GET against the operator-declared voices URL (``ANVIL_VOICE_VOICES_URL``).
    The serve answers ``{"voices": [{"id": ...}, ...]}`` (or a bare list); the ids
    are scrubbed, length-bounded, and count-bounded before they leave the hub.  Any
    non-200, transport error, oversize body, or parse failure surfaces as
    :class:`VoiceServingError` -- there is no provider fallback and the upstream
    body is never surfaced.  The relay boundary is preserved: the browser never
    talks to the serve directly.
    """
    text = str(url or "").strip()
    if not text or not (text.startswith("http://") or text.startswith("https://")):
        raise VoiceServingError()
    request = Request(text, method="GET", headers={"Accept": "application/json"})
    try:
        with _urlopen(request, timeout=float(timeout_s)) as response:  # nosec B310: operator-declared tailnet serve, redirect-free
            if getattr(response, "status", 200) != 200:
                raise VoiceServingError()
            raw = response.read(_MAX_VOICES_JSON_BYTES + 1)
    except VoiceServingError:
        raise
    except HTTPError as exc:
        raise VoiceServingError() from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise VoiceServingError() from exc
    if len(raw) > _MAX_VOICES_JSON_BYTES:
        raise VoiceServingError()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceServingError() from exc
    if isinstance(payload, Mapping):
        entries = payload.get("voices")
    else:
        entries = payload
    if not isinstance(entries, list):
        raise VoiceServingError()
    catalog: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries[:_MAX_VOICE_CATALOG_ENTRIES]:
        coerced = _coerce_voice_entry(entry)
        if coerced is None or coerced["id"] in seen:
            continue
        seen.add(coerced["id"])
        catalog.append(coerced)
    return catalog
