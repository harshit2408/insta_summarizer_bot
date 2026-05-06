"""Tests for the urllib-based Groq client."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from groq_client import (  # type: ignore[import-not-found]
    GroqAPIError,
    GroqClient,
    GroqResponse,
)


def _http_response(payload: dict, request_id: str | None = "req-abc") -> object:
    """Fake the file-like object returned by ``urllib.request.urlopen``."""

    class _FakeResp:
        def __init__(self):
            self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))
            self.headers = {"x-request-id": request_id} if request_id else {}

        def read(self):
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _FakeResp()


def _http_error(status: int, body: str = '{"error": "boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.groq.com/x",
        code=status,
        msg="err",
        hdrs={"x-request-id": "req-err"},
        fp=io.BytesIO(body.encode("utf-8")),
    )


CHAT_PAYLOAD = {
    "id": "chat-1",
    "model": "llama-test",
    "choices": [{"message": {"content": '{"ok": true}'}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


class TestConstruction:
    def test_rejects_empty_api_key(self):
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            GroqClient(api_key="")


class TestSuccessfulCall:
    def test_returns_groq_response(self):
        client = GroqClient(api_key="k", max_retries=1)

        with patch("urllib.request.urlopen", return_value=_http_response(CHAT_PAYLOAD)):
            resp = client.complete(system="sys", user="usr")

        assert isinstance(resp, GroqResponse)
        assert resp.content == '{"ok": true}'
        assert resp.model == "llama-test"
        assert resp.request_id == "req-abc"
        assert resp.total_tokens == 15

    def test_sends_json_mode_by_default(self):
        client = GroqClient(api_key="k", max_retries=1)
        captured: dict = {}

        def _capture(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _http_response(CHAT_PAYLOAD)

        with patch("urllib.request.urlopen", side_effect=_capture):
            client.complete(system="s", user="u")

        assert captured["body"]["response_format"] == {"type": "json_object"}
        assert captured["body"]["messages"][0]["role"] == "system"
        assert captured["body"]["messages"][1]["role"] == "user"


class TestRetryLogic:
    def test_retries_on_429_then_succeeds(self):
        client = GroqClient(api_key="k", max_retries=3)

        responses = [_http_error(429), _http_response(CHAT_PAYLOAD)]

        def _side_effect(req, timeout=None):
            r = responses.pop(0)
            if isinstance(r, urllib.error.HTTPError):
                raise r
            return r

        with patch("urllib.request.urlopen", side_effect=_side_effect), \
             patch("time.sleep"):  # don't actually sleep in tests
            resp = client.complete(system="s", user="u")

        assert resp.content == '{"ok": true}'

    def test_retries_on_503_then_succeeds(self):
        client = GroqClient(api_key="k", max_retries=3)
        responses = [_http_error(503), _http_response(CHAT_PAYLOAD)]

        def _side_effect(req, timeout=None):
            r = responses.pop(0)
            if isinstance(r, urllib.error.HTTPError):
                raise r
            return r

        with patch("urllib.request.urlopen", side_effect=_side_effect), \
             patch("time.sleep"):
            resp = client.complete(system="s", user="u")
        assert resp.total_tokens == 15

    def test_does_not_retry_on_401(self):
        client = GroqClient(api_key="k", max_retries=3)
        with patch("urllib.request.urlopen", side_effect=_http_error(401)), \
             patch("time.sleep"):
            with pytest.raises(GroqAPIError) as exc:
                client.complete(system="s", user="u")
        assert exc.value.status == 401

    def test_gives_up_after_max_retries(self):
        client = GroqClient(api_key="k", max_retries=2)
        with patch("urllib.request.urlopen", side_effect=_http_error(503)), \
             patch("time.sleep"):
            with pytest.raises(GroqAPIError):
                client.complete(system="s", user="u")


class TestMalformedResponses:
    def test_invalid_json_response_body(self):
        client = GroqClient(api_key="k", max_retries=1)

        class _Bad:
            def __init__(self):
                self.headers = {}

            def read(self):
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("urllib.request.urlopen", return_value=_Bad()), patch("time.sleep"):
            with pytest.raises(GroqAPIError, match="not JSON"):
                client.complete(system="s", user="u")

    def test_empty_choices(self):
        client = GroqClient(api_key="k", max_retries=1)
        with patch(
            "urllib.request.urlopen",
            return_value=_http_response({"choices": []}),
        ), patch("time.sleep"):
            with pytest.raises(GroqAPIError, match="no choices"):
                client.complete(system="s", user="u")
