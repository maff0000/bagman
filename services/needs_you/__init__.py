"""BAGMAN universal Needs You queue component (CD-6 Slice 1, PID §98.2/§98.5).

``BAGMAN.NEEDS_YOU`` — the one cross-domain operator queue every BAGMAN
producer (today: governed evidence intake; later slices: email triage,
invoice review, rule proposals) raises a ``NeedsYouItem`` into when it
needs a human decision before BAGMAN can keep going. Owns
``NeedsYouItem``, its small closed state machine
(``OPEN -> RESOLVED``/``DISMISSED``), an in-memory reference
repository, and (see ``persistence/postgres/needs_you_repository.py``)
a durable PostgreSQL implementation.

Depends only on ``core`` primitives (identity, timestamps, errors,
contract validation) — same layering discipline as
``services/evidence/intake/`` — never on ``app``/``persistence``/
``adapters``/``agent``/``ui``.
"""
