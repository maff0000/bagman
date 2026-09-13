"""Tests for `ai.providers.claude.client.ClaudeClient` — the retry/
error-mapping/logging contract (CD-5 PID §49, WI-3).

Never performs real network I/O — `requests.Session.post` is monkey-
patched with a small deterministic fake per test (PID §61: ordinary
tests must not depend on a live provider). Also covers
`FakeClaudeClient` (the deterministic double every other WI-3 test
uses) directly, and the security/no-secrets-in-logs property.
"""
from __future__ import annotations

import logging

import pytest
import requests

from ai.providers.claude.client import (
    ClaudeAuthenticationError,
    ClaudeClient,
    ClaudeRateLimitedError,
    ClaudeRequestError,
    ClaudeTimeoutError,
    ClaudeUnavailableError,
    ToolDefinition,
)
from ai.providers.claude.fake import FakeClaudeClient, text_turn, tool_use_turn


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text_body: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text_body

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


def _ok_body(*, text="hello", tool_calls=(), stop_reason="end_turn", model="claude-sonnet-5"):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for call_id, name, tool_input in tool_calls:
        content.append({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
    return {
        "model": model,
        "stop_reason": stop_reason,
        "content": content,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _client(tmp_path, *, max_retries=2, session=None) -> ClaudeClient:
    key_file = tmp_path / "anthropic_api_key"
    key_file.write_text("sk-ant-test-key-do-not-log-me", encoding="utf-8")
    return ClaudeClient(
        api_key_file=str(key_file),
        model="claude-sonnet-5",
        max_retries=max_retries,
        connect_timeout_s=0.01,
        read_timeout_s=0.01,
        session=session or requests.Session(),
    )


class _CountingPoster:
    """Monkeypatch target for `requests.Session.post` — returns each
    scripted item in order (a `_FakeResponse`, or an exception
    instance to raise), recording every call."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    def __call__(self, url, *, headers, json, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "json": json, "timeout": timeout})
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------


def test_send_message_success_normalises_text_response(tmp_path, monkeypatch):
    client = _client(tmp_path)
    poster = _CountingPoster([_FakeResponse(200, _ok_body(text="hi there"))])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))

    result = client.send_message(system="sys", messages=[{"role": "user", "content": "hello"}])
    assert result.text == "hi there"
    assert result.tool_calls == ()
    assert result.provider_model == "claude-sonnet-5"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}
    assert len(poster.calls) == 1


def test_send_message_normalises_tool_use_response(tmp_path, monkeypatch):
    client = _client(tmp_path)
    poster = _CountingPoster(
        [_FakeResponse(200, _ok_body(text=None, tool_calls=[("id1", "get_document", {"evidence_id": "x"})], stop_reason="tool_use"))]
    )
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))

    result = client.send_message(
        system="sys",
        messages=[{"role": "user", "content": "hello"}],
        tools=[ToolDefinition(name="get_document", description="d", input_schema={"type": "object"})],
    )
    assert result.stop_reason == "tool_use"
    assert result.text is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_document"
    assert result.tool_calls[0].tool_call_id == "id1"
    # the request body actually included the tools payload
    assert poster.calls[0]["json"]["tools"][0]["name"] == "get_document"


def test_api_key_is_never_present_in_the_request_body(tmp_path, monkeypatch):
    client = _client(tmp_path)
    poster = _CountingPoster([_FakeResponse(200, _ok_body())])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert "sk-ant-test-key-do-not-log-me" not in str(poster.calls[0]["json"])
    # it IS present as the x-api-key header (the only correct place)
    assert poster.calls[0]["headers"]["x-api-key"] == "sk-ant-test-key-do-not-log-me"


def test_api_key_is_never_logged(tmp_path, monkeypatch, caplog):
    client = _client(tmp_path)
    poster = _CountingPoster([_FakeResponse(200, _ok_body())])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    with caplog.at_level(logging.DEBUG):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    for record in caplog.records:
        assert "sk-ant-test-key-do-not-log-me" not in record.getMessage()
        assert "sk-ant-test-key-do-not-log-me" not in str(record.__dict__)


# ---------------------------------------------------------------------
# retry policy (PID §49) — never retried
# ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure_is_never_retried(tmp_path, monkeypatch, status):
    client = _client(tmp_path, max_retries=3)
    poster = _CountingPoster([_FakeResponse(status)])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    with pytest.raises(ClaudeAuthenticationError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert len(poster.calls) == 1  # no retry attempted


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_4xx_is_never_retried(tmp_path, monkeypatch, status):
    client = _client(tmp_path, max_retries=3)
    poster = _CountingPoster([_FakeResponse(status, text_body="bad request")])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    with pytest.raises(ClaudeRequestError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert len(poster.calls) == 1


# ---------------------------------------------------------------------
# retry policy (PID §49) — retried up to max_retries, then raised
# ---------------------------------------------------------------------


def test_rate_limit_is_retried_then_raises_after_bound_exhausted(tmp_path, monkeypatch):
    client = _client(tmp_path, max_retries=2)
    poster = _CountingPoster([_FakeResponse(429), _FakeResponse(429), _FakeResponse(429)])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    monkeypatch.setattr("ai.providers.claude.client._backoff_seconds", lambda attempt: 0.0)
    with pytest.raises(ClaudeRateLimitedError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert len(poster.calls) == 3  # max_retries=2 -> 3 total attempts


def test_rate_limit_succeeds_on_a_later_attempt(tmp_path, monkeypatch):
    client = _client(tmp_path, max_retries=2)
    poster = _CountingPoster([_FakeResponse(429), _FakeResponse(200, _ok_body(text="ok now"))])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    monkeypatch.setattr("ai.providers.claude.client._backoff_seconds", lambda attempt: 0.0)
    result = client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert result.text == "ok now"
    assert len(poster.calls) == 2


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_is_retried_then_raises_unavailable(tmp_path, monkeypatch, status):
    client = _client(tmp_path, max_retries=1)
    poster = _CountingPoster([_FakeResponse(status), _FakeResponse(status)])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    monkeypatch.setattr("ai.providers.claude.client._backoff_seconds", lambda attempt: 0.0)
    with pytest.raises(ClaudeUnavailableError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert len(poster.calls) == 2  # max_retries=1 -> 2 total attempts


def test_timeout_is_retried_then_raises_claude_timeout_error(tmp_path, monkeypatch):
    client = _client(tmp_path, max_retries=1)
    poster = _CountingPoster([requests.exceptions.Timeout("boom"), requests.exceptions.Timeout("boom")])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    monkeypatch.setattr("ai.providers.claude.client._backoff_seconds", lambda attempt: 0.0)
    with pytest.raises(ClaudeTimeoutError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert len(poster.calls) == 2


def test_connection_error_is_retried_then_raises_unavailable(tmp_path, monkeypatch):
    client = _client(tmp_path, max_retries=1)
    poster = _CountingPoster(
        [requests.exceptions.ConnectionError("boom"), requests.exceptions.ConnectionError("boom")]
    )
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    monkeypatch.setattr("ai.providers.claude.client._backoff_seconds", lambda attempt: 0.0)
    with pytest.raises(ClaudeUnavailableError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert len(poster.calls) == 2


def test_non_json_response_body_raises_unavailable(tmp_path, monkeypatch):
    client = _client(tmp_path, max_retries=0)
    poster = _CountingPoster([_FakeResponse(200, json_body=None)])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    with pytest.raises(ClaudeUnavailableError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------


def test_is_available_true_on_success(tmp_path, monkeypatch):
    client = _client(tmp_path)
    poster = _CountingPoster([_FakeResponse(200, _ok_body(text="OK"))])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    assert client.is_available() is True


def test_is_available_false_on_auth_failure_never_raises(tmp_path, monkeypatch):
    client = _client(tmp_path)
    poster = _CountingPoster([_FakeResponse(401)])
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: poster(url, **kw))
    assert client.is_available() is False


def test_missing_api_key_file_raises_authentication_error_not_a_raw_os_error(tmp_path):
    client = ClaudeClient(api_key_file=str(tmp_path / "does_not_exist"), connect_timeout_s=0.01, read_timeout_s=0.01)
    with pytest.raises(ClaudeAuthenticationError):
        client.send_message(system="sys", messages=[{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------
# configuration defaults (PID §18)
# ---------------------------------------------------------------------


def test_default_model_is_a_real_non_stale_claude_identifier(tmp_path):
    client = _client(tmp_path)
    assert client.model.startswith("claude-")


def test_model_is_overridable_via_constructor_and_env(tmp_path, monkeypatch):
    client = ClaudeClient(api_key_file=str(tmp_path / "k"), model="claude-custom-override")
    assert client.model == "claude-custom-override"

    (tmp_path / "k2").write_text("x", encoding="utf-8")
    monkeypatch.setenv("BAGMAN_OPERATOR_MODEL", "claude-env-override")
    client2 = ClaudeClient(api_key_file=str(tmp_path / "k2"))
    assert client2.model == "claude-env-override"


# ---------------------------------------------------------------------
# FakeClaudeClient (PID §61) — the double every other WI-3 test uses
# ---------------------------------------------------------------------


def test_fake_claude_client_returns_scripted_responses_in_order():
    fake = FakeClaudeClient(scripted_responses=[text_turn("first"), text_turn("second")])
    r1 = fake.send_message(system="s", messages=[])
    r2 = fake.send_message(system="s", messages=[])
    assert (r1.text, r2.text) == ("first", "second")


def test_fake_claude_client_falls_back_to_default_text_when_queue_empty():
    fake = FakeClaudeClient(default_text="fallback")
    result = fake.send_message(system="s", messages=[])
    assert result.text == "fallback"


def test_fake_claude_client_can_script_a_raised_exception():
    fake = FakeClaudeClient(scripted_responses=[ClaudeAuthenticationError("simulated auth failure")])
    with pytest.raises(ClaudeAuthenticationError):
        fake.send_message(system="s", messages=[])


def test_fake_claude_client_records_every_call():
    fake = FakeClaudeClient(scripted_responses=[text_turn("hi")])
    fake.send_message(system="the-system-prompt", messages=[{"role": "user", "content": "hello"}])
    assert len(fake.calls) == 1
    assert fake.calls[0].system == "the-system-prompt"


def test_fake_claude_client_is_available_is_configurable():
    assert FakeClaudeClient(available=True).is_available() is True
    assert FakeClaudeClient(available=False).is_available() is False
