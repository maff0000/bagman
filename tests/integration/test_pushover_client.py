"""Unit tests for `services.notifications.pushover` (WO-BAGMAN-118-
MAILBOX-HEALTH-ALERTING §6.A.6) — credential file reading, and
`PushoverClient.send`'s own outcome-as-data response parsing (mocked
`urllib.request.urlopen`, never real network I/O — mirrors
`tests/integration/test_mailbox_gmail_client.py`'s own convention for
Microsoft/Gmail provider adapters)."""
from __future__ import annotations

import io
import json
from unittest import mock

import pytest

from services.notifications.pushover import (
    FakePushoverClient,
    PushoverClient,
    PushoverCredentials,
    PushoverOutcomeStatus,
    PushoverSendResult,
    read_pushover_credentials,
)


def test_read_pushover_credentials_missing_files_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("BAGMAN_PUSHOVER_API_TOKEN_FILE", str(tmp_path / "missing_token"))
    monkeypatch.setenv("BAGMAN_PUSHOVER_USER_KEY_FILE", str(tmp_path / "missing_user"))
    assert read_pushover_credentials() is None


def test_read_pushover_credentials_reads_fresh_every_call(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    user_file = tmp_path / "user"
    monkeypatch.setenv("BAGMAN_PUSHOVER_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("BAGMAN_PUSHOVER_USER_KEY_FILE", str(user_file))

    assert read_pushover_credentials() is None

    token_file.write_text("my-token\n", encoding="utf-8")
    user_file.write_text("my-user\n", encoding="utf-8")
    credentials = read_pushover_credentials()
    assert credentials == PushoverCredentials(api_token="my-token", user_key="my-user")


def _fake_urlopen_ok(*args, **kwargs):
    body = json.dumps({"status": 1, "request": "req-id-123"}).encode("utf-8")
    response = mock.MagicMock()
    response.read.return_value = body
    response.status = 200
    response.__enter__.return_value = response
    return response


def test_pushover_client_send_ok(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_ok)
    client = PushoverClient()
    result = client.send(
        credentials=PushoverCredentials(api_token="t", user_key="u"), title="Test", message="hello"
    )
    assert result.status == PushoverOutcomeStatus.OK
    assert result.http_status == 200
    assert result.provider_request_id == "req-id-123"


def _fake_urlopen_http_error(status_code: int, body: bytes = b"{}"):
    import urllib.error

    def _raise(*args, **kwargs):
        raise urllib.error.HTTPError(url="https://api.pushover.net/1/messages.json", code=status_code, msg="err", hdrs=None, fp=io.BytesIO(body))

    return _raise


@pytest.mark.parametrize(
    "status_code,expected_status",
    [(401, PushoverOutcomeStatus.AUTH_ERROR), (429, PushoverOutcomeStatus.RATE_LIMITED), (500, PushoverOutcomeStatus.PROVIDER_ERROR)],
)
def test_pushover_client_send_http_errors(monkeypatch, status_code, expected_status):
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_http_error(status_code))
    client = PushoverClient()
    result = client.send(credentials=PushoverCredentials(api_token="t", user_key="u"), title="Test", message="hello")
    assert result.status == expected_status
    assert result.http_status == status_code


def test_pushover_client_send_malformed_response(monkeypatch):
    def _fake(*args, **kwargs):
        response = mock.MagicMock()
        response.read.return_value = b"not json"
        response.status = 200
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    client = PushoverClient()
    result = client.send(credentials=PushoverCredentials(api_token="t", user_key="u"), title="Test", message="hello")
    assert result.status == PushoverOutcomeStatus.MALFORMED_RESPONSE


def test_fake_pushover_client_records_calls():
    fake = FakePushoverClient(result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200))
    result = fake.send(credentials=PushoverCredentials(api_token="t", user_key="u"), title="T", message="M")
    assert result.status == PushoverOutcomeStatus.OK
    assert fake.calls == [{"title": "T", "message": "M"}]
