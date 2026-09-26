"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.imap.imap_client` — the real IMAP wrapper's own pure
parsing helpers, TLS-context security defaults, and password-redaction
discipline. NO real network/TLS connection to any external host is ever
made by this test file (the one connection attempt below targets a
closed local port purely to prove the failure path never hangs/crashes
and never falls back to plaintext — see
`test_connect_to_closed_local_port_fails_fast_never_hangs_never_plaintext`).
"""
from __future__ import annotations

import ssl

from services.mailbox.imap.imap_client import (
    ImapClient,
    ImapOutcomeStatus,
    _classify_transport_exception,
    _format_cert_name,
    _headers_from_bytes,
    _parse_list_line,
    _parse_uid_fetch_response,
    _redact,
)


def test_redact_scrubs_the_password_from_arbitrary_text():
    text = "IMAP login rejected for 'matt@noust.ai': b'hunter2 was wrong'"
    assert "hunter2" not in _redact(text, "hunter2")


def test_redact_is_a_no_op_for_empty_or_none_secrets():
    assert _redact("hello", "", None) == "hello"


def test_parse_list_line_extracts_name_flags_delimiter():
    info = _parse_list_line(rb'(\HasNoChildren \Junk) "/" "Junk"')
    assert info is not None
    assert info.name == "Junk"
    assert info.delimiter == "/"
    assert "\\Junk" in info.flags
    assert "\\HasNoChildren" in info.flags


def test_parse_list_line_handles_unquoted_name():
    info = _parse_list_line(rb'(\HasNoChildren) "/" INBOX')
    assert info is not None
    assert info.name == "INBOX"


def test_parse_list_line_returns_none_for_garbage():
    assert _parse_list_line(b"not a LIST response at all") is None


def test_parse_uid_fetch_response_extracts_uid_and_literal():
    raw_header_block = b"Subject: Hello\r\nFrom: a@b.com\r\n\r\n"
    data = [(b"1 (UID 42 BODY[HEADER] {35}", raw_header_block), b")"]
    parsed = _parse_uid_fetch_response(data)
    assert parsed == [(42, raw_header_block)]


def test_parse_uid_fetch_response_skips_non_tuple_entries():
    data = [b")", b"some other line"]
    assert _parse_uid_fetch_response(data) == []


def test_headers_from_bytes_parses_a_raw_header_block_into_name_value_pairs():
    raw = b"Subject: Hi there\r\nFrom: a@b.com\r\nMessage-ID: <abc@b.com>\r\n\r\n"
    headers = _headers_from_bytes(raw)
    as_dict = {h["name"]: h["value"] for h in headers}
    assert as_dict["Subject"] == "Hi there"
    assert as_dict["From"] == "a@b.com"
    assert as_dict["Message-ID"] == "<abc@b.com>"


def test_format_cert_name_renders_subject_tuple():
    subject = (((("commonName", "mail.noust.ai"),)),)
    assert _format_cert_name(subject) == "commonName=mail.noust.ai"


def test_format_cert_name_returns_none_for_empty():
    assert _format_cert_name(None) is None
    assert _format_cert_name(()) is None


def test_classify_transport_exception_maps_timeout():
    import socket

    assert _classify_transport_exception(socket.timeout("timed out")) == ImapOutcomeStatus.TIMEOUT


def test_classify_transport_exception_maps_tls_failure():
    assert _classify_transport_exception(ssl.SSLError("bad cert")) == ImapOutcomeStatus.TLS_VALIDATION_FAILED


def test_classify_transport_exception_defaults_to_transport_error():
    assert _classify_transport_exception(OSError("connection refused")) == ImapOutcomeStatus.TRANSPORT_ERROR


def test_connect_to_closed_local_port_fails_fast_never_hangs_never_plaintext():
    """Proves the real client never falls back to a plaintext/insecure
    connection when a genuine transport failure occurs — a closed local
    port (nothing listening) must fail as TRANSPORT_ERROR/TIMEOUT, never
    silently 'succeed' via some non-TLS path (there is no such path in
    this module at all — see its own module docstring)."""
    client = ImapClient()
    result = client.connect_and_login(
        host="127.0.0.1", port=1, username="nobody", password="not-a-real-secret", timeout_seconds=2.0
    )
    assert result.status in (ImapOutcomeStatus.TRANSPORT_ERROR, ImapOutcomeStatus.TIMEOUT)
    assert result.session is None
    # The (fake, test-only) password must never appear in the error detail.
    assert result.error_detail is not None
    assert "not-a-real-secret" not in result.error_detail


def test_ssl_default_context_has_full_verification_enabled():
    """A regression guard, not a test of this module's own logic per se
    — asserts the exact security property `ImapClient.connect_and_login`
    depends on: `ssl.create_default_context()` enables full chain
    validation AND hostname verification by construction. If a future
    Python/OpenSSL default ever weakened this, this test would catch
    it."""
    context = ssl.create_default_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_imap_client_never_exposes_a_mutating_method():
    """The read-only guarantee is structural — see module docstring's
    own 'no generic raw-command escape hatch' requirement. Assert none
    of the forbidden method names exist on the class at all."""
    forbidden = {"store", "copy", "move", "expunge", "append", "delete", "uid_store", "uid_copy", "uid_move"}
    public_methods = {name for name in dir(ImapClient) if not name.startswith("_")}
    assert forbidden.isdisjoint(public_methods)


def test_imap_client_source_never_issues_a_fetch_without_peek():
    """Every `UID FETCH` command literal in the real client's own source
    must use `BODY.PEEK[...]`, never plain `BODY[...]` (which implicitly
    sets `\\Seen` as a side effect — explicitly forbidden). A static
    source check rather than a mocked-socket test — this module never
    fabricates a fake `imaplib` connection; the real security property
    is that no code path in this FILE ever constructs a non-PEEK fetch
    string at all."""
    import inspect
    import re

    from services.mailbox.imap import imap_client as module

    source = inspect.getsource(module)
    # The FETCH "items" argument is always the LAST quoted string literal
    # on a `uid("FETCH", ...)` call — capture that literal specifically
    # (never the whole call, whose earlier arguments may themselves
    # legitimately contain a `)`, e.g. `str(uid)`).
    fetch_items = re.findall(r'uid\("FETCH",\s*[^,]+,\s*"([^"]+)"\)', source)
    assert fetch_items, "expected at least one UID FETCH call in imap_client.py"
    for item in fetch_items:
        assert "BODY.PEEK" in item, f"found a UID FETCH items string without BODY.PEEK: {item!r}"
        assert re.search(r"(?<!\.PEEK)\bBODY\[", item) is None, f"found a non-PEEK BODY[...] fetch item: {item!r}"


def test_imap_client_source_never_contains_a_mutating_imap_verb():
    """Bounded, source-level self-check: none of the mutating IMAP verbs
    (STORE/COPY/MOVE/EXPUNGE/APPEND/DELETE) ever appear as a command
    this client issues."""
    import inspect

    from services.mailbox.imap import imap_client as module

    source = inspect.getsource(module)
    for verb in ('"STORE"', '"COPY"', '"MOVE"', '"EXPUNGE"', '"APPEND"', '"DELETE"'):
        assert verb not in source, f"found a mutating IMAP verb {verb} referenced in imap_client.py"
