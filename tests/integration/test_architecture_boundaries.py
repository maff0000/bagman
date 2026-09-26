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


def test_agent_policies_and_memory_still_have_no_python_files():
    """CD-5 WI-3 populates `agent/bagman/` and `agent/tools/` for real
    (Ask BAGMAN orchestration + the fixed tool registry) — superseding
    this test's original CD-2 "agent/ has no .py files at all" form.
    `agent/policies/` and `agent/memory/` remain CD-1's original empty
    placeholders as of WI-3 (see WI-3's own delivery report: no
    genuinely CD-5-scoped content was found for either) — this narrower
    assertion is what's still meaningful. The import-boundary test below
    (`test_nothing_under_core_or_services_imports_from_agent`) is the
    one that actually matters regardless of agent/'s file listing.
    """
    for sub in ("policies", "memory"):
        subdir = REPO_ROOT / "agent" / sub
        py_files = sorted(p.relative_to(REPO_ROOT).as_posix() for p in subdir.rglob("*.py"))
        assert py_files == [], f"expected agent/{sub}/ to contain no .py files, found: {py_files}"


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

    assert not violations, (
        "no contracts/*.schema.json file may bake a provider name into a "
        "structural constraint (enum/const/required) — provider names belong "
        "only in free-text description/examples:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------
# CD-4 WI-5 (PID §67) — the required Evidence Intake architecture proof.
# Extends this existing module rather than duplicating a parallel
# import-boundary test file, per WI-5's own instruction.
# ---------------------------------------------------------------------


def test_static_ui_assets_contain_no_python_or_server_side_code():
    """PID §67: 'GUI does not write DB/object storage directly'. The
    strongest, most literal proof available by inspection: the GUI is
    served entirely from ``app/api/static/`` (see ``app/api/main.py``'s
    own docstring — a plain ``StaticFiles`` mount) and that directory
    must contain ONLY client-side assets (HTML/CSS/JS) — no Python, no
    server-side code of any kind that could reach a database or object
    store directly instead of going through the existing ``/internal/*``
    HTTP API."""
    static_dir = REPO_ROOT / "app" / "api" / "static"
    assert static_dir.is_dir(), f"expected {static_dir} to exist"

    python_files = sorted(p.relative_to(REPO_ROOT).as_posix() for p in static_dir.rglob("*.py"))
    assert python_files == [], (
        f"app/api/static/ must contain no Python/server-side code — found: {python_files}"
    )

    allowed_suffixes = {".js", ".html", ".css"}
    unexpected = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in static_dir.rglob("*")
        if p.is_file() and p.suffix.lower() not in allowed_suffixes
    )
    assert unexpected == [], (
        f"app/api/static/ contains file(s) outside the expected client-asset "
        f"types {sorted(allowed_suffixes)}: {unexpected}"
    )


def test_static_ui_javascript_only_calls_the_existing_internal_http_api():
    """Confirms, by inspection of the actual JS source (not merely by
    absence of a database driver import — there is no such thing as an
    'import' in plain browser JS to look for), that BAGMAN's own GUI
    talks to the backend ONLY through relative ``/internal/*`` (or
    ``/health``/``/ready``/``/version``) HTTP paths via ``fetch`` —
    never a raw database connection string, an AWS/S3 SDK-shaped
    endpoint, or any other infrastructure address.

    CD-5 WI-4 modularised the former single ``app.js`` monolith into
    ``shell/``/``shared/``/``features/{overview,documents,ai}/`` (PID
    §41) — ``app.js`` itself is now a thin entry point with no literal
    endpoint strings of its own, so this test scans every ``*.js`` file
    under ``app/api/static/`` (the same tree Starlette's ``StaticFiles``
    serves), not just the one former monolith file.
    """
    static_dir = REPO_ROOT / "app" / "api" / "static"
    js_files = sorted(static_dir.rglob("*.js"))
    assert js_files, "expected at least one JS file under app/api/static/"

    # Every literal path string these files reference as an API target
    # must be relative and rooted at one of the known, existing JSON
    # surfaces — never an absolute external host, never anything
    # database/object-store-shaped.
    forbidden_tokens = [
        "postgres://", "postgresql://", "mysql://", "mongodb://",
        "s3://", "minio", ":5432", ":9000", ":3310",
        "boto3", "psycopg", "sqlalchemy",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in js_files)
    lowered = combined.lower()
    violations = [token for token in forbidden_tokens if token in lowered]
    assert violations == [], (
        f"app/api/static/**/*.js appears to reference infrastructure directly: {violations}"
    )
    assert "/internal/" in combined, "expected the GUI's JS to call the existing /internal/* HTTP API"


# core/ and services/evidence/ must never import app/ (the HTTP/routing
# layer) or persistence/ (infrastructure) — extends the existing
# adapters/agent/ui check above with PID §67's "canonical core does not
# import scanner/UI/infrastructure code".
_FORBIDDEN_INFRASTRUCTURE_ROOTS = {"app", "persistence"}


def test_core_and_services_evidence_never_import_app_or_persistence():
    violations = []
    for path in _py_files("core", "services/evidence"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root in _FORBIDDEN_INFRASTRUCTURE_ROOTS:
                violations.append(f"{rel}:{imp.lineno} imports {imp.full!r} (root {imp.root!r})")

    assert not violations, (
        "core/ and services/evidence/ must never import app/ (HTTP/routing) or "
        "persistence/ (infrastructure) (PID §67) — persistence/*.py legitimately "
        "imports FROM services/core, never the reverse; see "
        "persistence/postgres/intake_repository.py's and "
        "services/evidence/intake/validation_pipeline.py's own module "
        "docstrings for the documented layering direction. Violations:\n"
        + "\n".join(violations)
    )


def test_validation_pipeline_never_imports_concrete_scanner_implementation():
    """PID §67: 'scanner implementation remains behind abstraction'.
    ``services.evidence.intake.validation_pipeline`` must depend only on
    the ``EvidenceSafetyScanner`` abstraction (plus the closed
    ``ScanVerdict``/``ScanResult`` value types) — never import
    ``ClamAVScanner`` (or any other concrete implementation) directly.
    The one legitimate place that imports the concrete implementation is
    ``app/api/composition.py`` (the composition root), which this test
    does not touch."""
    path = REPO_ROOT / "services" / "evidence" / "intake" / "validation_pipeline.py"
    tree_source = path.read_text(encoding="utf-8")

    import ast

    tree = ast.parse(tree_source, filename=str(path))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])

    assert "ClamAVScanner" not in imported_names, (
        "services/evidence/intake/validation_pipeline.py must depend only on the "
        "EvidenceSafetyScanner abstraction, never import ClamAVScanner directly "
        f"(PID §19/§67) — found imported names: {sorted(imported_names)}"
    )
    assert "EvidenceSafetyScanner" in imported_names, (
        "expected validation_pipeline.py to import the EvidenceSafetyScanner "
        "abstraction it is supposed to depend on"
    )


def test_no_live_mailbox_adapter_code_exists_yet():
    """PID §67: 'adapters do not exist yet for live email'. `adapters/`
    is CD-4's own explicitly-reserved placeholder directory for a
    FUTURE delivery (CD-5) — as of CD-4 it must contain no Python code
    at all, only the placeholder README files each subdirectory already
    has."""
    adapters_dir = REPO_ROOT / "adapters"
    python_files = sorted(p.relative_to(REPO_ROOT).as_posix() for p in adapters_dir.rglob("*.py"))
    assert python_files == [], (
        f"adapters/ must contain no Python code yet (CD-5's job, not CD-4's) — found: {python_files}"
    )


def test_internal_router_direct_upload_bypass_is_genuinely_gone():
    """PID §67: 'direct upload route cannot bypass intake'. Confirms, by
    AST inspection (not merely by grepping the module's own prose
    docstring, which legitimately still narrates the historical removal
    in plain text), that no function in
    ``app/api/routers/internal.py`` declares an ``UploadFile`` parameter
    or calls ``File(...)`` anywhere in its body — the CD-3
    ``POST /internal/evidence`` byte-accepting route is genuinely gone,
    not merely renamed/hidden."""
    import ast

    path = REPO_ROOT / "app" / "api" / "routers" / "internal.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_args = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
            for arg in all_args:
                if arg.annotation is not None and ast.dump(arg.annotation).find("UploadFile") != -1:
                    violations.append(f"{node.name}({arg.arg}: ...) at line {node.lineno}")
        if isinstance(node, ast.Call):
            callee = node.func
            callee_name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if callee_name == "File":
                violations.append(f"File(...) call at line {node.lineno}")

    assert violations == [], (
        "app/api/routers/internal.py must never accept a raw file upload "
        f"(the CD-3 bypass must stay closed, PID §26/§27/§67) — found: {violations}"
    )


# ---------------------------------------------------------------------
# CD-4 WI-5 (PID §67/§69) — no forbidden live-provider SDK/mailbox-
# polling library anywhere in the repository yet (grep across every
# .py file, not just core/services — a stray import anywhere would
# still represent exactly the "no mailbox integration" invariant being
# broken).
#
# CD-6 GUI-operations-foundation follow-on WO update (second mailbox
# provider — plain IMAP for `matt@noust.ai`): this WO's own explicit
# mandate is to build the real, network-speaking IMAP adapter using
# stdlib `imaplib` (no third-party IMAP library is available/needed —
# see `services/mailbox/imap/imap_client.py`'s own module docstring).
# `imaplib` is therefore REMOVED from the fully-forbidden root set and
# instead governed by :data:`_IMAPLIB_SANCTIONED_PATH_PREFIX` below —
# the ONE sanctioned location it may be imported from; a stray
# `imaplib` import anywhere ELSE in the repository remains a violation.
# Every OTHER provider SDK (`msal`/`googleapiclient`/
# `google_auth_oauthlib`/`imapclient`/`exchangelib`/`O365`) remains
# fully forbidden repo-wide — Microsoft Graph's own real adapter
# (`services/mailbox/microsoft/graph_client.py`) deliberately uses
# stdlib `urllib` only, precisely so this invariant never needed
# loosening for that provider; this is documented, minimal, additive
# scope-narrowing for IMAP alone, never a general relaxation.
# ---------------------------------------------------------------------

_FORBIDDEN_MAILBOX_AND_PROVIDER_IMPORT_ROOTS = {
    "msal",
    "googleapiclient",
    "google_auth_oauthlib",
    "imapclient",
    "exchangelib",
    "O365",
}

#: The ONE sanctioned location `imaplib` may be imported from — see the
#: comment block above. A POSIX-style relative path prefix (matched
#: against the file's own repo-relative path).
_IMAPLIB_SANCTIONED_PATH_PREFIX = "services/mailbox/imap/"


def test_no_forbidden_mailbox_or_provider_sdk_imported_anywhere_in_the_repo():
    """PID §67/§69, narrowed by the CD-6 GUI-operations-foundation
    follow-on WO (see comment block above): no Microsoft Graph/MSAL,
    Gmail/Google API client library, or ``imapclient``/``exchangelib``/
    ``O365`` mailbox-polling code exists anywhere in the repository —
    and plain stdlib ``imaplib`` exists ONLY under
    ``services/mailbox/imap/``, this delivery's own sanctioned real IMAP
    adapter. Scans every ``.py`` file under the repo (excluding
    third-party-managed directories), via real ``ast`` import parsing,
    not a text grep."""
    violations = []
    search_roots = ["core", "services", "persistence", "app", "adapters", "agent", "ui", "scripts", "tests", "ops"]
    for path in _py_files(*search_roots):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root == "imaplib":
                if not rel.startswith(_IMAPLIB_SANCTIONED_PATH_PREFIX):
                    violations.append(f"{rel}:{imp.lineno} imports {imp.full!r} (outside the sanctioned IMAP adapter package)")
                continue
            if imp.root in _FORBIDDEN_MAILBOX_AND_PROVIDER_IMPORT_ROOTS:
                violations.append(f"{rel}:{imp.lineno} imports {imp.full!r}")

    assert not violations, (
        "no Microsoft Graph/MSAL, Gmail/Google API client library, or "
        "imapclient/exchangelib/O365 mailbox-polling import may exist anywhere, and "
        "imaplib may only be imported from services/mailbox/imap/ (PID §67/§69, narrowed "
        "by the CD-6 GUI-operations-foundation follow-on WO). Violations:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------
# CD-5 WI-5 (PID §9/§75/§87/§92) — the full REPO-WIDE trinity-* alias
# sweep. `tests/security/test_ai_litellm_alias_lockdown.py` already
# proves this for WI-2's OWN new files
# (`test_no_trinity_star_alias_literal_in_this_wis_new_files`); this is
# the wider check PID §92's Auditor instruction and PID §9's own
# "BAGMAN's own architecture-boundary tests must assert that no BAGMAN
# source file references a trinity-* alias at all" require: every
# application source file in the repository, not just ai/providers/
# ai/gateway/ai/prompts/app/api/routers/ai.py.
#
# CD-6 §103 Inference Architecture Ruling update: `trinity-core` is now
# the SOLE authorised Trinity alias (`ai.invocation.BACKGROUND_CAPABILITY_ALIASES`),
# legitimately referenced throughout application source
# (`ai/invocation.py`, `ai/jobs.py`, `contracts/ai/bagman.ai_invocation.v1.schema.json`,
# `scripts/process_background_job_overflow.py`, ...) — it is REMOVED
# from this forbidden list below (was present when this sweep was first
# written, back when PID §9 forbade every `trinity-*` value with no
# exception). Every OTHER `trinity-*` alias remains forbidden
# everywhere in application source, exactly as before.
#
# Deliberately EXCLUDES `tests/` itself: several existing test files
# legitimately use `trinity-fast`/`trinity-deep`/`trinity-embed` as
# literal ADVERSARIAL/NEGATIVE fixture values (e.g.
# `tests/integration/test_litellm_client.py`'s own
# "reject every forbidden alias" parametrisation, `tests/contract/
# test_ai_invocation_contract.py`'s "this value must fail contract
# validation" fixture) — a test proving BAGMAN rejects a trinity-*
# alias necessarily contains that string once, and that is correct,
# not a violation. What matters is that no APPLICATION source file
# (everything BAGMAN actually ships/runs) contains one of the STILL
# forbidden aliases.
# ---------------------------------------------------------------------

_TRINITY_STAR_ALIASES_REPO_WIDE = ["trinity-fast", "trinity-deep", "trinity-embed"]
_APPLICATION_SOURCE_ROOTS = (
    "ai",
    "agent",
    "app",
    "core",
    "persistence",
    "services",
    "adapters",
    "ui",
    "scripts",
    "ops",
    "config",
    "deployment",
    "contracts",
)


def test_no_trinity_star_alias_literal_anywhere_in_application_source():
    """PID §9/§75/§87/§92 (CD-6 §103 update): no `trinity-fast`/
    `trinity-deep`/`trinity-embed` literal may exist anywhere in
    BAGMAN's own shipped application source — only
    `bagman-fast`/`bagman-core`/`trinity-core`
    (`ai.invocation.BACKGROUND_CAPABILITY_ALIASES`) are ever permitted;
    `trinity-core` is the sole authorised Trinity alias (CD-6 §103) and
    is deliberately NOT in this forbidden list any more. Scans every
    file (not just `.py`) under the application-source roots, so a
    stray reference in a `.yml`/`.md`/`.json`/`.sh` file would be
    caught too, not just a Python import."""
    violations: list[str] = []
    for root_name in _APPLICATION_SOURCE_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue  # binary/unreadable file — not a source-literal concern
            lowered = text.lower()
            for bad_alias in _TRINITY_STAR_ALIASES_REPO_WIDE:
                if bad_alias in lowered:
                    violations.append(f"{path.relative_to(REPO_ROOT)} contains {bad_alias!r}")

    assert violations == [], (
        "no application source file may reference a trinity-* alias — only bagman-* aliases "
        f"are permitted anywhere BAGMAN actually ships/runs (PID §9/§75/§87/§92):\n"
        + "\n".join(violations)
    )


def test_requirements_files_do_not_mention_forbidden_mailbox_or_provider_sdks():
    forbidden_package_tokens = [
        "msal", "google-api-python-client", "google-auth", "imapclient",
        "exchangelib", "o365",
    ]
    for req_file in ("requirements.txt", "requirements-dev.txt"):
        text = (REPO_ROOT / req_file).read_text(encoding="utf-8").lower()
        violations = [token for token in forbidden_package_tokens if token in text]
        assert violations == [], f"{req_file} must not depend on: {violations}"

    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------
# CD-6 Slice 2 (PID §98.4, architect spec §1) — "No hardcoded company
# strings anywhere in app/api/static/... the existing seed entities
# remain the canonical source; do not duplicate or shadow them."
# ---------------------------------------------------------------------

#: The exact literal tokens PID §98.3/§98.4 name as this delivery's
#: real, canonical BAGMAN companies (`app/api/composition.py
#: ::SEED_ENTITIES` — the single source of truth these tokens are
#: copied from, not re-typed independently). Every one of these is
#: "business truth" the GUI must resolve through a live `GET
#: /internal/entities` call, never bake in as a literal — both the
#: `canonical_name` form (`INFOSECURS_LIMITED`) an `<option value=...>`
#: could hardcode, and the `display_name` form (`Infosecurs Limited`) a
#: label could hardcode.
_HARDCODED_COMPANY_TOKENS: tuple[str, ...] = (
    "INFOSECURS_LIMITED",
    "NOUSTAI_LIMITED",
    "MATTHEW_SCOTT_PERSONAL",
    "Infosecurs Limited",
    "NoustAI Limited",
    "Matthew Scott Personal",
)


def test_no_hardcoded_company_truth_anywhere_in_static_ui():
    """A real, repo-wide grep proof — mirrors
    ``test_no_trinity_star_alias_literal_anywhere_in_application_source``'s
    own "scan every file, not just .py" technique, scoped to
    ``app/api/static/`` and CD-6's own closed set of real company
    tokens (see :data:`_HARDCODED_COMPANY_TOKENS`).

    This is not merely aspirational: CD-6 Slice 2's own delivery
    removed a real violation this test caught during development — CD-4
    WI-4's Documents "Upload evidence" panel (``index.html``) hardcoded
    all three companies directly as ``<option>`` literals. It now
    resolves them at runtime via ``shell/entities.js``'s existing
    ``listEntities()`` cache (see
    ``features/documents/documents.js::_wireUpload``), exactly like
    every other company-aware surface in this GUI already does.
    """
    static_dir = REPO_ROOT / "app" / "api" / "static"
    violations: list[str] = []
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in _HARDCODED_COMPANY_TOKENS:
            if token in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains hardcoded company token {token!r}")

    assert violations == [], (
        "app/api/static/ must never hardcode a real company name/canonical_name — every "
        "company-aware surface must resolve companies through GET /internal/entities "
        "(shell/entities.js::listEntities()), the one canonical source (PID §98.3/§98.4):\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------
# CD-6 Slice 5 WI-1 — EvidenceClassification/EvidenceClassificationRule
# architecture-boundary proofs (WO §39-44's own explicit list).
# ---------------------------------------------------------------------

_CLASSIFICATION_MODULE_PATHS = [
    REPO_ROOT / "services" / "evidence" / "classification.py",
    REPO_ROOT / "services" / "evidence" / "classification_rule.py",
    REPO_ROOT / "persistence" / "postgres" / "evidence_classification_models.py",
    REPO_ROOT / "persistence" / "postgres" / "evidence_classification_repository.py",
    REPO_ROOT / "persistence" / "postgres" / "evidence_classification_rule_models.py",
    REPO_ROOT / "persistence" / "postgres" / "evidence_classification_rule_repository.py",
    # CD-6 Slice 5 WI-2 additions — the deterministic matcher/guard/
    # preview/governed-service/router layer built on top of WI-1's own
    # domain model. Added to this SAME shared list (rather than a
    # parallel one) so every existing WI-1 check below (no EvidenceItem
    # mutation, no ENTITY_PROPOSAL/Needs-You wiring, no PDF/OCR import)
    # automatically covers WI-2's new modules too.
    REPO_ROOT / "services" / "evidence" / "classification_matcher.py",
    REPO_ROOT / "services" / "evidence" / "classification_observation.py",
    REPO_ROOT / "services" / "evidence" / "classification_rule_service.py",
    REPO_ROOT / "services" / "evidence" / "classification_service.py",
    REPO_ROOT / "app" / "api" / "routers" / "evidence_classification.py",
]


def test_evidence_classification_code_never_mutates_evidence_item():
    """PID/WO invariant: classification code may reference `EvidenceItem`
    (via `evidence_id`/`EvidenceItemRow`) but must never mutate it — the
    only two EvidenceItem mutation paths anywhere in this codebase are
    `EvidenceRepository.update_status`/`assign_entity`; neither may ever
    be called from classification code, and no classification module
    may write to an `EvidenceItemRow`'s own columns (the Postgres
    repository's `SELECT ... FOR UPDATE` lock on `evidence_items` is
    read-only — it exists purely to serialise the expected-current
    check, never to change the row it locks)."""
    forbidden_substrings = [
        ".update_status(", ".assign_entity(",
        "evidence_row.status", "evidence_row.entity_id", "evidence_row.evidence_type",
        "evidence_row.mime_type", "evidence_row.size_bytes", "evidence_row.content_hash",
    ]
    violations = []
    for path in _CLASSIFICATION_MODULE_PATHS:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_substrings:
            if pattern in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {pattern!r}")
    assert violations == [], (
        "CD-6 Slice 5 WI-1 classification code must never mutate EvidenceItem — violations:\n"
        + "\n".join(violations)
    )


def test_evidence_classification_migration_never_touches_evidence_items():
    """The WI-1 migration is purely additive — two brand new tables,
    zero modification to any existing table (in particular
    `evidence_items` itself)."""
    migration_path = REPO_ROOT / "alembic" / "versions" / "b4d8f1a92c65_evidence_classification.py"
    assert migration_path.is_file(), f"expected {migration_path} to exist"
    text = migration_path.read_text(encoding="utf-8")
    forbidden_substrings = [
        "add_column('evidence_items'", 'add_column("evidence_items"',
        "alter_column('evidence_items'", 'alter_column("evidence_items"',
        "drop_column('evidence_items'", 'drop_column("evidence_items"',
    ]
    violations = [p for p in forbidden_substrings if p in text]
    assert violations == [], (
        f"{migration_path.relative_to(REPO_ROOT)} must never modify evidence_items — found: {violations}"
    )


def test_classification_modules_never_import_ai_provider_or_gateway_code():
    """CD-6 Slice 5 WI-1 is persistence/domain only — no AI execution.
    Neither classification module may import anything under
    `ai.providers`/`ai.gateway`/`ai.prompts` (the orchestration layers a
    future WI-3 would wire up), only the neutral `ai.invocation` domain
    model (`AIInvocation`/`AIInvocationRepository`, for type-shape/DI
    purposes and existence-checking `.get_invocation`)."""
    forbidden_ai_submodules = {"ai.providers", "ai.gateway", "ai.prompts", "ai.tasks"}
    violations = []
    for path in [
        REPO_ROOT / "services" / "evidence" / "classification.py",
        REPO_ROOT / "services" / "evidence" / "classification_rule.py",
        REPO_ROOT / "persistence" / "postgres" / "evidence_classification_repository.py",
        REPO_ROOT / "persistence" / "postgres" / "evidence_classification_rule_repository.py",
    ]:
        for imp in _imports_of(path):
            if any(imp.full == mod or imp.full.startswith(mod + ".") for mod in forbidden_ai_submodules):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}")
    assert violations == [], (
        "CD-6 Slice 5 WI-1 classification code must never import AI orchestration code — violations:\n"
        + "\n".join(violations)
    )


def test_classification_rule_module_never_imports_mailbox_sweep_or_provider_adapters():
    """`services.evidence.classification_rule` must never import
    anything under `services.mailbox` at all (CD-6 Slice 5 WI-2's own
    shared-normalization refactor: the pure text-matching helpers this
    module needs now live in the neutral `core.text_matching` module,
    imported directly — see that module's own docstring, and
    `classification_rule.py`'s own "Normalization — reused, not
    re-implemented" docstring section). Before WI-2, this module's only
    `services.mailbox` import was `services.mailbox.domain_rule` (for
    those same three normalization helpers) — that transitive
    dependency is now gone entirely, not merely narrowed."""
    path = REPO_ROOT / "services" / "evidence" / "classification_rule.py"
    mailbox_imports = [imp.full for imp in _imports_of(path) if imp.full == "services.mailbox" or imp.full.startswith("services.mailbox.")]
    assert mailbox_imports == [], (
        "services/evidence/classification_rule.py must never import anything under services.mailbox — "
        f"found: {mailbox_imports}"
    )


def test_no_entity_proposal_or_needs_you_review_wiring_in_classification_modules():
    """CD-6 Slice 5 WI-1 prohibitions: no `ENTITY_PROPOSAL` wiring, and
    `ITEM_TYPE_CLASSIFICATION_REVIEW` (a Needs You producer) stays
    completely untouched — zero new code anywhere references either."""
    forbidden_tokens = ("ENTITY_PROPOSAL", "ITEM_TYPE_CLASSIFICATION_REVIEW", "run_background_task")
    violations = []
    for path in _CLASSIFICATION_MODULE_PATHS:
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {token!r}")
    assert violations == [], "\n".join(violations)


def test_no_pdf_or_ocr_library_import_in_classification_modules():
    """WI-1 is persistence/domain only — no document text-extraction
    pipeline exists yet; classification code must never import a PDF/OCR
    library."""
    forbidden_roots = {
        "fitz", "pypdf", "PyPDF2", "pdfplumber", "pytesseract", "pdf2image", "textract", "pikepdf", "pdfminer",
    }
    violations = []
    for path in _CLASSIFICATION_MODULE_PATHS:
        for imp in _imports_of(path):
            if imp.root in forbidden_roots:
                violations.append(f"{path.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}")
    assert violations == [], "\n".join(violations)


def test_no_production_content_fixture_added_for_evidence_classification():
    """No binary/real-document fixture file was added anywhere under
    `tests/` for this delivery — every classification/rule test uses
    plain, synthetic, test-local values only (fabricated hashes,
    in-memory dataclasses), nothing that could be mistaken for real
    evidence content."""
    forbidden_suffixes = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".tiff", ".eml", ".msg"}
    violations = []
    for pattern in ("*evidence_classification*", "*classification_rule*"):
        for path in REPO_ROOT.joinpath("tests").rglob(pattern):
            if path.is_file() and path.suffix.lower() in forbidden_suffixes:
                violations.append(str(path.relative_to(REPO_ROOT)))
    assert violations == [], (
        f"no binary/production-content fixture file may exist for this delivery's tests: {violations}"
    )


# ---------------------------------------------------------------------
# CD-6 Slice 5 WI-2 — matcher/guard/preview/governed-service/router
# architecture-boundary proofs.
# ---------------------------------------------------------------------

_WI2_NEW_MODULE_PATHS = [
    REPO_ROOT / "services" / "evidence" / "classification_matcher.py",
    REPO_ROOT / "services" / "evidence" / "classification_observation.py",
    REPO_ROOT / "services" / "evidence" / "classification_rule_service.py",
    REPO_ROOT / "services" / "evidence" / "classification_service.py",
    REPO_ROOT / "app" / "api" / "routers" / "evidence_classification.py",
    REPO_ROOT / "core" / "text_matching.py",
]


def test_wi2_classification_modules_never_import_provider_adapters_or_sweep():
    """The matcher/guard/preview/service layer must never import a
    Gmail/Microsoft-Graph/IMAP provider adapter or the sweep engine —
    these modules are pure domain/compute logic operating on data a
    caller already fetched, never a live mail-fetch path themselves."""
    forbidden_mailbox_submodules = (
        "services.mailbox.sweep",
        "services.mailbox.microsoft",
        "services.mailbox.gmail",
        "services.mailbox.imap",
    )
    violations = []
    for path in _WI2_NEW_MODULE_PATHS:
        for imp in _imports_of(path):
            if any(imp.full == mod or imp.full.startswith(mod + ".") for mod in forbidden_mailbox_submodules):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}")
    assert violations == [], (
        "CD-6 Slice 5 WI-2 modules must never import a Gmail/Microsoft-Graph/IMAP provider adapter or the "
        "sweep engine — violations:\n" + "\n".join(violations)
    )


def test_wi2_classification_modules_never_import_ai_orchestration_code():
    """No `ai.*` import anywhere in WI-2's new modules — this delivery
    is deterministic/non-AI only."""
    forbidden_ai_roots = {"ai"}
    violations = []
    for path in _WI2_NEW_MODULE_PATHS:
        for imp in _imports_of(path):
            if imp.root in forbidden_ai_roots:
                violations.append(f"{path.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}")
    assert violations == [], (
        "CD-6 Slice 5 WI-2 modules must never import ai.* — this delivery is deterministic/non-AI only — "
        "violations:\n" + "\n".join(violations)
    )


def test_wi2_classification_modules_never_call_mailbox_domain_rule_write_methods():
    """Classification rules must never touch mailbox policy —
    `MailboxDomainRuleRepository.upsert_rule` (its only mutating method)
    is never called from any WI-2 module."""
    violations = []
    for path in _WI2_NEW_MODULE_PATHS:
        text = path.read_text(encoding="utf-8")
        if ".upsert_rule(" in text:
            violations.append(str(path.relative_to(REPO_ROOT)))
    assert violations == [], (
        f"CD-6 Slice 5 WI-2 modules must never call MailboxDomainRuleRepository.upsert_rule: {violations}"
    )


def test_wi2_classification_modules_never_call_evidence_object_store():
    """The preview/guard/matcher/service layer reads only
    `EvidenceItem.metadata` (sender/subject) — never document content,
    raw MIME, or attachment bytes. No WI-2 module ever calls into an
    object store."""
    violations = []
    for path in _WI2_NEW_MODULE_PATHS:
        text = path.read_text(encoding="utf-8")
        if "object_store" in text and "object_store.get(" in text:
            violations.append(str(path.relative_to(REPO_ROOT)))
    assert violations == [], f"CD-6 Slice 5 WI-2 modules must never read from an object store: {violations}"


# ---------------------------------------------------------------------
# CD-6 Slice 5 WI-3 — context-builder/fingerprint/orchestrator
# architecture-boundary proofs. Deliberately a SEPARATE module list
# from `_CLASSIFICATION_MODULE_PATHS`/`_WI2_NEW_MODULE_PATHS` above —
# unlike WI-1/WI-2, WI-3's own orchestrator IS supposed to import
# `ai.gateway.background`/`ai.tasks`/`ai.prompts.loader` (that is its
# entire job), so it must never be added to either of those two
# AI-import-forbidding lists.
# ---------------------------------------------------------------------

_WI3_CONTEXT_AND_FINGERPRINT_MODULE_PATHS = [
    REPO_ROOT / "services" / "evidence" / "classification_context.py",
    REPO_ROOT / "services" / "evidence" / "classification_ai_fingerprint.py",
]

_WI3_ORCHESTRATOR_MODULE_PATH = REPO_ROOT / "services" / "evidence" / "classification_orchestrator.py"


def test_wi3_context_and_fingerprint_modules_never_import_ai_provider_or_gateway_code():
    """The context builder and fingerprint modules are pure,
    deterministic, non-AI helpers (WI-3 §13/§23) — neither may import
    anything under `ai.providers`/`ai.gateway`/`ai.tasks`. (The
    orchestrator, which genuinely needs those, is checked separately
    below — never against this same prohibition.)"""
    forbidden_ai_submodules = {"ai.providers", "ai.gateway", "ai.tasks"}
    violations = []
    for path in _WI3_CONTEXT_AND_FINGERPRINT_MODULE_PATHS:
        for imp in _imports_of(path):
            if any(imp.full == mod or imp.full.startswith(mod + ".") for mod in forbidden_ai_submodules):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}")
    assert violations == [], (
        "CD-6 Slice 5 WI-3 context/fingerprint modules must never import AI orchestration code — "
        "violations:\n" + "\n".join(violations)
    )


def test_wi3_context_module_never_imports_pdf_or_ocr_library():
    """WI-3 adds no PDF/OCR/Office parser (§14/§21) — only stdlib
    `email`/`html.parser` for `message/rfc822`/`text/plain`."""
    forbidden_roots = {
        "fitz", "pypdf", "PyPDF2", "pdfplumber", "pytesseract", "pdf2image", "textract", "pikepdf", "pdfminer",
        "docx", "openpyxl", "bs4", "lxml",
    }
    violations = []
    for imp in _imports_of(REPO_ROOT / "services" / "evidence" / "classification_context.py"):
        if imp.root in forbidden_roots:
            violations.append(f"classification_context.py:{imp.lineno} imports {imp.full!r}")
    assert violations == [], "\n".join(violations)


def test_wi3_orchestrator_never_mutates_evidence_item():
    """Same invariant WI-1/WI-2 already enforce for their own modules
    (`test_evidence_classification_code_never_mutates_evidence_item`),
    re-proven independently for WI-3's new orchestrator: it may
    reference `evidence_id`/read `EvidenceItem` fields, but must never
    call the two EvidenceItem mutation paths, and must never write to
    an `EvidenceItemRow`'s own columns."""
    forbidden_substrings = [
        ".update_status(", ".assign_entity(",
        "evidence_row.status", "evidence_row.entity_id", "evidence_row.evidence_type",
        "evidence_row.mime_type", "evidence_row.size_bytes", "evidence_row.content_hash",
    ]
    text = _WI3_ORCHESTRATOR_MODULE_PATH.read_text(encoding="utf-8")
    violations = [pattern for pattern in forbidden_substrings if pattern in text]
    assert violations == [], (
        f"CD-6 Slice 5 WI-3 classification_orchestrator.py must never mutate EvidenceItem — "
        f"violations: {violations}"
    )


def test_wi3_orchestrator_never_wires_entity_proposal_or_needs_you_review():
    """WI-3 §43: AI document classification cannot alter
    `EvidenceItem.entity_id` — ownership and document type remain
    separate authorities. `ENTITY_PROPOSAL` is never invoked/modified,
    and the Needs You producer `ITEM_TYPE_CLASSIFICATION_REVIEW` stays
    completely untouched."""
    forbidden_tokens = ("ENTITY_PROPOSAL", "ITEM_TYPE_CLASSIFICATION_REVIEW")
    text = _WI3_ORCHESTRATOR_MODULE_PATH.read_text(encoding="utf-8")
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi3_orchestrator_never_calls_claude_code_or_operator_document_review():
    """WI-3 §44: no silent cloud/Claude escalation on a failed local
    classification. Neither `OPERATOR_DOCUMENT_REVIEW` nor any
    Claude-operator-gateway symbol ever appears in the orchestrator."""
    forbidden_tokens = ("OPERATOR_DOCUMENT_REVIEW", "ClaudeCodeOperatorRunner", "claude_code", "ASK_BAGMAN")
    text = _WI3_ORCHESTRATOR_MODULE_PATH.read_text(encoding="utf-8")
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi3_orchestrator_never_imports_persistence_or_app():
    """WI-3's orchestrator is dependency-injected (constructor/call
    parameters only) — it must never import `app.*`/`persistence.*`
    directly, mirroring `ai.gateway.background.run_background_task`'s
    own documented discipline."""
    forbidden_roots = {"app", "persistence"}
    violations = []
    for imp in _imports_of(_WI3_ORCHESTRATOR_MODULE_PATH):
        if imp.root in forbidden_roots:
            violations.append(f"classification_orchestrator.py:{imp.lineno} imports {imp.full!r}")
    assert violations == [], "\n".join(violations)


def test_wi3_document_type_proposal_v2_rejected_by_generic_ai_tasks_endpoint():
    """CD-6 Slice 5 WI-3 §22 — a genuine request-level proof (this
    file's usual style is purely static/import-based, but §22 is
    fundamentally an HTTP-behavioural guarantee: "this exact request
    shape is refused" can only be proven by actually sending it).
    `DOCUMENT_TYPE_PROPOSAL` v2 must be refused by the generic
    `POST /internal/ai/tasks` surface; v1 must remain completely
    unaffected through the exact same surface."""
    from fastapi.testclient import TestClient

    from app.api.composition import get_composition, reset_composition_for_tests
    from app.api.main import app

    reset_composition_for_tests()
    client = TestClient(app)
    composition = get_composition()

    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD", provider="architecture-boundary-test", status="ACTIVE",
        actor_type="SYSTEM", actor_id="wi3-boundary-test",
    )
    import hashlib
    from datetime import datetime, timezone

    from core import identity as _identity

    content = b"Synthetic WI-3 architecture-boundary-test evidence."
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = composition.object_store.put(_identity.generate_id(), content_hash, content)
    evidence = composition.api.register_evidence(
        entity_id=None, evidence_type="INVOICE", source_id=source.source_id,
        observed_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc),
        content_hash=content_hash, mime_type="text/plain", size_bytes=len(content),
        actor_type="SYSTEM", actor_id="wi3-boundary-test", storage_reference=storage_reference,
    )

    v2_response = client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL", "task_version": 2,
            "input_references": {"evidence_id": evidence.evidence_id},
            "actor_type": "SYSTEM", "actor_id": "wi3-boundary-test",
        },
    )
    assert v2_response.status_code == 422, (
        f"expected DOCUMENT_TYPE_PROPOSAL v2 to be rejected (422) on the generic path, "
        f"got {v2_response.status_code}: {v2_response.text}"
    )

    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.5, "signals": [], "warnings": []}),
    )
    v1_response = client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL", "task_version": 1,
            "input_references": {"evidence_id": evidence.evidence_id},
            "actor_type": "SYSTEM", "actor_id": "wi3-boundary-test",
        },
    )
    assert v1_response.status_code == 200, (
        f"v1 must remain completely callable through the generic path (WI-3 §22) — got "
        f"{v1_response.status_code}: {v1_response.text}"
    )
    assert v1_response.json()["task_version"] == 1


# ---------------------------------------------------------------------
# CD-6 Slice 5 WI-4 — human-control-loop architecture-boundary proofs
# (§61). `_WI4_REVIEW_MODULE_PATH` is deliberately its OWN, separate
# module path — never added to `_CLASSIFICATION_MODULE_PATHS`, which
# still (correctly) forbids `ITEM_TYPE_CLASSIFICATION_REVIEW`/
# `ENTITY_PROPOSAL` wiring for WI-1/WI-2/WI-3's own modules; WI-4's
# whole job is to wire exactly that review vocabulary in, just from a
# NEW module.
# ---------------------------------------------------------------------

_WI4_REVIEW_MODULE_PATH = REPO_ROOT / "services" / "evidence" / "classification_review.py"


def test_wi4_review_module_never_imports_ai_provider_or_gateway_code():
    """WI-4 §2/§61 — the human-control loop never itself calls AI to
    decide an operator resolution: `classification_review.py` must
    never import anything under `ai.providers`/`ai.gateway`/`ai.tasks`
    (it does not even need `ai.invocation` — it only ever handles
    classification rows the orchestrator already resolved)."""
    forbidden_ai_roots = {"ai"}
    violations = [
        f"{_WI4_REVIEW_MODULE_PATH.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}"
        for imp in _imports_of(_WI4_REVIEW_MODULE_PATH)
        if imp.root in forbidden_ai_roots
    ]
    assert violations == [], "\n".join(violations)


def test_wi4_review_module_never_wires_entity_proposal_claude_or_evidence_mutation():
    """WI-4 §61 — the resolver never invokes `ENTITY_PROPOSAL`, never
    calls the Claude operator gateway, and never mutates `EvidenceItem`
    (`update_status`/`assign_entity` are the only two EvidenceItem
    mutation paths anywhere in this codebase — see this file's own
    WI-1 `test_evidence_classification_code_never_mutates_evidence_item`
    for the identical doctrine applied to WI-1/WI-2/WI-3's modules)."""
    forbidden_tokens = (
        "ENTITY_PROPOSAL", "ClaudeCodeOperatorRunner", "claude_code", "ASK_BAGMAN",
        ".update_status(", ".assign_entity(",
    )
    text = _WI4_REVIEW_MODULE_PATH.read_text(encoding="utf-8")
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi4_review_module_never_imports_mailbox_provider_code():
    """WI-4 §61 — the resolver never fetches mailbox provider data; it
    only ever reads the `EvidenceItem.metadata` a caller already
    fetched. No `services.mailbox.*` import anywhere in this module."""
    violations = [
        f"{_WI4_REVIEW_MODULE_PATH.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}"
        for imp in _imports_of(_WI4_REVIEW_MODULE_PATH)
        if imp.full == "services.mailbox" or imp.full.startswith("services.mailbox.")
    ]
    assert violations == [], "\n".join(violations)


def test_wi4_orchestrator_review_wiring_never_imports_persistence_or_app():
    """WI-4 extends `classify_evidence` in-place — it must still never
    import `app.*`/`persistence.*` directly (WI-3's own existing
    `test_wi3_orchestrator_never_imports_persistence_or_app` re-run
    above already proves this holds after the WI-4 edit; this is the
    same proof, named for WI-4, so a future regression in either
    delivery is caught under the section its own author is looking
    at)."""
    forbidden_roots = {"app", "persistence"}
    violations = [
        f"classification_orchestrator.py:{imp.lineno} imports {imp.full!r}"
        for imp in _imports_of(_WI3_ORCHESTRATOR_MODULE_PATH)
        if imp.root in forbidden_roots
    ]
    assert violations == [], "\n".join(violations)


def test_wi4_document_type_vocabulary_unchanged():
    """WI-4 §61 — no new classification vocabulary: `document_type`
    remains exactly the 8 existing WI-1 values."""
    from services.evidence.classification import DOCUMENT_TYPES

    assert DOCUMENT_TYPES == frozenset(
        {
            "SUPPLIER_INVOICE", "RECEIPT", "ORDER_CONFIRMATION", "REFUND_CONFIRMATION",
            "BROKER_STATEMENT", "BROKER_ACTIVITY_NOTICE", "NON_ACCOUNTING_DOCUMENT", "UNKNOWN",
        }
    )
    assert len(DOCUMENT_TYPES) == 8


# ---------------------------------------------------------------------
# CD-6 Slice 5 WI-5 — GUI-operations-foundation Document projection
# boundary proofs (WO §64): the new projection service/router must
# never call ENTITY_PROPOSAL, never mutate EvidenceItem ownership,
# never import services.xero.*/services.mailbox.*, and never create an
# EvidenceClassification/EvidenceClassificationRule row itself (a pure
# read projection, by construction).
# ---------------------------------------------------------------------

_WI5_PROJECTION_MODULE_PATH = REPO_ROOT / "services" / "evidence" / "document_projection.py"
_WI5_ROUTER_MODULE_PATH = REPO_ROOT / "app" / "api" / "routers" / "documents.py"
_WI5_MODULE_PATHS = [_WI5_PROJECTION_MODULE_PATH, _WI5_ROUTER_MODULE_PATH]


def test_wi5_projection_modules_never_wire_entity_proposal_or_mutate_evidence():
    """WI-5 §64 — the projection never invokes `ENTITY_PROPOSAL`, and
    never calls either of the two real `EvidenceItem` mutation paths
    (`update_status`/`assign_entity` — see this file's own WI-1
    `test_evidence_classification_code_never_mutates_evidence_item` for
    the identical doctrine)."""
    forbidden_tokens = ("ENTITY_PROPOSAL", ".update_status(", ".assign_entity(")
    violations = []
    for path in _WI5_MODULE_PATHS:
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {token!r}")
    assert violations == [], "\n".join(violations)


def test_wi5_projection_modules_never_import_xero_or_mailbox_code():
    """WI-5 §64 — document review must never fetch live mailbox
    provider data, and this GUI-operations projection has no business
    calling Xero at all. No `services.xero.*`/`services.mailbox.*`
    import anywhere in either new module."""
    forbidden_roots = {"services.xero", "services.mailbox"}
    violations = []
    for path in _WI5_MODULE_PATHS:
        for imp in _imports_of(path):
            if any(imp.full == mod or imp.full.startswith(mod + ".") for mod in forbidden_roots):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{imp.lineno} imports {imp.full!r}")
    assert violations == [], "\n".join(violations)


def test_wi5_projection_modules_never_create_classification_or_rule_rows():
    """WI-5 §5/§49 — a pure read projection: neither module may call
    any classification/rule CREATE path (`create_classification`,
    `create_classification_with_result`, `create_classification_rule`).
    Retrieval-only calls (`get_current_classification`,
    `list_classification_history`, `get_classification`) are fine and
    expected."""
    forbidden_substrings = [
        "create_classification(", "create_classification_with_result(", "create_classification_rule(",
    ]
    violations = []
    for path in _WI5_MODULE_PATHS:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_substrings:
            if pattern in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {pattern!r}")
    assert violations == [], "\n".join(violations)


def test_wi5_projection_service_never_imports_app_or_persistence():
    """The projection service is dependency-injected (constructor/call
    parameters only), mirroring every other services.evidence.* module
    in this codebase (see e.g. classification_orchestrator's own
    identical proof) — it must never import `app.*`/`persistence.*`
    directly. Only the router (the HTTP layer) may unpack
    `get_composition()` onto these parameters."""
    forbidden_roots = {"app", "persistence"}
    violations = [
        f"document_projection.py:{imp.lineno} imports {imp.full!r}"
        for imp in _imports_of(_WI5_PROJECTION_MODULE_PATH)
        if imp.root in forbidden_roots
    ]
    assert violations == [], "\n".join(violations)


def test_wi5_projection_module_never_imports_ai_provider_or_gateway_code():
    """The read projection never runs AI itself — no
    `ai.providers`/`ai.gateway`/`ai.prompts` import."""
    forbidden_ai_submodules = {"ai.providers", "ai.gateway", "ai.prompts"}
    violations = [
        f"document_projection.py:{imp.lineno} imports {imp.full!r}"
        for imp in _imports_of(_WI5_PROJECTION_MODULE_PATH)
        if any(imp.full == mod or imp.full.startswith(mod + ".") for mod in forbidden_ai_submodules)
    ]
    assert violations == [], "\n".join(violations)


# ---------------------------------------------------------------------
# CD-6 Slice 5 WI-6 — historical reprocessing / evaluation tooling
# boundary proofs (WO §61). Both new tools live under `scripts/` (real
# operator CLI utilities, never `app/api/routers/*`), so their own
# "no router" property is trivially true by construction — these tests
# instead prove the substantive WI-6 boundaries: DETERMINISTIC_RULE_ONLY
# for the reprocessing tool (no AI, no evidence/entity mutation, no rule
# creation, no unbounded/un-gated apply), and a true read-only SHADOW
# tool for the evaluator (no classification/rule/NeedsYou writes, no
# mailbox/Xero reach).
# ---------------------------------------------------------------------

_WI6_REPROCESS_MODULE_PATH = REPO_ROOT / "scripts" / "reprocess_evidence_classification.py"
_WI6_EVALUATE_MODULE_PATH = REPO_ROOT / "scripts" / "evaluate_document_classifier.py"


def test_wi6_reprocess_script_never_imports_ai_or_mailbox_or_xero_code():
    """WI-6 §4 — DETERMINISTIC_RULE_ONLY: this tool must never import
    anything under `ai.*` (no AI fallback of any kind), and, like every
    other classification module in this codebase, never
    `services.mailbox.*`/`services.xero.*`."""
    violations = []
    for imp in _imports_of(_WI6_REPROCESS_MODULE_PATH):
        if imp.root == "ai":
            violations.append(f"reprocess_evidence_classification.py:{imp.lineno} imports {imp.full!r}")
        if imp.full == "services.mailbox" or imp.full.startswith("services.mailbox."):
            violations.append(f"reprocess_evidence_classification.py:{imp.lineno} imports {imp.full!r}")
        if imp.full == "services.xero" or imp.full.startswith("services.xero."):
            violations.append(f"reprocess_evidence_classification.py:{imp.lineno} imports {imp.full!r}")
    assert violations == [], "\n".join(violations)


def test_wi6_reprocess_script_never_calls_any_ai_provider_shaped_function():
    """WI-6 §4/§16 — no `litellm_client`/`run_background_task` token
    anywhere in this tool's own source at all (it never even accepts an
    AI dependency as a parameter)."""
    text = _WI6_REPROCESS_MODULE_PATH.read_text(encoding="utf-8")
    forbidden_tokens = ("litellm_client", "run_background_task", "ai_invocation_repository")
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi6_reprocess_script_never_mutates_evidence_ownership_or_creates_rules():
    """WI-6 §61 — the historical tool may never call either real
    `EvidenceItem` mutation path (`update_status`/`assign_entity` — see
    this file's own WI-1 proof for the identical doctrine), never
    `create_classification_rule(` (no new-rule creation), and never
    constructs a `supersedes_classification_id=` itself — the only
    write path it may use is the existing, unmodified
    `classify_evidence_deterministically`, whose own internal
    supersession behaviour (none, in V1) it must never second-guess or
    duplicate."""
    text = _WI6_REPROCESS_MODULE_PATH.read_text(encoding="utf-8")
    forbidden_tokens = (
        ".update_status(", ".assign_entity(", "create_classification_rule(", "supersedes_classification_id=",
    )
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi6_reprocess_script_cli_requires_limit_and_manifest_hash_for_apply():
    """WI-6 §9/§10 — `--apply` genuinely requires both
    `--expected-manifest-sha256` and a bounded `--limit`; neither may be
    silently defaulted. Exercised via the real `_parse_args` function,
    not by eyeballing the argparse definition."""
    import scripts.reprocess_evidence_classification as reprocess_script

    with pytest.raises(SystemExit):
        reprocess_script._parse_args(["--runtime-dir", "/tmp/wi6-boundary", "--rule-id", "r1", "--apply", "--limit", "5"])
    with pytest.raises(SystemExit):
        reprocess_script._parse_args([
            "--runtime-dir", "/tmp/wi6-boundary", "--rule-id", "r1", "--apply",
            "--expected-manifest-sha256", "a" * 64,
        ])
    # Both present -> accepted (proves the two errors above are genuinely
    # about the missing flag, not some other rejection).
    args = reprocess_script._parse_args([
        "--runtime-dir", "/tmp/wi6-boundary", "--rule-id", "r1", "--apply",
        "--expected-manifest-sha256", "a" * 64, "--limit", "5",
    ])
    assert args.apply is True
    assert args.limit == 5


def test_wi6_reprocess_script_never_defines_a_router_or_http_endpoint():
    """WI-6 §3 — the historical tool is a CLI script only; it must never
    import `fastapi`/`APIRouter` or define an endpoint decorator."""
    text = _WI6_REPROCESS_MODULE_PATH.read_text(encoding="utf-8")
    forbidden_tokens = ("fastapi", "APIRouter", "@router.")
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi6_evaluate_script_never_imports_mailbox_or_xero_code():
    """WI-6 §61 — the shadow evaluator never fetches live mailbox
    provider data and has no business calling Xero at all."""
    violations = []
    for imp in _imports_of(_WI6_EVALUATE_MODULE_PATH):
        if imp.full == "services.mailbox" or imp.full.startswith("services.mailbox."):
            violations.append(f"evaluate_document_classifier.py:{imp.lineno} imports {imp.full!r}")
        if imp.full == "services.xero" or imp.full.startswith("services.xero."):
            violations.append(f"evaluate_document_classifier.py:{imp.lineno} imports {imp.full!r}")
    assert violations == [], "\n".join(violations)


def test_wi6_evaluate_script_never_calls_any_classification_rule_or_needs_you_write_path():
    """WI-6 §19/§26/§61 — a true SHADOW tool: zero call sites anywhere
    in this script's own source for
    `create_classification(`/`create_classification_with_result(`/
    `create_classification_rule(`/`ensure_classification_review_item(`.
    This script only ever calls the existing, governed
    `classify_evidence(persist=False, ...)` — already proven elsewhere
    to make every one of those write call sites structurally
    unreachable under `persist=False` — and never any of them directly
    itself."""
    text = _WI6_EVALUATE_MODULE_PATH.read_text(encoding="utf-8")
    forbidden_substrings = [
        "create_classification(", "create_classification_with_result(", "create_classification_rule(",
        "ensure_classification_review_item(",
    ]
    violations = [pattern for pattern in forbidden_substrings if pattern in text]
    assert violations == [], "\n".join(violations)


def test_wi6_evaluate_script_never_defines_a_router_or_http_endpoint():
    """WI-6 §3 — the evaluator is a CLI script only."""
    text = _WI6_EVALUATE_MODULE_PATH.read_text(encoding="utf-8")
    forbidden_tokens = ("fastapi", "APIRouter", "@router.")
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi6_evaluate_script_never_calls_persist_true():
    """WI-6 §19 — the evaluator must always call the governed
    orchestrator with `persist=False`; it must never spell
    `persist=True` anywhere in its own source (the one and only
    `classify_evidence(...)` call site in this script is grepped for
    this literal)."""
    text = _WI6_EVALUATE_MODULE_PATH.read_text(encoding="utf-8")
    assert "persist=True" not in text
    assert "persist=False" in text


# ---------------------------------------------------------------------
# CD-6 follow-up ("AI classifier boundary correction") — the new
# `--ai-only` evaluation path added to this SAME script (Task 4) gets
# its own, extended boundary proofs: never any classification/rule/
# NeedsYou write (already covered above, since those tests scan the
# WHOLE file's text), and — going further, since this path is fully
# disposable by design — never even an `AIInvocation` row either.
# ---------------------------------------------------------------------


def test_wi6_evaluate_script_ai_only_path_never_creates_an_ai_invocation_row():
    """The `--ai-only` path (`evaluate_item_ai_only`/
    `run_ai_only_evaluation`) calls `litellm_client.complete()` directly
    and validates with `ai.tasks.validate_task_output` — it never calls
    `ai.gateway.background.run_background_task` (which would create a
    real, durable `AIInvocation` row via a repository) and never calls
    any repository's own `create_invocation(` directly either, so it can
    never create an `AIInvocation` row for either a real evidence_id or
    a synthetic challenge-set fixture."""
    text = _WI6_EVALUATE_MODULE_PATH.read_text(encoding="utf-8")
    forbidden_tokens = ("run_background_task(", "create_invocation(")
    violations = [token for token in forbidden_tokens if token in text]
    assert violations == [], "\n".join(violations)


def test_wi6_evaluate_script_ai_only_path_functions_exist_and_are_importable():
    """A cheap, direct proof that Task 4's new surface actually exists
    with the expected names (a real import, not just a text grep) —
    catches an accidental rename/removal that the text-scan tests above
    would not."""
    import scripts.evaluate_document_classifier as evl

    assert callable(evl.load_challenge_set)
    assert callable(evl.evaluate_item_ai_only)
    assert callable(evl.run_ai_only_evaluation)
