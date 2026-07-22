"""Hermetic proofs for the Voice-tab voice-catalog reader (workbench.serving_audio).

The request/response STT/TTS RELAY no longer lives in this module: it goes through
Anvil Serving's unified audio gateway (anvil-serving#280) via
``workbench.voice.ServingVoiceTransport`` over the router base URL (proven in
``tests/test_deployment_wiring.py`` and ``tests/test_harness_kernel.py``).  The
interim ``DarkServingAudioTransport`` that spoke the two raw Dark serves' wire
protocols has been retired.

What remains here is the voice PICKER's catalog read (``fetch_voice_catalog``),
which #280 deliberately does NOT supersede (the gateway exposes no ``/audio/voices``).
These tests monkeypatch the stdlib ``urlopen`` the reader uses so nothing touches a
real serve, then assert the happy-path mapping, credential/bound scrubbing, and
fail-closed + SSRF hardening.
"""
from __future__ import annotations

import json

import pytest

from urllib.error import HTTPError

import workbench.serving_audio as sa
from workbench.serving_audio import fetch_voice_catalog
from workbench.voice import VoiceServingError

_VOICES_URL = "http://serving-tts.internal:30011/v1/audio/voices"


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


# --- catalog happy path + shaping --------------------------------------------


def test_fetch_voice_catalog_maps_mapping_and_string_entries(monkeypatch):
    captured: list = []
    _patch_urlopen(monkeypatch, body=json.dumps(
        {"voices": [{"id": "af_heart", "name": "Heart"}, "bm_george", {"id": "af_heart"}]}
    ).encode(), capture=captured)
    catalog = fetch_voice_catalog(_VOICES_URL)
    # A mapping entry keeps id + name; a bare string becomes an id; the duplicate is
    # de-duplicated.
    assert catalog == [{"id": "af_heart", "name": "Heart"}, {"id": "bm_george"}]
    request = captured[0]
    assert request.full_url == _VOICES_URL
    assert request.get_method() == "GET"


def test_fetch_voice_catalog_accepts_a_bare_list(monkeypatch):
    _patch_urlopen(monkeypatch, body=json.dumps(["af_heart", "bm_george"]).encode())
    assert fetch_voice_catalog(_VOICES_URL) == [{"id": "af_heart"}, {"id": "bm_george"}]


def test_fetch_voice_catalog_scrubs_a_credential_in_an_id_or_name(monkeypatch):
    # A serve must not be able to smuggle a secret out through a voice label.
    _patch_urlopen(monkeypatch, body=json.dumps(
        {"voices": [{"id": "ok_voice", "name": "token: supersecretvalue123"}]}
    ).encode())
    catalog = fetch_voice_catalog(_VOICES_URL)
    blob = json.dumps(catalog)
    assert "supersecretvalue123" not in blob
    assert "[REDACTED]" in blob


# --- fail-closed + SSRF hardening --------------------------------------------


def test_fetch_voice_catalog_rejects_a_non_http_url():
    with pytest.raises(VoiceServingError):
        fetch_voice_catalog("ftp://nope")


def test_fetch_voice_catalog_fails_closed_on_non_200(monkeypatch):
    marker = "UPSTREAM-VOICES-LEAK-MARKER"
    err = HTTPError(_VOICES_URL, 500, "boom: " + marker, {}, None)
    _patch_urlopen(monkeypatch, raises=err)
    with pytest.raises(VoiceServingError) as exc:
        fetch_voice_catalog(_VOICES_URL)
    assert marker not in str(exc.value)
    assert str(exc.value) == "voice relay is unavailable"


def test_fetch_voice_catalog_fails_closed_on_unparseable_json(monkeypatch):
    _patch_urlopen(monkeypatch, body=b"not json at all")
    with pytest.raises(VoiceServingError):
        fetch_voice_catalog(_VOICES_URL)


def test_fetch_voice_catalog_refuses_a_redirect_from_the_serve(monkeypatch):
    # A compromised serve answering 3xx must NOT re-aim the GET. The redirect-free
    # opener turns the 3xx into an HTTPError -> fixed VoiceServingError.
    err = HTTPError(_VOICES_URL, 302, "Found", {"Location": "http://attacker.example/steal"}, None)
    _patch_urlopen(monkeypatch, raises=err)
    with pytest.raises(VoiceServingError):
        fetch_voice_catalog(_VOICES_URL)


def test_audio_opener_has_no_redirect_following():
    # Structural guard: the module opener refuses redirects (redirect_request None).
    handler = sa._NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://attacker.example") is None
