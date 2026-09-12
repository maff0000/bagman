"""BAGMAN persistence layer (CD-3, PID §13/§53).

Provider-specific durable-storage implementations live under this
package's subpackages (e.g. ``persistence.objects`` for evidence
object storage; a parallel CD-3 work item introduces
``persistence.postgres`` for structured canonical state). Canonical
``core``/``services`` domain models never import a provider SDK
directly — persistence implementations depend on the domain layer's
abstract repository/store interfaces, not the other way round (PID
§53).

This module intentionally contains nothing beyond this docstring; it
exists only to make ``persistence`` importable as a package.
"""
from __future__ import annotations
