#!/usr/bin/env python3
"""Generate BAGMAN's architecture-memory projection (PID §27-28).

Reads three authoritative sources from the source tree:

* every ``component.yaml`` manifest in the repository (discovered by
  glob, not a hardcoded list), each validated against
  ``contracts/manifest/bagman.component_manifest.v1.schema.json``;
* every ``contracts/**/*.schema.json`` contract file, for its
  ``title``/``description``/``$id``;
* the canonical entity registry, ``config/base/entities.yaml``;

and renders a single deterministic Markdown file,
``memory/generated/architecture-index.md``.

This script *is* the validator for component manifests — there is no
separate standalone validation entry point. Any invalid
``component.yaml`` causes this script to fail loudly (non-zero exit,
a clear message naming the offending file and the validation error)
before anything is rendered or written.

"Deterministic" means: running this script twice against the same
inputs produces byte-identical output. No timestamps, wall-clock
values, hash-randomised orderings, or filesystem-iteration-order
artifacts may appear in the generated content — every section is
explicitly sorted by a stable key before rendering.

Usage
-----
    python3 scripts/generate_architecture_memory.py
        Regenerate memory/generated/architecture-index.md in place.

    python3 scripts/generate_architecture_memory.py --check
        Regenerate the content in memory only and compare it against
        the committed file. Exits non-zero (without writing anything)
        if they differ — intended for a later CI/architectural test
        that wants to catch architecture-memory drift (PID §28).

Authority boundary (PID §27)
-----------------------------
The Markdown this script produces is a generated *projection*, not
canonical truth. See ``memory/architecture/README.md`` for the full
statement of this boundary: canonical domain state lives in
``core/``/``services/*`` (contracts + repositories) and is
authoritative; this file (and everything else under ``memory/``) must
never be treated as the source of truth for EvidenceItem/
GovernedEntity/AuditEvent identities.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "memory" / "generated" / "architecture-index.md"

BANNER = (
    "<!--\n"
    "  GENERATED FILE — DO NOT HAND-EDIT.\n"
    "  Produced by scripts/generate_architecture_memory.py from:\n"
    "    * every component.yaml manifest in the repository\n"
    "    * every contracts/**/*.schema.json contract\n"
    "    * config/base/entities.yaml\n"
    "  Regenerate with: python3 scripts/generate_architecture_memory.py\n"
    "  Check for drift with: python3 scripts/generate_architecture_memory.py --check\n"
    "  This is a projection, not canonical truth — see\n"
    "  memory/architecture/README.md (PID §27-28).\n"
    "-->\n"
)


class ManifestValidationError(RuntimeError):
    """Raised when a component.yaml fails schema validation."""


def _excluded(path: Path) -> bool:
    """True if `path` sits under a directory this script must ignore
    (VCS internals, git worktree metadata, virtualenvs, caches)."""
    ignored_parts = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    return any(part in ignored_parts for part in path.parts)


def discover_component_manifests(repo_root: Path) -> list[Path]:
    return sorted(
        p for p in repo_root.rglob("component.yaml") if not _excluded(p)
    )


def discover_contract_schemas(repo_root: Path) -> list[Path]:
    contracts_dir = repo_root / "contracts"
    if not contracts_dir.is_dir():
        return []
    return sorted(
        p for p in contracts_dir.rglob("*.schema.json") if not _excluded(p)
    )


def load_manifest_schema(schema_path: Path) -> dict[str, Any]:
    with schema_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_and_validate_manifest(
    path: Path, validator: Draft202012Validator, repo_root: Path
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    rel_path = path.relative_to(repo_root)
    if not isinstance(data, dict):
        raise ManifestValidationError(
            f"{rel_path}: manifest must be a YAML mapping, got "
            f"{type(data).__name__}"
        )

    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in errors
        )
        raise ManifestValidationError(
            f"{rel_path}: failed component-manifest schema validation — {details}"
        )
    return data


def load_component_manifests(repo_root: Path) -> list[dict[str, Any]]:
    schema_path = repo_root / "contracts" / "manifest" / "bagman.component_manifest.v1.schema.json"
    schema = load_manifest_schema(schema_path)
    validator = Draft202012Validator(schema)

    manifests = []
    for path in discover_component_manifests(repo_root):
        manifests.append(load_and_validate_manifest(path, validator, repo_root))
    return sorted(manifests, key=lambda m: m["id"])


def load_contracts(repo_root: Path) -> list[dict[str, Any]]:
    contracts = []
    for path in discover_contract_schemas(repo_root):
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        contracts.append(
            {
                "id": data.get("$id", ""),
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "path": path.relative_to(repo_root).as_posix(),
            }
        )
    return sorted(contracts, key=lambda c: c["id"])


def load_entities(entities_path: Path) -> list[dict[str, Any]]:
    if not entities_path.is_file():
        return []
    with entities_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entities = data.get("entities", {}) or {}
    result = [
        {
            "key": key,
            "display_name": value.get("display_name", ""),
            "type": value.get("type", ""),
        }
        for key, value in entities.items()
    ]
    return sorted(result, key=lambda e: e["key"])


def _render_list(items: list[str]) -> str:
    if not items:
        return "_(none)_"
    return ", ".join(f"`{item}`" for item in items)


def render_components_section(components: list[dict[str, Any]]) -> str:
    lines = ["## Components", ""]
    if not components:
        lines.append("_(none declared)_")
        lines.append("")
        return "\n".join(lines)

    for component in components:
        lines.append(f"### `{component['id']}` (v{component['version']})")
        lines.append("")
        lines.append(component["responsibility"].strip())
        lines.append("")
        lines.append(f"- **Owns:** {_render_list(component['owns'])}")
        lines.append(f"- **Consumes:** {_render_list(component['consumes'])}")
        lines.append(f"- **Produces:** {_render_list(component['produces'])}")
        lines.append(f"- **Dependencies:** {_render_list(component['dependencies'])}")
        lines.append(f"- **External access:** `{str(component['external_access']).lower()}`")
        lines.append(f"- **Prohibited:** {_render_list(component['prohibited'])}")
        lines.append("")
    return "\n".join(lines)


def render_contracts_section(contracts: list[dict[str, Any]]) -> str:
    lines = ["## Contracts", ""]
    if not contracts:
        lines.append("_(none declared)_")
        lines.append("")
        return "\n".join(lines)

    lines.append("| `$id` | Title | Path |")
    lines.append("|-------|-------|------|")
    for contract in contracts:
        title = contract["title"] or "_(untitled)_"
        lines.append(f"| `{contract['id']}` | {title} | `{contract['path']}` |")
    lines.append("")
    return "\n".join(lines)


def render_entities_section(entities: list[dict[str, Any]]) -> str:
    lines = ["## Canonical Entities", ""]
    if not entities:
        lines.append("_(none declared)_")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Key | Display Name | Type |")
    lines.append("|-----|--------------|------|")
    for entity in entities:
        lines.append(f"| `{entity['key']}` | {entity['display_name']} | `{entity['type']}` |")
    lines.append("")
    return "\n".join(lines)


def generate(repo_root: Path = REPO_ROOT) -> str:
    components = load_component_manifests(repo_root)
    contracts = load_contracts(repo_root)
    entities = load_entities(repo_root / "config" / "base" / "entities.yaml")

    sections = [
        BANNER,
        "# BAGMAN Architecture Index",
        "",
        render_components_section(components),
        render_contracts_section(contracts),
        render_entities_section(entities),
    ]
    content = "\n".join(sections)
    if not content.endswith("\n"):
        content += "\n"
    return content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Regenerate in memory and compare against the committed file; "
        "exit non-zero on drift without writing anything.",
    )
    args = parser.parse_args(argv)

    try:
        content = generate(REPO_ROOT)
    except ManifestValidationError as exc:
        print(f"error: invalid component manifest: {exc}", file=sys.stderr)
        return 1

    if args.check:
        if not OUTPUT_PATH.is_file():
            print(
                f"error: {OUTPUT_PATH.relative_to(REPO_ROOT)} does not exist "
                "— run without --check to generate it",
                file=sys.stderr,
            )
            return 1
        existing = OUTPUT_PATH.read_text(encoding="utf-8")
        if existing != content:
            print(
                "error: memory/generated/architecture-index.md is out of date "
                "with the source tree (component.yaml manifests, contracts, "
                "or config/base/entities.yaml changed since it was last "
                "generated). Run: python3 scripts/generate_architecture_memory.py",
                file=sys.stderr,
            )
            return 1
        print("architecture-index.md is up to date.")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
