"""``services.mailbox.gmail`` — the Gmail API v1 provider adapter for
BAGMAN's THIRD mailbox provider (CD-6 GUI-operations-foundation follow-
on WO — "Gmail mailbox provider (real API, two accounts)").

Two independent Gmail accounts (``mgs241171@gmail.com`` and
``matt.george.scott@gmail.com``) will each be connected as their own
``MailboxSource`` row, governed by ONE shared Gmail app credential
(``client_id``/``client_secret`` — one Google Cloud OAuth app, exactly
the same "one app, N mailboxes" shape
``services.mailbox.microsoft.secrets`` already establishes for
Microsoft) and TWO independent, per-mailbox-id-scoped OAuth token
pairs.

Mirrors ``services.mailbox.microsoft`` package-for-package: a secrets
module, an OAuth CSRF-state module, a real stdlib-HTTP-only network-
speaking client, a deterministic fake client, a provider adapter
satisfying ``services/mailbox/sweep.py``'s own ``_AdapterProtocol``
duck-type, and a provisional (always-``UNKNOWN``) authentication
selector. See each sibling module's own docstring for the full
reasoning; nothing is re-explained here.

Hard constraint — stdlib HTTP only, no Google SDK
------------------------------------------------------
No module in this package may ever import ``googleapiclient`` or
``google_auth_oauthlib`` (both are repo-wide-forbidden —
``tests/integration/test_architecture_boundaries.py``
``::test_no_forbidden_mailbox_or_provider_sdk_imported_anywhere_in_the_repo``
— with NO sanctioned carve-out for this package, unlike ``imaplib``'s
narrow one for ``services/mailbox/imap/``). Every OAuth2/Gmail-API call
in this package is plain ``urllib.request``/``http.client`` + ``json``
against Gmail's own REST endpoints — see ``gmail_client.py``'s own
module docstring for the exact URLs and the same "why stdlib, not the
provider's own SDK" reasoning ``services.mailbox.microsoft.graph_client``
already documents (which applies identically here).

No real Google Cloud OAuth app exists yet (this delivery's own hard
constraint) — every test in this codebase drives
``FakeGmailOAuthClient``/``FakeGmailClient``
(``services/mailbox/gmail/fake_gmail_client.py``) instead of the real,
network-speaking classes in ``gmail_client.py``; this delivery never
attempts a real network call to Google.
"""
from __future__ import annotations
