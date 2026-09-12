"""BAGMAN durable persistence implementations (CD-3, PID §13/§53).

Deliberately a top-level directory, sibling to ``core/`` and
``services/`` — not nested inside either. PID §53 requires canonical
contracts/domain models to never depend on a storage technology (
PostgreSQL, MinIO/S3, or anything else), while persistence
implementations may depend on ``core``/``services``. Putting
provider-specific durable-storage code in its own top-level directory
makes that boundary a literal, easily-tested directory boundary:
``core/`` and ``services/`` must never import ``persistence/``; the
reverse (``persistence/`` importing ``core``/``services``) is expected
and fine.

Provider-specific implementations live under this package's
subpackages: ``persistence.objects`` for durable evidence object
storage (MinIO/S3, CD-3 WI-2), ``persistence.postgres`` for durable
canonical structured state (CD-3 WI-1).

This module intentionally contains nothing beyond this docstring; it
exists only to make ``persistence`` importable as a package.
"""
from __future__ import annotations
