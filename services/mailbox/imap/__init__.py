"""``services.mailbox.imap`` — the plain-IMAP provider adapter for
``matt@noust.ai`` (mail.noust.ai), BAGMAN's SECOND mailbox provider
(CD-6 GUI-operations-foundation follow-on WO).

Mirrors ``services.mailbox.microsoft`` package-for-package: a secrets
module, a real network-speaking client, a deterministic fake client, a
provider adapter satisfying ``services/mailbox/sweep.py``'s own
``_AdapterProtocol`` duck-type, and a provisional authentication
selector. See each sibling module's own docstring for the full
reasoning; nothing is re-explained here.
"""
from __future__ import annotations
