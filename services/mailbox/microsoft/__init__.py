"""Microsoft Graph-specific mailbox adapter code (CD-6 Slice 4).

Everything Microsoft/Graph-specific — OAuth, credential storage, the
real HTTP adapter and its deterministic ``Fake*`` substitute, the
delta-sweep adapter, and trusted evidence ingestion — lives under this
package. ``services/mailbox/mailbox.py`` and the rest of
``services/mailbox/`` (message projection, sweep-run ledger, cursor,
sweep lease, provider-neutral sweep orchestration) stay provider-
neutral, so a future IMAP/Gmail adapter could be added as a sibling
package without touching any of that.
"""
