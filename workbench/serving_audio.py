"""Hub-side adapter to the Anvil-Serving-managed Dark audio serves.

This module implements the :class:`workbench.voice.VoiceServingTransport`
Protocol against TWO raw Anvil-Serving-managed audio hosts whose wire protocols
differ from the single JSON ``{audio_b64}`` contract the stock
:class:`workbench.voice.ServingVoiceTransport` assumes:

* an STT serve that accepts a ``multipart/form-data`` upload (``file`` +
  ``model``) and answers ``application/json {"text": "..."}``; and
* a TTS serve that accepts a JSON body and answers RAW PCM16 bytes (not JSON).

Both hosts are operator-declared through env (analogous to
``ANVIL_ROUTER_BASE_URL``): the adapter constructs no endpoint of its own, names
no third-party provider, and has no provider fallback -- a serve failure settles
as a fixed :class:`workbench.voice.VoiceServingError`, never a retry elsewhere.

Security discipline mirrors the relay it feeds: raw request/response audio and
the draft transcript are transient, held only for the length of one call, and
are NEVER logged, persisted, or embedded in an error. Every failure detail is a
fixed, non-leaking string (the upstream body is dropped, never surfaced).
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .redaction import redact_value
from .voice import (
    MAX_STT_DURATION_MS,
    MAX_STT_INPUT_BYTES,
    MAX_SYNTH_AUDIO_BYTES,
    VoiceServingError,
)

#: Defaults for the adapter's bounded relay hop.
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_TTS_SAMPLE_RATE = 24000

#: The hub client captures 16 kHz mono PCM16 wrapped in a WAV header (see the
#: browser ``PushToTalk`` control).  Used only to bound a best-effort duration
#: estimate from a WAV payload -- never to transcode.
_STT_CAPTURE_SAMPLE_RATE = 16000

#: Mirrors the durable content-text bound the relay/router enforce so a
#: misbehaving serve cannot smuggle an unbounded transcript back through.
_MAX_TRANSCRIPT_CHARS = 20_000

#: A generous ceiling on the STT serve's small JSON transcript answer; anything
#: larger is treated as a misbehaving upstream and fails closed.
_MAX_STT_JSON_BYTES = 2_000_000

class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every HTTP redirect on the audio hop.

    Belt-and-braces SSRF hardening: a compromised or spoofed serve must not be
    able to re-aim the audio POST at another host via a 3xx.  Returning ``None``
    means the redirect is never followed; urllib then raises an ``HTTPError`` for
    the 3xx, which :meth:`DarkServingAudioTransport._post` settles closed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
        return None


#: A module-level, redirect-free opener for both audio hops.  Combined with the
#: explicit 200-only check in ``_post``, any non-200 (including an unfollowed
#: redirect) fails closed.
_AUDIO_OPENER = build_opener(_NoRedirectHandler())


def _urlopen(request, timeout):  # noqa: ANN001
    """Open one request through the redirect-free audio opener (test seam)."""
    return _AUDIO_OPENER.open(request, timeout=timeout)


#: Truthful part labels per accepted STT container.  The hub client only ever
#: sends ``wav``; other bounded formats are labeled honestly for completeness.
#: A raw ``pcm16`` chunk has no container, so it is sent as opaque bytes.
_STT_PART: dict[str, tuple[str, str]] = {
    "wav": ("audio.wav", "audio/wav"),
    "webm_opus": ("audio.webm", "audio/webm"),
    "ogg_opus": ("audio.ogg", "audio/ogg"),
    "mp3": ("audio.mp3", "audio/mpeg"),
    "pcm16": ("audio.pcm", "application/octet-stream"),
}


def _encode_multipart(
    text_fields: Mapping[str, str], file_part: tuple[str, str, bytes]
) -> tuple[str, bytes]:
    """Encode one file plus text fields as ``multipart/form-data`` (stdlib only)."""
    boundary = "----workbenchvoice" + uuid.uuid4().hex
    marker = boundary.encode("ascii")
    crlf = b"\r\n"
    body = bytearray()
    for name, value in text_fields.items():
        body += b"--" + marker + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode("ascii") + crlf + crlf
        body += str(value).encode("utf-8") + crlf
    filename, content_type, data = file_part
    body += b"--" + marker + crlf
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode("ascii")
        + crlf
    )
    body += f"Content-Type: {content_type}".encode("ascii") + crlf + crlf
    body += bytes(data) + crlf
    body += b"--" + marker + b"--" + crlf
    return f"multipart/form-data; boundary={boundary}", bytes(body)


class DarkServingAudioTransport:
    """A :class:`VoiceServingTransport` over the two Dark audio serves.

    ``transcribe`` uploads the bounded audio to the STT serve and maps its
    ``{"text": ...}`` answer to the relay's ``{text, is_final, duration_ms}``
    draft mapping.  ``synthesize`` posts the message text to the TTS serve and
    maps its raw PCM16 answer to ``{audio_b64, format, sample_rate}``.  Both talk
    ONLY to the operator-declared serve URLs; there is no provider fallback.
    """

    def __init__(
        self,
        stt_url: str,
        tts_url: str,
        stt_model: str,
        tts_model: str,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        sample_rate: int = _DEFAULT_TTS_SAMPLE_RATE,
    ) -> None:
        self._stt_url = str(stt_url)
        self._tts_url = str(tts_url)
        self._stt_model = str(stt_model)
        self._tts_model = str(tts_model)
        self._timeout_s = float(timeout_s)
        self._sample_rate = int(sample_rate)

    # -- STT ------------------------------------------------------------------

    def transcribe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        audio_b64 = request.get("audio_b64")
        audio_format = str(request.get("audio_format") or "")
        is_final = bool(request.get("is_final"))
        if not isinstance(audio_b64, str) or not audio_b64:
            raise VoiceServingError()
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise VoiceServingError() from exc
        if not audio or len(audio) > MAX_STT_INPUT_BYTES:
            raise VoiceServingError()
        filename, content_type = _STT_PART.get(audio_format, ("audio", "application/octet-stream"))
        part_type, body = _encode_multipart(
            {"model": self._stt_model}, (filename, content_type, audio)
        )
        payload = self._post(self._stt_url, body, part_type, expect_json=True)
        text = payload.get("text") if isinstance(payload, Mapping) else None
        if not isinstance(text, str):
            text = ""
        # Scrub the draft exactly as every retained transcript is scrubbed, so a
        # credential the speaker uttered can never ride the draft back out.  Redact
        # BEFORE truncating: truncating first could split a credential across the
        # 20k boundary and leave a fragment the scrub no longer recognizes.
        text = redact_value(text)[:_MAX_TRANSCRIPT_CHARS]
        return {
            "text": text,
            "is_final": is_final,
            "duration_ms": self._estimate_duration_ms(audio, audio_format),
        }

    def _estimate_duration_ms(self, audio: bytes, audio_format: str) -> int:
        """A bounded, best-effort duration for a 16 kHz mono PCM16 WAV, else 0."""
        if audio_format == "wav" and len(audio) > 44:
            samples = (len(audio) - 44) // 2
            estimate = int(samples * 1000 / _STT_CAPTURE_SAMPLE_RATE)
            return max(0, min(estimate, MAX_STT_DURATION_MS))
        return 0

    # -- TTS ------------------------------------------------------------------

    def synthesize(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        text = request.get("text")
        if not isinstance(text, str) or not text:
            raise VoiceServingError()
        body = json.dumps(
            {"model": self._tts_model, "input": text, "response_format": "pcm"},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        audio = self._post(self._tts_url, body, "application/json", expect_json=False)
        if not isinstance(audio, (bytes, bytearray)) or not audio:
            raise VoiceServingError()
        if len(audio) > MAX_SYNTH_AUDIO_BYTES:
            raise VoiceServingError()
        return {
            "audio_b64": base64.b64encode(bytes(audio)).decode("ascii"),
            "format": "pcm16",
            "sample_rate": self._sample_rate,
        }

    # -- transport ------------------------------------------------------------

    def _post(
        self, url: str, body: bytes, content_type: str, *, expect_json: bool
    ) -> Any:
        """One bounded POST hop.  Returns parsed JSON or raw bytes; fails closed.

        The upstream body is NEVER surfaced: any non-200, transport error, size
        overrun, or parse failure becomes a fixed :class:`VoiceServingError`.
        """
        request = Request(
            url,
            data=bytes(body),
            method="POST",
            headers={
                "Content-Type": content_type,
                "Accept": "application/json" if expect_json else "*/*",
            },
        )
        ceiling = _MAX_STT_JSON_BYTES if expect_json else MAX_SYNTH_AUDIO_BYTES
        try:
            with _urlopen(request, timeout=self._timeout_s) as response:  # nosec B310: operator-declared tailnet serve, redirect-free
                if getattr(response, "status", 200) != 200:
                    raise VoiceServingError()
                raw = response.read(ceiling + 1)
        except VoiceServingError:
            raise
        except HTTPError as exc:  # drop the upstream body; never surface it
            raise VoiceServingError() from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise VoiceServingError() from exc
        if len(raw) > ceiling:
            raise VoiceServingError()
        if not expect_json:
            return raw
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VoiceServingError() from exc
