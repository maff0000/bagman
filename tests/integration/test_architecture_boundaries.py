"""Architectural-boundary tests (PID §32).

Static/import-based: these tests parse Python source with the `ast`
module (never a regex) and inspect `contracts/*.schema.json` structure
directly. Nothing here imports or instantiates any domain object — it
proves properties of the *source tree itself* (import graphs, schema
shapes, dependency manifests), which is what makes it an architectural
test rather than a behavioural/integration one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONTRACTS_ROOT = REPO_ROOT / "contracts"

_STDLIB_MODULE_NAMES = set(sys.stdlib_module_names)


class Import(NamedTuple):
    root: str  # top-level module name, e.g. "services" for "services.evidence.evidence"
    full: str  # the full dotted module referenced, e.g. "services.evidence.evidence"
    lineno: int


def _py_files(*relative_dirs: str) -> list[Path]:
    files: list[Path] = []
    for rel_dir in relative_dirs:
        directory = REPO_ROOT / rel_dir
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def _imports_of(path: Path) -> list[Import]:
    """Parse every `import x` / `from x import y` statement in `path`
    (via `ast`, not a regex) and return one `Import` per imported
    module, resolved to its top-level root module name.

    Relative imports (`from . import foo`, `from .. import bar`, i.e.
    `node.level > 0`) are skipped: a relative import can never cross a
    top-level package boundary such as `core` -> `adapters`, so it is
    never itself a boundary violation.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    Import(root=alias.name.split(".")[0], full=alias.name, lineno=node.lineno)
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                imports.append(
                    Import(
                        root=node.module.split(".")[0],
                        full=node.module,
                        lineno=node.lineno,
                    )
                )
    return imports


# ---------------------------------------------------------------------
# core/ + services/evidence/ must never import provider-adapter/UI/agent code
# ---------------------------------------------------------------------

_FORBIDDEN_ROOTS = {"adapters", "agent", "ui"}


def test_core_and_services_evidence_never_import_adapters_agent_or_ui():
    violations = []
    for path in _py_files("core", "services/evidence"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root in _FORBIDDEN_ROOTS:
                violations.append(f"{rel}:{imp.lineno} imports {imp.full!r} (root {imp.root!r})")

    assert not violations, (
        "core/ and services/evidence/ must never import adapters/agent/ui "
        "(PID §32) — violations:\n" + "\n".join(violations)
    )


def test_agent_directory_currently_has_no_python_files():
    agent_dir = REPO_ROOT / "agent"
    agent_py_files = sorted(p.relative_to(REPO_ROOT).as_posix() for p in agent_dir.rglob("*.py"))
    assert agent_py_files == [], (
        f"expected agent/ to contain no .py files as of CD-2, found: {agent_py_files} "
        "(if agent/ now legitimately has code, the import-boundary test below is what "
        "must catch a future core/services -> agent import, not this one)"
    )


def test_nothing_under_core_or_services_imports_from_agent():
    """Meaningful even while agent/ is empty (see the test above): this
    inspects the IMPORT STATEMENTS in core/ and services/, not agent/'s
    current file listing, so it starts failing the moment anything
    under core/ or services/ imports `agent.*` in the future — it does
    not pass merely because agent/ has nothing to import today.
    """
    violations = []
    for path in _py_files("core", "services"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root == "agent":
                violations.append(f"{rel}:{imp.lineno} imports {imp.full!r}")

    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------
# core/ -> services/ has exactly one documented exception: core/api.py
# ---------------------------------------------------------------------


def test_core_to_services_boundary_has_exactly_one_documented_exception():
    """`core/api.py`'s own module docstring documents this: `core/`
    otherwise never imports `services/`, but `core/api.py` is the one
    deliberate composition-root exception (PID §33's single-facade
    requirement, composing `services.evidence.evidence.EvidenceRepository`
    alongside the pure `core/` operations).

    Assert that exception is EXACTLY `core/api.py`, importing EXACTLY
    from `services.evidence...` — so this test fails loudly the moment
    ANY other file under `core/` imports anything from `services`,
    i.e. a new, undocumented cross-boundary import.
    """
    crossings: list[tuple[str, str]] = []
    for path in _py_files("core"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root == "services":
                crossings.append((rel, imp.full))

    assert crossings, (
        "expected exactly one core/ -> services/ import crossing "
        "(core/api.py's documented exception); found none — either the "
        "exception was removed (update this test) or detection is broken"
    )
    assert len(crossings) == 1, (
        f"expected exactly ONE core/ -> services/ import crossing, found "
        f"{len(crossings)}: {crossings}"
    )

    rel_path, full_module = crossings[0]
    assert rel_path == "core/api.py", (
        f"the one core/ -> services/ crossing must be core/api.py; found it in "
        f"{rel_path!r} instead — this is a NEW, undocumented cross-boundary import"
    )
    assert full_module.startswith("services.evidence"), (
        f"expected core/api.py's one documented crossing to import from "
        f"'services.evidence...', got {full_module!r}"
    )


# ---------------------------------------------------------------------
# no Redis dependency, anywhere
# ---------------------------------------------------------------------


def test_requirements_files_do_not_mention_redis():
    for req_file in ("requirements.txt", "requirements-dev.txt"):
        text = (REPO_ROOT / req_file).read_text(encoding="utf-8")
        assert "redis" not in text.lower(), f"{req_file} must not mention redis (PID §32/§38)"


def test_no_redis_import_under_core_services_contracts_or_scripts():
    violations = []
    for path in _py_files("core", "services", "contracts", "scripts"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root == "redis":
                violations.append(f"{rel}:{imp.lineno} imports {imp.full!r}")

    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------
# no SDK-shaped third-party dependency introduced in core/ yet
# ---------------------------------------------------------------------

_ALLOWED_THIRD_PARTY_IN_CORE = {"jsonschema", "referencing", "rfc3339_validator"}


def test_only_allowed_third_party_imports_exist_under_core():
    """The only third-party (non-stdlib) imports anywhere under `core/`
    are `jsonschema`, `referencing` (jsonschema's own dependency), and
    the format-checker registration surface from `rfc3339_validator`
    (import name for the `rfc3339-validator` package). No SDK-shaped
    import (e.g. anything resembling `msal`, `google.*`, `xero*`,
    `chargebee*`, `O365`, `imapclient`, ...) may exist under `core/`
    yet — this should trivially pass today; its value is catching a
    regression later (PID §32/§38)."""
    violations = []
    for path in _py_files("core"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root in ("core", "services"):
                continue  # internal; covered by the boundary tests above
            if imp.root in _STDLIB_MODULE_NAMES:
                continue
            if imp.root not in _ALLOWED_THIRD_PARTY_IN_CORE:
                violations.append(f"{rel}:{imp.lineno} imports {imp.full!r} (root {imp.root!r})")

    assert not violations, (
        "unexpected third-party/SDK-shaped import under core/ — only "
        f"{sorted(_ALLOWED_THIRD_PARTY_IN_CORE)} are permitted:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------
# WI-1's open-vs-closed contract field design is locked in
# ---------------------------------------------------------------------

# Fields WI-1 deliberately left OPEN (a pattern string, NOT a closed
# JSON Schema enum/const), so new values are addable without a schema
# redesign (PID §4/§9/§24/§25/§13).
_OPEN_TAXONOMY_FIELDS = [
    ("entity/bagman.entity.v1.schema.json", "entity_type"),
    ("source/bagman.source.v1.schema.json", "source_type"),
    ("source/bagman.source.v1.schema.json", "provider"),
    ("evidence/bagman.evidence.v1.schema.json", "evidence_type"),
    ("evidence/bagman.evidence.v1.schema.json", "status"),
    ("audit/bagman.audit_event.v1.schema.json", "event_type"),
]

# Fields WI-1 deliberately CLOSED (a fixed JSON Schema enum), because
# the PID gives an exact, non-extensible vocabulary for them (PID
# §11/§14).
_CLOSED_VOCABULARY_FIELDS = [
    ("provenance/bagman.provenance.v1.schema.json", "relationship"),
    ("audit/bagman.audit_event.v1.schema.json", "actor_type"),
]


def _load_schema(relative_path: str) -> dict:
    return json.loads((CONTRACTS_ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative_path, field", _OPEN_TAXONOMY_FIELDS)
def test_open_taxonomy_fields_are_not_closed_enums_or_consts(relative_path, field):
    schema = _load_schema(relative_path)
    field_schema = schema["properties"][field]
    assert "enum" not in field_schema, (
        f"{relative_path}::{field} was deliberately left open (PID) but now "
        f"has a closed 'enum' — this is a structural design regression"
    )
    assert "const" not in field_schema, (
        f"{relative_path}::{field} was deliberately left open (PID) but now "
        f"has a 'const' — this is a structural design regression"
    )


@pytest.mark.parametrize("relative_path, field", _CLOSED_VOCABULARY_FIELDS)
def test_closed_vocabulary_fields_are_json_schema_enums(relative_path, field):
    schema = _load_schema(relative_path)
    field_schema = schema["properties"][field]
    assert "enum" in field_schema, (
        f"{relative_path}::{field} is documented as WI-1's deliberately CLOSED "
        f"vocabulary but is missing a JSON Schema 'enum' — this is a "
        f"structural design regression, not documentation drift"
    )
    assert isinstance(field_schema["enum"], list) and field_schema["enum"], (
        f"{relative_path}::{field}'s 'enum' must be a non-empty list"
    )


def test_no_schema_file_hardcodes_a_provider_name_inside_a_structural_constraint():
    """No schema file under `contracts/` may bake a specific external
    provider name (e.g. `MICROSOFT_GRAPH`, `XERO`, `STARLING`,
    `CHARGEBEE`) into a STRUCTURAL constraint (`required`/`enum`/
    `const`) — provider names may appear only in free-text
    `description`/`examples`, which do not constrain what validates.
    This is what actually keeps `provider`, `source_type`, etc. open in
    practice, not just in prose."""
    provider_like_tokens = {
        "MICROSOFT_GRAPH",
        "GMAIL",
        "IMAP",
        "STARLING",
        "REVOLUT",
        "XERO",
        "CHARGEBEE",
        "USECURE",
        "HUNTRESS",
    }
    violations = []
    for path in sorted(CONTRACTS_ROOT.rglob("*.schema.json")):
        data = json.loads(path.read_text(encoding="utf-8"))

        def _walk(node, key_path):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("enum", "const", "required") and key != "description":
                        flat = json.dumps(value)
                        for token in provider_like_tokens:
                            if token in flat:
                                violations.append(
                                    f"{path.relative_to(CONTRACTS_ROOT)}::{'/'.join(key_path + [key])} "
                                    f"contains provider token {token!r}"
                                )
                    _walk(value, key_path + [key])
            elif isinstance(node, list):
                for item in node:
                    _walk(item, key_path)

        _walk(data, [])

    assert not violations, "\n".join(violations)
