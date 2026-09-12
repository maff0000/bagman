"""BAGMAN durable persistence implementations (CD-3 WI-1).

Deliberately a top-level directory, sibling to ``core/`` and
``services/`` — not nested inside either. PID §53 requires canonical
contracts/domain models to never depend on PostgreSQL (or any other
storage technology), while persistence implementations may depend on
``core``/``services``. Putting Postgres-backed code in its own
top-level directory makes that boundary a literal, easily-tested
directory boundary: ``core/`` and ``services/`` must never import
``persistence/``; the reverse (``persistence/`` importing ``core`` and
``services``) is expected and fine.
"""
from __future__ import annotations
