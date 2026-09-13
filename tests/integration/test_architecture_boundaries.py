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
# ---------------------------------------------------------------------

_FORBIDDEN_MAILBOX_AND_PROVIDER_IMPORT_ROOTS = {
    "msal",
    "googleapiclient",
    "google_auth_oauthlib",
    "imapclient",
    "imaplib",
    "exchangelib",
    "O365",
}


def test_no_forbidden_mailbox_or_provider_sdk_imported_anywhere_in_the_repo():
    """PID §67/§69: no Microsoft Graph/MSAL, Gmail/Google API client
    library, or ``imapclient``/``imaplib``-based mailbox polling code
    exists anywhere in the repository yet — CD-5's job, not CD-4's.
    Scans every ``.py`` file under the repo (excluding
    third-party-managed directories), via real ``ast`` import parsing,
    not a text grep."""
    violations = []
    search_roots = ["core", "services", "persistence", "app", "adapters", "agent", "ui", "scripts", "tests", "ops"]
    for path in _py_files(*search_roots):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for imp in _imports_of(path):
            if imp.root in _FORBIDDEN_MAILBOX_AND_PROVIDER_IMPORT_ROOTS:
                violations.append(f"{rel}:{imp.lineno} imports {imp.full!r}")

    assert not violations, (
        "no Microsoft Graph/MSAL, Gmail/Google API client library, or "
        "imapclient/imaplib mailbox-polling import may exist yet (PID §67/§69) "
        "— CD-5's job, not CD-4's. Violations:\n" + "\n".join(violations)
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
# Deliberately EXCLUDES `tests/` itself: several existing test files
# legitimately use `trinity-fast`/`trinity-core`/`trinity-deep`/
# `trinity-embed` as literal ADVERSARIAL/NEGATIVE fixture values (e.g.
# `tests/integration/test_litellm_client.py`'s own
# "reject every forbidden alias" parametrisation, `tests/contract/
# test_ai_invocation_contract.py`'s "this value must fail contract
# validation" fixture) — a test proving BAGMAN rejects a trinity-*
# alias necessarily contains that string once, and that is correct,
# not a violation. What matters is that no APPLICATION source file
# (everything BAGMAN actually ships/runs) contains one.
# ---------------------------------------------------------------------

_TRINITY_STAR_ALIASES_REPO_WIDE = ["trinity-fast", "trinity-core", "trinity-deep", "trinity-embed"]
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
    """PID §9/§75/§87/§92: no `trinity-fast`/`trinity-core`/`trinity-deep`/
    `trinity-embed` literal may exist anywhere in BAGMAN's own shipped
    application source — only `bagman-fast`/`bagman-core`/`bagman-deep`
    (`ai.invocation.BACKGROUND_CAPABILITY_ALIASES`) are ever permitted.
    Scans every file (not just `.py`) under the application-source
    roots, so a stray reference in a `.yml`/`.md`/`.json`/`.sh` file
    would be caught too, not just a Python import."""
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
