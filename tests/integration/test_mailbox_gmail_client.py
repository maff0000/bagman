"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.gmail_client` — the real Gmail OAuth2/API
wrapper's own pure helpers, authorize-URL construction, outcome-status
mapping, and read-only structural guarantee. NO real network call to
Google is ever made by this test file (per this delivery's own hard
constraint — no live credentials exist).
"""
from __future__ import annotations

import base64
import urllib.error
import urllib.parse

import pytest

from services.mailbox.gmail.gmail_client import (
    DEFAULT_METADATA_HEADERS,
    GmailClient,
    GmailOAuthClient,
    GmailOutcomeStatus,
    _decode_base64url,
    _status_for_http_error,
    _status_for_transport_error,
)
from services.mailbox.gmail.secrets import GmailAppCredentials


def _fake_http_error(code: int) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(url="https://example.com", code=code, msg="err", hdrs=None, fp=io.BytesIO(b"{}"))


# -- base64url decode ------------------------------------------------------


def test_decode_base64url_handles_missing_padding():
    raw = b"Subject: hi\r\n\r\nbody"
    encoded_no_padding = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    assert _decode_base64url(encoded_no_padding) == raw


def test_decode_base64url_handles_full_padding():
    raw = b"a"
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    assert _decode_base64url(encoded) == raw


# -- outcome-status mapping --------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, GmailOutcomeStatus.AUTH_ERROR),
        (403, GmailOutcomeStatus.PERMISSION_ERROR),
        (404, GmailOutcomeStatus.NOT_FOUND),
        (429, GmailOutcomeStatus.RATE_LIMITED),
        (500, GmailOutcomeStatus.PROVIDER_ERROR),
        (503, GmailOutcomeStatus.PROVIDER_ERROR),
    ],
)
def test_status_for_http_error_mapping(code, expected):
    assert _status_for_http_error(_fake_http_error(code)) == expected


def test_status_for_transport_error_classifies_timeout():
    assert _status_for_transport_error(TimeoutError("timed out")) == GmailOutcomeStatus.TIMEOUT


def test_status_for_transport_error_defaults_to_transport_error():
    assert _status_for_transport_error(OSError("connection refused")) == GmailOutcomeStatus.TRANSPORT_ERROR


def test_shared_vocabulary_with_graph_outcome_status_via_string_equality():
    """Mirrors `services.mailbox.imap.imap_client`'s own documented
    "shared vocabulary via plain string-enum equality" proof — see that
    module's own docstring, and `gmail_client.py`'s own "Outcome-status
    pattern" section."""
    from services.mailbox.microsoft.graph_client import GraphOutcomeStatus

    assert GmailOutcomeStatus.AUTH_ERROR == GraphOutcomeStatus.AUTH_ERROR
    assert GmailOutcomeStatus.RATE_LIMITED == GraphOutcomeStatus.RATE_LIMITED
    assert GmailOutcomeStatus.OK == GraphOutcomeStatus.OK


# -- OAuth client (no network — pure URL/config-state helpers only) ------


def test_is_configured_false_when_no_credentials():
    client = GmailOAuthClient(credentials_provider=lambda: None)
    assert client.is_configured() is False


def test_is_configured_true_when_credentials_present():
    client = GmailOAuthClient(credentials_provider=lambda: GmailAppCredentials(client_id="id", client_secret="secret"))
    assert client.is_configured() is True


def test_build_authorize_url_requests_readonly_scope_offline_access_and_consent_prompt():
    client = GmailOAuthClient(credentials_provider=lambda: GmailAppCredentials(client_id="fake-client-id", client_secret="s"))
    url = client.build_authorize_url(state="fake-state-value", redirect_uri="https://localhost:8543/internal/mailboxes/gmail/oauth/callback")
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"
    params = urllib.parse.parse_qs(parsed.query)
    assert params["client_id"] == ["fake-client-id"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert params["state"] == ["fake-state-value"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    # NEVER a broader scope than read-only.
    assert "modify" not in params["scope"][0]
    assert "send" not in params["scope"][0]


def test_build_authorize_url_never_leaks_client_secret():
    client = GmailOAuthClient(credentials_provider=lambda: GmailAppCredentials(client_id="id", client_secret="super-secret-value"))
    url = client.build_authorize_url(state="s", redirect_uri="https://localhost:8543/cb")
    assert "super-secret-value" not in url


def test_default_metadata_headers_include_the_fields_the_adapter_needs():
    for expected in ("Subject", "From", "Message-ID", "Date", "Authentication-Results"):
        assert expected in DEFAULT_METADATA_HEADERS


# -- read-only structural guarantee --------------------------------------


def test_gmail_client_never_exposes_a_mutating_method():
    """The read-only guarantee is structural — see module docstring's
    own 'no generic raw-command escape hatch' requirement, mirroring
    `services.mailbox.imap.imap_client`'s identical proof. No
    `send`/`modify`/`trash`/`delete`/`draft*`/`batchModify`-shaped method
    exists anywhere on this class."""
    forbidden = {
        "send",
        "modify",
        "trash",
        "untrash",
        "delete",
        "batch_modify",
        "batchmodify",
        "create_draft",
        "send_draft",
        "update_draft",
        "delete_draft",
        "insert",
        "import_message",
    }
    public_methods = {name.lower() for name in dir(GmailClient) if not name.startswith("_")}
    assert forbidden.isdisjoint(public_methods)


def _non_docstring_string_literals(module) -> str:
    """Every string LITERAL in `module`'s own source, EXCLUDING module/
    class/function docstrings and comments (comments are never part of
    the parsed AST at all) — used so a source-level self-check below
    inspects only real code (URL templates, import names), never this
    module's own prose explaining what it deliberately does NOT do."""
    import ast
    import inspect

    source = inspect.getsource(module)
    tree = ast.parse(source)
    docstring_ids: set[int] = set()

    def _mark(node) -> None:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docstring_ids.add(id(body[0].value))
        for child in ast.iter_child_nodes(node):
            _mark(child)

    _mark(tree)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids
    ]
    return "\n".join(literals)


def test_gmail_client_source_never_contains_a_mutating_gmail_endpoint_path():
    """Bounded, source-level self-check: no mutating Gmail API path
    fragment (`/modify`, `/trash`, `/untrash`, `/send`, `/drafts`,
    `/import`, `/batchModify`, `/batchDelete`) ever appears as a real
    code string literal in this module's source (its own prose
    explaining what it deliberately does NOT do is excluded — see
    `_non_docstring_string_literals` above)."""
    from services.mailbox.gmail import gmail_client as module

    code_strings = _non_docstring_string_literals(module)
    for fragment in ("/modify", "/trash", "/untrash", "/send", "/drafts", "/import", "batchModify", "batchDelete"):
        assert fragment not in code_strings, f"found a mutating Gmail API path fragment {fragment!r} in gmail_client.py"


def test_gmail_client_source_never_imports_a_forbidden_google_sdk():
    """Direct, source-level self-check mirroring the repo-wide
    architecture-boundaries test — this module must never IMPORT
    `googleapiclient`/`google_auth_oauthlib` (a real `import` statement,
    checked via the AST's own `Import`/`ImportFrom` nodes — never a
    prose mention in a docstring, which this module's own docstring
    legitimately contains while explaining why it avoids them)."""
    import ast
    import inspect

    from services.mailbox.gmail import gmail_client as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert "googleapiclient" not in imported_roots
    assert "google_auth_oauthlib" not in imported_roots
    assert "urllib" in imported_roots
