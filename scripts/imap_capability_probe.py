#!/usr/bin/env python3
"""Safe, read-only diagnostic for the `matt@noust.ai` IMAP mailbox (CD-6
GUI-operations-foundation follow-on WO — second mailbox provider).

**Run this ONLY after real credentials exist** at
``/opt/bagman/secrets/mail/noustai/{username,password}`` (or the
directory named by ``BAGMAN_NOUSTAI_MAIL_SECRETS_DIR`` — mirrors
``services.mailbox.imap.secrets._SECRETS_DIR_ENV_VAR``, purely for a
disposable/dev run). Connects via the REAL
``services.mailbox.imap.imap_client.ImapClient`` (proving the wrapper
works end-to-end against the real server) and prints:

* IMAP hostname/port/TLS mode.
* Negotiated TLS version/cipher (from a normal handshake — no private
  key material).
* Certificate validation result: success/failure, subject/issuer CN.
* The raw ``CAPABILITY`` response.
* Which SASL/auth mechanisms are advertised (if any, beyond plain
  ``LOGIN``).
* Whether ``UIDPLUS`` is in the capability list.
* Whether ``MOVE``/``IDLE``/other extensions are advertised
  (informational only — never used by this codebase).
* Folder names/flags only — NO message content, NO subjects, NO sender
  addresses (this is a capability probe, not a content dump).

**NEVER prints the password**, under any circumstance, including in a
traceback on failure — only the username is printed, for confirmation
context. Exits cleanly with a clear ``NOT_CONFIGURED`` message (never a
raw traceback) if the credential files are absent.

How the PL should invoke this once credentials exist
------------------------------------------------------------------------
Via `docker exec` against the running BAGMAN API container (mirrors how
every other one-off diagnostic in this codebase is run — this script has
no other entrypoint):

    docker exec -it <bagman-api-container> python3 scripts/imap_capability_probe.py

Or, from a shell already inside the container / a dev checkout with the
real secrets directory populated:

    python3 scripts/imap_capability_probe.py

Optional overrides (mirrors the module's own env-var-override-for-tests
discipline — production never needs these):

    BAGMAN_NOUSTAI_MAIL_SECRETS_DIR=/custom/path \\
    BAGMAN_NOUSTAI_IMAP_HOST=mail.noust.ai \\
    BAGMAN_NOUSTAI_IMAP_PORT=993 \\
        python3 scripts/imap_capability_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `python3 scripts/imap_capability_probe.py` from the
# repository root without needing the package installed — mirrors every
# other standalone script's own bootstrap in this repository.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.mailbox.imap.imap_adapter import _imap_host, _imap_port  # noqa: E402
from services.mailbox.imap.imap_client import ImapClient, ImapOutcomeStatus  # noqa: E402
from services.mailbox.imap.secrets import read_noustai_imap_credentials  # noqa: E402

_SPECIAL_USE_EXTENSION = "SPECIAL-USE"
_INFORMATIONAL_EXTENSIONS = ("MOVE", "IDLE", "UIDPLUS", "CONDSTORE", "QRESYNC", "LITERAL+", "ID", "ENABLE")


def main() -> int:
    credentials = read_noustai_imap_credentials()
    if credentials is None:
        print("NOT_CONFIGURED — no username/password found at the configured secrets directory.")
        print("Place both files (mode 0600, in a 0700 directory) before running this probe.")
        return 1

    host = _imap_host()
    port = _imap_port()
    print(f"IMAP host:     {host}")
    print(f"IMAP port:     {port}")
    print("TLS mode:      implicit TLS (IMAP4_SSL), full cert-chain + hostname validation")
    print(f"Username:      {credentials.username}")
    print("Password:      [REDACTED]")
    print()

    client = ImapClient()
    connect_result = client.connect_and_login(
        host=host, port=port, username=credentials.username, password=credentials.password
    )

    if connect_result.status != ImapOutcomeStatus.OK or connect_result.session is None:
        print(f"CONNECT/LOGIN FAILED: {connect_result.status.value}")
        if connect_result.error_detail:
            print(f"  detail: {connect_result.error_detail}")
        return 1

    print("Connect + login: OK")
    print(f"  TLS version:        {connect_result.tls_version}")
    print(f"  TLS cipher:         {connect_result.tls_cipher}")
    print(f"  Certificate subject: {connect_result.peer_cert_subject}")
    print(f"  Certificate issuer:  {connect_result.peer_cert_issuer}")
    print()

    session = connect_result.session
    exit_code = 0
    try:
        capability_result = client.capability(session)
        if capability_result.status != ImapOutcomeStatus.OK:
            print(f"CAPABILITY FAILED: {capability_result.status.value} — {capability_result.error_detail}")
            exit_code = 1
        else:
            caps = capability_result.capabilities
            print(f"Raw CAPABILITY response ({len(caps)} tokens):")
            print(f"  {' '.join(caps)}")
            print()
            upper_caps = {c.upper() for c in caps}
            auth_mechanisms = sorted(c for c in caps if c.upper().startswith("AUTH="))
            print(f"SASL/auth mechanisms beyond plain LOGIN: {auth_mechanisms or '(none advertised)'}")
            print(f"SPECIAL-USE advertised: {_SPECIAL_USE_EXTENSION in upper_caps}")
            for ext in _INFORMATIONAL_EXTENSIONS:
                print(f"  {ext} advertised: {ext in upper_caps}")
            print()

        folder_list_result = client.list_folders(session)
        if folder_list_result.status != ImapOutcomeStatus.OK:
            print(f"LIST FAILED: {folder_list_result.status.value} — {folder_list_result.error_detail}")
            exit_code = 1
        else:
            print(f"Folders ({len(folder_list_result.folders)}) — names/flags only, no content:")
            for info in folder_list_result.folders:
                flags = ", ".join(info.flags) if info.flags else "(no flags)"
                print(f"  {info.name!r} [{flags}] (delimiter={info.delimiter!r})")
    finally:
        client.logout(session)
        print()
        print("Logged out cleanly.")

    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never expose an unnecessary raw traceback
        # Defensive final backstop: even an unexpected internal failure
        # must never risk echoing a password into a traceback. The
        # exception's own string form is printed (never its full
        # traceback), and every code path above already redacts
        # password material from anything that could reach here.
        print(f"Unexpected failure: {exc}")
        raise SystemExit(1)
