"""Hermetic proofs for the Dark Serving audio adapter (workbench.serving_audio).

The adapter is the ONLY place that speaks the two real Dark serve protocols:
the STT serve's multipart-upload -> ``{"text"}`` contract and the TTS serve's
JSON -> raw-PCM16 contract. These tests monkeypatch the stdlib ``urlopen`` the
adapter uses so NOTHING touches a real serve, then assert:

* the request/response mapping in BOTH directions matches the real contracts;
* a non-200 / oversize / unparseable serve answer fails closed as a fixed,
  non-leaking ``VoiceServingError`` (no audio, text, or upstream body echoed);
* the returned draft is credential-scrubbed.
"""
from __future__ import annotations

import base64
import json

import pytest

from urllib.error import HTTPError

import workbench.serving_audio as sa
from workbench.serving_audio import DarkServingAudioTransport
from workbench.voice import MAX_SYNTH_AUDIO_BYTES, VoiceServingError

_STT_URL = "http://serving-stt.internal:30010/v1/audio/transcriptions"
_TTS_URL = "http://serving-tts.internal:30011/v1/audio/speech"


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            return self._body
        return self._body[:n]


def _patch_urlopen(monkeypatch, *, status=200, body=b"", raises=None, capture=None):
    def fake(request, timeout=None):  # noqa: ANN001
        if capture is not None:
            capture.append(request)
        if raises is not None:
            raise raises
        return _FakeResponse(status, body)

    monkeypatch.setattr(sa, "_urlopen", fake)


def _transport() -> DarkServingAudioTransport:
    return DarkServingAudioTransport(
        _STT_URL, _TTS_URL, "tdt-model", "kokoro-model", sample_rate=24000,
    )


# --- STT (parakeet-shaped multipart -> {"text"}) -----------------------------


def test_transcribe_maps_multipart_upload_to_the_serve_text(monkeypatch):
    captured: list = []
    _patch_urlopen(monkeypatch, status=200, body=json.dumps({"text": "hello there"}).encode(), capture=captured)
    audio = b"RIFF____WAVEfmt " + b"\x00" * 64  # a wav-ish bounded blob
    result = _transport().transcribe({
        "audio_b64": base64.b64encode(audio).decode(), "audio_format": "wav", "is_final": True,
    })
    # The mapping the relay expects.
    assert result["text"] == "hello there"
    assert result["is_final"] is True
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0

    # The request really was a multipart upload to the STT serve carrying the
    # model field and a file part (the real parakeet contract).
    request = captured[0]
    assert request.full_url == _STT_URL
    assert request.get_method() == "POST"
    ctype = dict(request.header_items())["Content-type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    body = request.data
    assert b'name="model"' in body and b"tdt-model" in body
    assert b'name="file"; filename="audio.wav"' in body
    assert audio in body  # the raw audio rode as the file part


def test_transcribe_passes_through_interim_flag(monkeypatch):
    _patch_urlopen(monkeypatch, status=200, body=json.dumps({"text": "partial"}).encode())
    result = _transport().transcribe({
        "audio_b64": base64.b64encode(b"x" * 40).decode(), "audio_format": "wav", "is_final": False,
    })
    assert result["is_final"] is False
    assert result["text"] == "partial"


def test_transcribe_scrubs_a_credential_in_the_returned_draft(monkeypatch):
    # A speaker who dictated a secret must not have it ride the draft back out.
    _patch_urlopen(monkeypatch, status=200, body=json.dumps({"text": "the token: supersecretvalue123 ok"}).encode())
    result = _transport().transcribe({
        "audio_b64": base64.b64encode(b"y" * 40).decode(), "audio_format": "wav", "is_final": True,
    })
    assert "supersecretvalue123" not in result["text"]
    assert "[REDACTED]" in result["text"]


def test_transcribe_fails_closed_on_non_200(monkeypatch):
    marker = "UPSTREAM-STT-LEAK-MARKER-tok_abcdefgh"
    err = HTTPError(_STT_URL, 500, "install failed: " + marker, {}, None)
    _patch_urlopen(monkeypatch, raises=err)
    with pytest.raises(VoiceServingError) as exc:
        _transport().transcribe({
            "audio_b64": base64.b64encode(b"z" * 40).decode(), "audio_format": "wav", "is_final": True,
        })
    # The fixed public detail never carries the upstream body.
    assert marker not in str(exc.value)
    assert str(exc.value) == "voice relay is unavailable"


def test_transcribe_fails_closed_on_unparseable_json(monkeypatch):
    _patch_urlopen(monkeypatch, status=200, body=b"not json at all")
    with pytest.raises(VoiceServingError):
        _transport().transcribe({
            "audio_b64": base64.b64encode(b"z" * 40).decode(), "audio_format": "wav", "is_final": True,
        })


def test_transcribe_rejects_empty_or_invalid_audio(monkeypatch):
    _patch_urlopen(monkeypatch, status=200, body=json.dumps({"text": "x"}).encode())
    with pytest.raises(VoiceServingError):
        _transport().transcribe({"audio_b64": "", "audio_format": "wav", "is_final": True})
    with pytest.raises(VoiceServingError):
        _transport().transcribe({"audio_b64": "!!not base64!!", "audio_format": "wav", "is_final": True})


# --- TTS (kokoro-shaped JSON -> raw PCM16) -----------------------------------


def test_synthesize_posts_json_and_maps_raw_pcm_to_b64(monkeypatch):
    captured: list = []
    pcm = b"\x01\x02\x03\x04" * 32  # raw PCM16 bytes, NOT json
    _patch_urlopen(monkeypatch, status=200, body=pcm, capture=captured)
    result = _transport().synthesize({"text": "read this aloud", "output_format": "pcm16"})
    # The mapping the relay expects: base64 of the raw PCM, a pcm16 format, and
    # the configured serve sample rate (24000 for kokoro, NOT the 16k capture rate).
    assert base64.b64decode(result["audio_b64"]) == pcm
    assert result["format"] == "pcm16"
    assert result["sample_rate"] == 24000

    # The request was a JSON POST to the TTS serve asking for raw PCM.
    request = captured[0]
    assert request.full_url == _TTS_URL
    assert dict(request.header_items())["Content-type"] == "application/json"
    payload = json.loads(request.data.decode())
    assert payload == {"model": "kokoro-model", "input": "read this aloud", "response_format": "pcm"}


def test_synthesize_fails_closed_over_the_audio_ceiling(monkeypatch):
    _patch_urlopen(monkeypatch, status=200, body=b"\x00" * (MAX_SYNTH_AUDIO_BYTES + 8))
    with pytest.raises(VoiceServingError):
        _transport().synthesize({"text": "too much audio", "output_format": "pcm16"})


def test_synthesize_fails_closed_on_empty_body(monkeypatch):
    _patch_urlopen(monkeypatch, status=200, body=b"")
    with pytest.raises(VoiceServingError):
        _transport().synthesize({"text": "silence", "output_format": "pcm16"})


def test_synthesize_fails_closed_on_non_200(monkeypatch):
    marker = "UPSTREAM-TTS-LEAK-MARKER"
    err = HTTPError(_TTS_URL, 503, marker, {}, None)
    _patch_urlopen(monkeypatch, raises=err)
    with pytest.raises(VoiceServingError) as exc:
        _transport().synthesize({"text": "hello", "output_format": "pcm16"})
    assert marker not in str(exc.value)


# --- SSRF hardening: redirects are refused (never followed) -------------------


def test_transcribe_refuses_a_redirect_from_the_serve(monkeypatch):
    # A compromised serve answering 3xx must NOT re-aim the audio POST. The
    # redirect-free opener turns the 3xx into an HTTPError -> fixed VoiceServingError.
    err = HTTPError(_STT_URL, 302, "Found", {"Location": "http://attacker.example/steal"}, None)
    _patch_urlopen(monkeypatch, raises=err)
    with pytest.raises(VoiceServingError):
        _transport().transcribe({
            "audio_b64": base64.b64encode(b"z" * 40).decode(), "audio_format": "wav", "is_final": True,
        })


def test_audio_opener_has_no_redirect_following():
    # Structural guard: the module opener refuses redirects (redirect_request None).
    handler = sa._NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://attacker.example") is None
