"""BAGMAN evidence-intake component (PID §4-8, CD-4 WI-1).

``BAGMAN.EVIDENCE.INTAKE`` — the single governed boundary through which
untrusted, external/user-supplied bytes may become canonical BAGMAN
evidence (PID §3). Owns ``IntakeRecord`` and its state machine,
in-memory reference repository, and (see
``persistence/postgres/intake_repository.py``) durable PostgreSQL
implementation.

Depends on ``core`` primitives (identity, timestamps, errors, contract
validation) exactly like ``services/evidence/evidence.py`` does, but
never the reverse, and never on any provider-adapter/UI/agent code
(PID §32, enforced by ``tests/integration/test_architecture_boundaries.py``,
which already scans all of ``services/evidence`` recursively).
"""
