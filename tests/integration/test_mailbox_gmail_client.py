"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.gmail_client` — the real Gmail OAuth2/API
wrapper's own pure helpers, authorize-URL construction, outcome-status
mapping, and read-only structural guarantee. NO real network call to
Google is ever made by this test file (per this delivery's own hard
constraint — no live credentials exist).
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import pytest

from services.mailbox.gmail.gmail_client import (
    DEFAULT_METADATA_HEADERS,
    GMAIL_MESSAGES_LIST_URL,
    GMAIL_PROFILE_URL,
    GmailClient,
    GmailClientProtocol,
    GmailOAuthClient,
    GmailOutcomeStatus,
    _MAX_ERROR_BODY_BYTES,
    _classify_http_error,
    _decode_base64url,
    _reason_for_403,
    _status_for_transport_error,
)
from services.mailbox.gmail.secrets import GmailAppCredentials


def _fake_http_error(code: int, *, body: bytes = b"{}", headers: Optional[dict] = None) -> urllib.error.HTTPError:
    import email.message
    import io

    hdrs = None
    if headers:
        hdrs = email.message.Message()
        for key, value in headers.items():
            hdrs[key] = value
    return urllib.error.HTTPError(url="https://example.com", code=code, msg="err", hdrs=hdrs, fp=io.BytesIO(body))


def _quota_body(reason: str) -> bytes:
    """A real Google-shaped 403 JSON body carrying `reason` — the exact
    shape Google's own Gmail API returns for both transient quota
    exhaustion (`rateLimitExceeded`/`userRateLimitExceeded`) and genuine
    permission failures (any other `reason`)."""
    return json.dumps(
        {"error": {"errors": [{"domain": "usageLimits", "reason": reason, "message": "Quota exceeded for..."}], "code": 403, "message": "..."}}
    ).encode("utf-8")


#: The REAL production message body text from the incident this
#: delivery fixes — see WO/CLAUDE.md quoted body — verbatim, not a
#: paraphrase, so `_production_quota_body` below reproduces the exact
#: failing shape as closely as this test file reasonably can.
_REAL_QUOTA_MESSAGE_TEXT = (
    "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user' "
    "of service 'gmail.googleapis.com' for consumer 'project_number:393131324766'."
)


def _production_quota_body(reason: str, *, second_entry: Optional[dict] = None) -> bytes:
    """A real, valid, Google-shaped 403 JSON body — same `error.errors[]`
    structure as `_quota_body` above, but comfortably OVER 500 characters
    once serialized (matching the real production incident's own body,
    which is itself long because Google's quota messages are verbose and
    the message text is repeated at both `error.message` and
    `error.errors[0].message`, plus a real `extendedHelp` URL and a
    `status` field — all fields a genuine Google 403 quota response
    carries). This is the load-bearing fixture for the >500-char
    regression: the OLD `_read_body()[:500]`-then-`json.loads()` code
    path truncates this body mid-string (see
    `test_403_rate_limit_exceeded_with_body_over_500_chars_still_classifies_as_rate_limited`
    for the direct proof), which is exactly the real incident's root
    cause.

    `second_entry`, when given, is appended as a SECOND entry in
    `errors[]` — used by the "any entry wins, not just the first" proof
    below."""
    entries = [
        {
            "message": _REAL_QUOTA_MESSAGE_TEXT,
            "domain": "usageLimits",
            "reason": reason,
            "extendedHelp": "https://developers.google.com/gmail/api/reference/quota",
        }
    ]
    if second_entry is not None:
        entries.append(second_entry)
    body = {
        "error": {
            "code": 403,
            "message": _REAL_QUOTA_MESSAGE_TEXT,
            "errors": entries,
            "status": "RESOURCE_EXHAUSTED",
        }
    }
    encoded = json.dumps(body).encode("utf-8")
    assert len(encoded) > 500, "fixture must exceed 500 chars to be the load-bearing regression it claims to be"
    return encoded


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
        (403, GmailOutcomeStatus.PERMISSION_ERROR),  # plain `{}` body -> no recognized structured reason -> fail-closed
        (404, GmailOutcomeStatus.NOT_FOUND),
        (429, GmailOutcomeStatus.RATE_LIMITED),
        (500, GmailOutcomeStatus.PROVIDER_ERROR),
        (503, GmailOutcomeStatus.PROVIDER_ERROR),
    ],
)
def test_classify_http_error_status_mapping(code, expected):
    assert _classify_http_error(_fake_http_error(code)).status == expected


# -- CD-6 structured-403 quota classification -----------------------------
#
# A real Gmail historical sweep once hit Google's own per-user API quota
# mid-round (HTTP 403, structured `usageLimits`/`rateLimitExceeded`
# body) — the old `_status_for_http_error` classified EVERY 403 as
# `PERMISSION_ERROR` regardless of body content, which caused
# `services/mailbox/sweep.py` to mark a genuinely healthy mailbox's
# `connection_state -> ERROR` over nothing more than transient quota
# exhaustion. These tests prove the fix's full decision table, and the
# single-body-read refactor that makes it safe.


def test_403_rate_limit_exceeded_reason_classified_as_rate_limited_not_permission_error():
    classified = _classify_http_error(_fake_http_error(403, body=_quota_body("rateLimitExceeded")))
    assert classified.status == GmailOutcomeStatus.RATE_LIMITED
    assert classified.status != GmailOutcomeStatus.PERMISSION_ERROR


def test_403_user_rate_limit_exceeded_reason_classified_as_rate_limited():
    classified = _classify_http_error(_fake_http_error(403, body=_quota_body("userRateLimitExceeded")))
    assert classified.status == GmailOutcomeStatus.RATE_LIMITED


def test_403_genuine_permission_reason_classified_as_permission_error():
    classified = _classify_http_error(_fake_http_error(403, body=_quota_body("insufficientPermissions")))
    assert classified.status == GmailOutcomeStatus.PERMISSION_ERROR


def test_403_forbidden_reason_classified_as_permission_error():
    classified = _classify_http_error(_fake_http_error(403, body=_quota_body("forbidden")))
    assert classified.status == GmailOutcomeStatus.PERMISSION_ERROR


@pytest.mark.parametrize(
    "malformed_body",
    [
        b"not json at all",
        b"",
        json.dumps({"error": "not the expected shape"}).encode("utf-8"),
        json.dumps({"error": {"errors": "not a list"}}).encode("utf-8"),
        json.dumps({"error": {"errors": []}}).encode("utf-8"),
        json.dumps({"unexpected": "shape"}).encode("utf-8"),
    ],
)
def test_403_malformed_or_unexpected_body_shape_fails_closed_to_permission_error(malformed_body):
    """Malformed/unparseable/unexpected-shape 403 bodies must never
    raise, and must fail closed to `PERMISSION_ERROR` (architect's own
    explicit instruction) — never silently treated as transient."""
    classified = _classify_http_error(_fake_http_error(403, body=malformed_body))
    assert classified.status == GmailOutcomeStatus.PERMISSION_ERROR


# -- CD-6 real-incident regression: >500-char quota body -----------------
#
# THE load-bearing regression test for this delivery. The real
# production incident (222-candidate MUST_READ backfill, candidate 78)
# hit Google's own genuine quota-exceeded 403 with a body over 500
# characters; the OLD code truncated that body to 500 chars BEFORE
# `json.loads`, which cut the JSON off mid-structure, made `_reason_for_403`
# raise+catch `JSONDecodeError` and return `None`, and fell through to the
# fail-closed default `PERMISSION_ERROR` — so the bounded-retry-on-
# RATE_LIMITED logic never fired and the whole backfill call raised
# `ConflictError`. These tests prove: (a) the exact truncate-then-parse
# failure mode this fixture reproduces, via `_reason_for_403` itself
# (unchanged logic) fed a manually-truncated string, and (b) the NEW code
# — fed the full, untruncated (up to `_MAX_ERROR_BODY_BYTES`) body via
# `_read_body_bounded` — correctly classifies `RATE_LIMITED`.


def test_403_rate_limit_exceeded_with_body_over_500_chars_still_classifies_as_rate_limited():
    """The single most important test in this delivery."""
    raw = _production_quota_body("rateLimitExceeded")
    assert len(raw) > 500

    # (a) Direct proof of the OLD bug's mechanics: `_reason_for_403`'s
    # own logic is UNCHANGED by this delivery — feed it the SAME
    # 500-char truncation the old `_read_body()` used to produce, and it
    # returns `None` (a `JSONDecodeError` on the truncated JSON), which
    # is exactly the fail-closed-to-PERMISSION_ERROR path the real
    # incident hit.
    truncated_the_old_way = raw.decode("utf-8", errors="replace")[:500]
    assert _reason_for_403(truncated_the_old_way) is None

    # (b) The NEW code, given the same real body via the real
    # `_classify_http_error` entry point (bounded to `_MAX_ERROR_BODY_BYTES`,
    # never to 500 chars), classifies correctly.
    classified = _classify_http_error(_fake_http_error(403, body=raw))
    assert classified.status == GmailOutcomeStatus.RATE_LIMITED
    assert classified.status != GmailOutcomeStatus.PERMISSION_ERROR
    assert "rateLimitExceeded" in classified.error_detail


def test_403_user_rate_limit_exceeded_with_body_over_500_chars_still_classifies_as_rate_limited():
    raw = _production_quota_body("userRateLimitExceeded")
    assert len(raw) > 500
    truncated_the_old_way = raw.decode("utf-8", errors="replace")[:500]
    assert _reason_for_403(truncated_the_old_way) is None  # same truncate-then-parse failure mode

    classified = _classify_http_error(_fake_http_error(403, body=raw))
    assert classified.status == GmailOutcomeStatus.RATE_LIMITED


def test_403_multi_entry_over_500_chars_any_entry_wins_not_just_first():
    """`errors[0]` is a genuine non-quota reason, `errors[1]` is the
    transient-quota reason — proves "any entry wins", not just the
    first, on a real over-500-char body."""
    raw = _production_quota_body("insufficientPermissions", second_entry={"domain": "usageLimits", "reason": "rateLimitExceeded", "message": _REAL_QUOTA_MESSAGE_TEXT})
    assert len(raw) > 500

    classified = _classify_http_error(_fake_http_error(403, body=raw))
    assert classified.status == GmailOutcomeStatus.RATE_LIMITED


def test_403_daily_limit_exceeded_not_added_to_transient_quota_reasons():
    """`dailyLimitExceeded` is a hard daily/project cap, deliberately
    kept conservatively distinct from the short transient per-minute
    limiter — must NOT be treated as `RATE_LIMITED` unless a future
    delivery explicitly decides otherwise."""
    classified = _classify_http_error(_fake_http_error(403, body=_quota_body("dailyLimitExceeded")))
    assert classified.status == GmailOutcomeStatus.PERMISSION_ERROR


def test_403_message_text_only_quota_wording_never_influences_classification():
    """A 403 whose `error.errors[]` has NO `reason` field at all (or a
    non-quota one), but whose `message` text literally contains
    quota-sounding human wording — must still fail closed to
    `PERMISSION_ERROR`. Classification is driven ONLY by the structured
    `reason` field, never by `message` text content."""
    body = json.dumps(
        {
            "error": {
                "code": 403,
                "message": "Rate Limit Exceeded — please slow down your requests to this API immediately.",
                "errors": [
                    {
                        "domain": "usageLimits",
                        "message": "Rate Limit Exceeded — please slow down your requests to this API immediately.",
                        # deliberately NO "reason" key at all
                    }
                ],
            }
        }
    ).encode("utf-8")
    classified = _classify_http_error(_fake_http_error(403, body=body))
    assert classified.status == GmailOutcomeStatus.PERMISSION_ERROR


def test_403_oversized_body_fails_closed_without_attempting_to_parse_a_truncated_document():
    """A body GENUINELY larger than `_MAX_ERROR_BODY_BYTES` — even
    though it would otherwise be valid, quota-shaped JSON — must fail
    closed to `PERMISSION_ERROR`, and the diagnostic must name the bound
    being exceeded, never dump the oversized body itself."""
    # Pad the message field until the whole body is genuinely larger
    # than the bound, while still being syntactically valid JSON on its
    # own (never parsed, since it's oversized — but constructing it as
    # valid JSON proves this is a SIZE-only rejection, not a
    # malformed-JSON rejection).
    padding = "x" * (_MAX_ERROR_BODY_BYTES + 1000)
    oversized_body = json.dumps(
        {"error": {"errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded", "message": padding}], "code": 403}}
    ).encode("utf-8")
    assert len(oversized_body) > _MAX_ERROR_BODY_BYTES

    classified = _classify_http_error(_fake_http_error(403, body=oversized_body))
    assert classified.status == GmailOutcomeStatus.PERMISSION_ERROR
    assert "exceed" in classified.error_detail.lower()
    # Never dumps the (huge) body itself into the diagnostic.
    assert "x" * 100 not in classified.error_detail
    assert len(classified.error_detail) < 200


def test_403_retry_after_unaffected_by_body_classification_change_both_fields_together():
    """A 403 with structured `reason=rateLimitExceeded` AND an HTTP
    `Retry-After: 30` header, using the real over-500-char production
    body shape, must produce BOTH `status=RATE_LIMITED` AND
    `retry_after_seconds=30.0` together — proving the body-classification
    rewrite left `Retry-After` handling completely unaffected."""
    raw = _production_quota_body("rateLimitExceeded")
    classified = _classify_http_error(_fake_http_error(403, body=raw, headers={"Retry-After": "30"}))
    assert (classified.status, classified.retry_after_seconds) == (GmailOutcomeStatus.RATE_LIMITED, 30.0)


# -- CD-6: metadata and raw callers share the identical classifier -------


class _RaisingUrlopen:
    """Monkeypatch target for `urllib.request.urlopen` that always
    raises the SAME `HTTPError` (bounded body, mirrors the real
    incident's shape) — used to prove `fetch_message_metadata` and
    `fetch_message_raw` both route through the one shared
    `_classify_http_error` (no duplicated per-method classification
    logic)."""

    def __init__(self, exc_factory) -> None:
        self._exc_factory = exc_factory
        self.calls = 0

    def __call__(self, request, timeout=None):  # noqa: ARG002 - matches urlopen's own signature
        self.calls += 1
        raise self._exc_factory()


def test_metadata_and_raw_real_client_share_identical_classifier_for_over_500_char_403(monkeypatch):
    """Confirmation test: both `GmailClient.fetch_message_metadata` and
    `GmailClient.fetch_message_raw` produce `RATE_LIMITED` for the exact
    same real, over-500-char quota-shaped 403 — via the one shared
    `_classify_http_error`, never a duplicated per-method
    classification path."""
    raw_body = _production_quota_body("rateLimitExceeded")

    def _new_exc():
        return _fake_http_error(403, body=raw_body)

    raising = _RaisingUrlopen(_new_exc)
    monkeypatch.setattr(urllib.request, "urlopen", raising)
    client = GmailClient()

    metadata_result = client.fetch_message_metadata(access_token="tok", message_id="m1")
    raw_result = client.fetch_message_raw(access_token="tok", message_id="m1")

    assert metadata_result.status == GmailOutcomeStatus.RATE_LIMITED
    assert raw_result.status == GmailOutcomeStatus.RATE_LIMITED
    assert raising.calls == 2


def test_429_still_classified_rate_limited_unchanged():
    classified = _classify_http_error(_fake_http_error(429, body=b"{}"))
    assert classified.status == GmailOutcomeStatus.RATE_LIMITED


@pytest.mark.parametrize(
    "code,body",
    [
        (403, _quota_body("rateLimitExceeded")),
        (429, b"{}"),
    ],
)
def test_retry_after_header_preserved_when_present(code, body):
    classified = _classify_http_error(_fake_http_error(code, body=body, headers={"Retry-After": "17"}))
    assert classified.retry_after_seconds == 17.0


@pytest.mark.parametrize(
    "code,body",
    [
        (403, _quota_body("rateLimitExceeded")),
        (429, b"{}"),
    ],
)
def test_retry_after_none_when_header_absent(code, body):
    classified = _classify_http_error(_fake_http_error(code, body=body))
    assert classified.retry_after_seconds is None


def test_error_detail_still_populated_after_single_read_refactor():
    """The exact trap the PL flagged: `_classify_http_error` reads the
    body exactly ONCE and derives `error_detail` from that same read —
    proving the single-read refactor didn't silently break the existing
    diagnostic string (previously produced by a SEPARATE `_read_body`
    call at each call site, which would now see an exhausted, already-
    consumed stream and return an empty string)."""
    classified = _classify_http_error(_fake_http_error(403, body=_quota_body("rateLimitExceeded")))
    assert "403" in classified.error_detail
    assert "rateLimitExceeded" in classified.error_detail


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


def _authorize_scope_tokens(client: GmailOAuthClient) -> list[str]:
    url = client.build_authorize_url(state="s", redirect_uri="https://localhost:8543/cb")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return params["scope"][0].split(" ")


def _gmail_oauth_client() -> GmailOAuthClient:
    return GmailOAuthClient(credentials_provider=lambda: GmailAppCredentials(client_id="id", client_secret="s"))


def test_authorize_url_scope_excludes_openid():
    """Identity verification now goes through Gmail's own
    `users.getProfile` (covered by `gmail.readonly`) — the authorize URL
    must never request the generic OAuth2 `openid` scope."""
    assert "openid" not in _authorize_scope_tokens(_gmail_oauth_client())


def test_authorize_url_scope_excludes_email():
    assert "email" not in _authorize_scope_tokens(_gmail_oauth_client())


def test_authorize_url_scope_excludes_profile():
    assert "profile" not in _authorize_scope_tokens(_gmail_oauth_client())


# -- identity verification: users.getProfile, never the old userinfo URL --


def test_identity_verification_targets_gmail_users_me_profile():
    """The ONE identity-verification endpoint this module calls —
    replaces the old generic `https://www.googleapis.com/oauth2/v2/userinfo`
    (removed entirely — see below)."""
    assert GMAIL_PROFILE_URL == "https://www.googleapis.com/gmail/v1/users/me/profile"


def test_userinfo_url_constant_no_longer_exists():
    from services.mailbox.gmail import gmail_client as module

    assert not hasattr(module, "USERINFO_URL")


def test_gmail_client_implements_get_profile():
    """`get_profile` — not `GmailOAuthClientProtocol.get_identity` (which
    no longer exists at all, see module docstring's "ONE identity-
    verification code path" doctrine) — lives on `GmailClientProtocol`/
    `GmailClient`, the Gmail-API-authenticated read surface, since
    `users.getProfile` is a Gmail API call, not an OAuth-identity call."""
    assert hasattr(GmailClient, "get_profile")
    assert hasattr(GmailClientProtocol, "get_profile")


def test_gmail_oauth_client_protocol_no_longer_declares_get_identity():
    from services.mailbox.gmail.gmail_client import GmailOAuthClientProtocol

    assert not hasattr(GmailOAuthClientProtocol, "get_identity")
    assert not hasattr(GmailOAuthClient, "get_identity")


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


# ---------------------------------------------------------------------
# CD-6 ALL_RECEIVED discovery-model fix — real `GmailClient.list_messages`
# HTTP request shape. These drive the REAL client (never `FakeGmailClient`)
# against a monkeypatched `urllib.request.urlopen`, capturing the exact
# constructed `Request` so the real query-string parameters can be
# asserted directly — no real network call is ever made.
# ---------------------------------------------------------------------


class _FakeUrlopenResponse:
    """A minimal stand-in for the `http.client.HTTPResponse` context
    manager `urllib.request.urlopen` normally returns — just enough for
    `GmailClient._get_json`'s own `with ... as response: response.read()`
    usage."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeUrlopenResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _capture_request_url(monkeypatch, *, payload: dict) -> dict:
    """Monkeypatches `urllib.request.urlopen` (the exact call
    `GmailClient._get_json` makes) to record the real constructed
    `Request`'s `full_url` and return `payload` as the JSON body. Returns
    a mutable dict the caller reads `["request"]` from after the real
    client call completes."""
    captured: dict = {}

    def _fake_urlopen(request, timeout=None):  # noqa: ARG001 - timeout unused, matches urlopen's own signature
        captured["request"] = request
        return _FakeUrlopenResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return captured


def test_list_messages_real_client_includes_spam_trash_true_param(monkeypatch):
    """13. `include_spam_trash=True` must produce a real
    `includeSpamTrash=true` query parameter on the constructed request —
    the ONE mechanism that structurally guarantees Spam/Trash coverage
    for the `ALL_RECEIVED` stream (Gmail's `messages.list` otherwise
    excludes those dispositions from normal results even with no
    `labelIds` restriction at all)."""
    captured = _capture_request_url(monkeypatch, payload={"messages": []})
    client = GmailClient()

    result = client.list_messages(access_token="tok", label_id=None, include_spam_trash=True, query="after:100")

    assert result.status == GmailOutcomeStatus.OK
    parsed = urllib.parse.urlparse(captured["request"].full_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.googleapis.com"
    assert parsed.path == urllib.parse.urlparse(GMAIL_MESSAGES_LIST_URL).path
    params = urllib.parse.parse_qs(parsed.query)
    assert params["includeSpamTrash"] == ["true"]
    assert params["q"] == ["after:100"]


def test_list_messages_real_client_omits_label_ids_param_when_label_id_is_none(monkeypatch):
    """14. `label_id=None` must produce a constructed request carrying NO
    `labelIds` parameter at all — never the string `"None"`, never a
    fabricated value, and (per this delivery's own hard boundary) the
    synthetic `ALL_RECEIVED` stream identity must never itself be sent
    as a real `labelIds` value."""
    captured = _capture_request_url(monkeypatch, payload={"messages": []})
    client = GmailClient()

    client.list_messages(access_token="tok", label_id=None, include_spam_trash=True)

    parsed = urllib.parse.urlparse(captured["request"].full_url)
    params = urllib.parse.parse_qs(parsed.query)
    assert "labelIds" not in params


def test_list_messages_real_client_still_includes_label_ids_when_provided(monkeypatch):
    """Backward-compatibility proof: `label_id`, when explicitly
    provided, still produces a single real `labelIds` value exactly as
    before — the parameter is kept for flexibility, even though nothing
    in this delivery calls it that way any more."""
    captured = _capture_request_url(monkeypatch, payload={"messages": []})
    client = GmailClient()

    client.list_messages(access_token="tok", label_id="INBOX")

    parsed = urllib.parse.urlparse(captured["request"].full_url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["labelIds"] == ["INBOX"]


def test_list_messages_real_client_after_query_construction_unchanged(monkeypatch):
    """15. The existing governed `after:<epoch>` query construction is
    unchanged in behavior for the real client — the `q` parameter is
    forwarded verbatim, whatever the caller builds it as (the ALL_RECEIVED
    discovery-model fix never touches this mechanic)."""
    captured = _capture_request_url(monkeypatch, payload={"messages": []})
    client = GmailClient()

    client.list_messages(access_token="tok", label_id=None, include_spam_trash=True, query="after:1700000000")

    parsed = urllib.parse.urlparse(captured["request"].full_url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["q"] == ["after:1700000000"]


def test_list_messages_real_client_sends_bearer_authorization_header(monkeypatch):
    captured = _capture_request_url(monkeypatch, payload={"messages": []})
    client = GmailClient()

    client.list_messages(access_token="secret-token", label_id=None, include_spam_trash=True)

    assert captured["request"].get_header("Authorization") == "Bearer secret-token"
