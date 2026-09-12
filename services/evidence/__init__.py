"""BAGMAN evidence service package (PID §6-8, §21-22, §25).

Owns the ``EvidenceItem`` domain model and its repository. Depends on
``core`` primitives (identity, timestamps, errors, contract
validation) but never the reverse — ``core`` must not import from
``services.evidence`` (PID §32/§37).
"""
