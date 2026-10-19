"""Export a finished design as machine-readable specs.

An LLM converts the reviewed design text into an OpenAPI document and a
PostgreSQL schema. The output is generated, not guaranteed: it is checked for
structure here (and parsed by sqlglot when that is installed), and anything the
checks flag is reported in the manifest and by the CLI.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from src.core.config import settings
from src.core.llm import invoke_checked
from src.export.prompts import (
    OPENAPI_SYSTEM_PROMPT,
    SQL_SYSTEM_PROMPT,
    build_export_prompt,
)

FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
DESTRUCTIVE_SQL = re.compile(r"\b(DROP|TRUNCATE|DELETE\s+FROM)\b", re.IGNORECASE)

MANIFEST_NOTE = (
    "openapi.yaml and schema.sql are LLM-generated from the design text. They have "
    "not been executed, and the security audit covers the design, not this output. "
    "Review them before use."
)


@dataclass
class ExportResult:
    openapi_yaml: str
    schema_sql: str
    problems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        return any(self.problems.values())


def strip_code_fence(text: str) -> str:
    """Return the longest fenced block if the model added fences, else the text."""
    blocks = FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()


def _collect_refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value)
            else:
                _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


def _resolves(doc: dict, ref: str) -> bool:
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def validate_openapi(text: str) -> list[str]:
    """Structural checks. Returns problems; empty means none were found."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"not valid YAML: {str(exc).splitlines()[0]}"]
    if not isinstance(doc, dict):
        return ["the document is not a YAML mapping"]

    problems = []
    if not str(doc.get("openapi", "")).startswith("3."):
        problems.append("missing or unsupported 'openapi' version (expected 3.x)")
    info = doc.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("title"), str):
        problems.append("missing info.title")
    components = doc.get("components") if isinstance(doc.get("components"), dict) else {}
    if not components.get("securitySchemes"):
        problems.append("no components.securitySchemes declared")

    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        problems.append("no paths defined")
        paths = {}
    for path, item in paths.items():
        operations = (
            {m: op for m, op in item.items() if m in HTTP_METHODS}
            if isinstance(item, dict)
            else {}
        )
        if not operations:
            problems.append(f"{path}: no HTTP operations")
            continue
        for method, operation in operations.items():
            label = f"{method.upper()} {path}"
            if not isinstance(operation, dict) or not operation.get("responses"):
                problems.append(f"{label}: no responses defined")
                continue
            # An explicit empty list marks a public operation; absence of any
            # security requirement is how an endpoint ends up unauthenticated.
            if "security" not in operation and doc.get("security") is None:
                problems.append(
                    f"{label}: no security requirement (add one, or `security: []` if public)"
                )

    refs: set[str] = set()
    _collect_refs(doc, refs)
    for ref in sorted(refs):
        if ref.startswith("#/") and not _resolves(doc, ref):
            problems.append(f"unresolved $ref {ref}")
    return problems


def _strip_sql(text: str) -> str:
    """Drop comments and string literals so structural checks ignore them."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    return re.sub(r"'(?:[^']|'')*'", "''", text)


def validate_sql(text: str) -> list[str]:
    """Structural checks, plus a real PostgreSQL parse when sqlglot is installed."""
    body = _strip_sql(text)
    problems = []
    if not re.search(r"\bCREATE\s+TABLE\b", body, re.IGNORECASE):
        problems.append("no CREATE TABLE statement")
    if body.count("(") != body.count(")"):
        problems.append("unbalanced parentheses")
    if DESTRUCTIVE_SQL.search(body):
        problems.append("contains destructive statements (DROP / TRUNCATE / DELETE)")
    if body.strip() and not body.rstrip().endswith(";"):
        problems.append("the last statement is not terminated with ';'")

    try:
        import sqlglot
        from sqlglot.errors import SqlglotError
    except ImportError:
        return problems
    try:
        sqlglot.parse(text, read="postgres")
    except SqlglotError as exc:
        problems.append(f"does not parse as PostgreSQL: {str(exc).splitlines()[0][:200]}")
    return problems


def export_design(llm, design: dict) -> ExportResult:
    """Generate and check openapi.yaml and schema.sql for `design`."""
    architecture = design.get("final_architecture") or ""
    api_spec = design.get("api_spec") or ""
    if not architecture.strip() or not api_spec.strip():
        raise ValueError("The design has no final_architecture / api_spec to export.")

    human = HumanMessage(content=build_export_prompt(architecture, api_spec))
    with ThreadPoolExecutor(max_workers=2) as pool:
        openapi_future = pool.submit(
            invoke_checked,
            llm,
            [SystemMessage(content=OPENAPI_SYSTEM_PROMPT), human],
            "openapi export",
        )
        sql_future = pool.submit(
            invoke_checked,
            llm,
            [SystemMessage(content=SQL_SYSTEM_PROMPT), human],
            "sql export",
        )
        openapi_yaml = strip_code_fence(openapi_future.result())
        schema_sql = strip_code_fence(sql_future.result())

    return ExportResult(
        openapi_yaml=openapi_yaml,
        schema_sql=schema_sql,
        problems={
            "openapi": validate_openapi(openapi_yaml),
            "schema": validate_sql(schema_sql),
        },
    )


def write_export(result: ExportResult, design: dict, out_dir: Path | str) -> list[Path]:
    """Write the specs, the design documents and a manifest into `out_dir`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    documents = {
        "design.md": design.get("final_architecture") or "",
        "api-spec.md": design.get("api_spec") or "",
        "security-audit.md": design.get("security_audit") or "",
        "openapi.yaml": result.openapi_yaml,
        "schema.sql": result.schema_sql,
    }
    paths = []
    for name, content in documents.items():
        path = out / name
        path.write_text(content.rstrip() + "\n", encoding="utf-8", newline="\n")
        paths.append(path)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.OPENROUTER_MODEL,
        "design_approved": bool(design.get("approved")),
        "total_revisions": design.get("total_revisions"),
        "files": list(documents),
        "validation": result.problems,
        "note": MANIFEST_NOTE,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    paths.append(manifest_path)
    return paths
