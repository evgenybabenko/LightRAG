"""CLI for Codex decision-memory workflows backed by LightRAG."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import fnmatch
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib import error, request

from lightrag.tools.memory_config import MemoryConfig, MemoryEndpoint, load_memory_config
from lightrag.tools.memory_notes import (
    build_discussion_note,
    now_iso,
    parse_front_matter,
    update_front_matter,
    write_discussion_note,
)


DEFAULT_QUERY_PROMPT = (
    "Return a concise Codex context pack. Include fresh decisions, project "
    "constraints, known hazards, relevant tests, and source links. Highlight "
    "conflicts between discussion decisions and documentation. Treat "
    "status:candidate notes as non-authoritative unless the user explicitly "
    "asks for candidates. Do not present notes with superseded_by as current."
)

TEXT_EXTENSIONS = {".md", ".txt", ".rst"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class DatabaseSpec:
    database: str
    user: str
    password: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Codex decision memory through LightRAG."
    )
    parser.add_argument("--config", type=Path, help="Path to .codex-memory.toml")

    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap-db")
    bootstrap.set_defaults(func=cmd_bootstrap_db)
    bootstrap.add_argument("--host", default=_env("MEMORY_POSTGRES_HOST", "postgres"))
    bootstrap.add_argument(
        "--port", type=int, default=int(_env("MEMORY_POSTGRES_PORT", "5432"))
    )
    bootstrap.add_argument(
        "--connect-retries",
        type=int,
        default=int(_env("MEMORY_POSTGRES_CONNECT_RETRIES", "30")),
    )
    bootstrap.add_argument(
        "--connect-delay",
        type=float,
        default=float(_env("MEMORY_POSTGRES_CONNECT_DELAY", "2")),
    )
    bootstrap.add_argument(
        "--admin-user",
        default=_env("MEMORY_POSTGRES_ADMIN_USER", _env("POSTGRES_USER", "postgres")),
    )
    bootstrap.add_argument(
        "--admin-password",
        default=_env("MEMORY_POSTGRES_ADMIN_PASSWORD", _env("POSTGRES_PASSWORD", "")),
    )
    bootstrap.add_argument(
        "--admin-database",
        default=_env("MEMORY_POSTGRES_ADMIN_DATABASE", _env("POSTGRES_DB", "postgres")),
    )
    bootstrap.add_argument(
        "--target",
        choices=["all", "personal", "limbal"],
        default="all",
        help="Which memory database spec to bootstrap.",
    )

    ingest = subparsers.add_parser("ingest")
    _add_endpoint_args(ingest)
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--batch-size", type=int, default=8)
    ingest.add_argument("--exclude", action="append", default=[])
    ingest.set_defaults(func=cmd_ingest)

    query = subparsers.add_parser("query")
    _add_endpoint_args(query)
    query.add_argument("query")
    query.add_argument("--mode", default="mix")
    query.add_argument("--top-k", type=int, default=20)
    query.add_argument("--chunk-top-k", type=int, default=20)
    query.add_argument("--user-prompt", default=DEFAULT_QUERY_PROMPT)
    query.add_argument("--context-only", action="store_true")
    query.set_defaults(func=cmd_query)

    capture = subparsers.add_parser("capture-discussion")
    _add_endpoint_args(capture)
    capture.add_argument("--title", required=True)
    capture.add_argument("--decision", required=True)
    capture.add_argument("--why", default="")
    capture.add_argument("--changes", default="")
    capture.add_argument("--docs-updated", action="append", default=[])
    capture.add_argument("--supersedes", action="append", default=[])
    capture.add_argument("--status", choices=["candidate", "accepted"], default="candidate")
    capture.add_argument("--authority", default=None)
    capture.add_argument("--source", default="codex-thread")
    capture.add_argument("--notes-dir", type=Path)
    capture.add_argument("--ingest", action="store_true")
    capture.set_defaults(func=cmd_capture_discussion)

    promote = subparsers.add_parser("promote")
    _add_endpoint_args(promote)
    promote.add_argument("note", type=Path)
    promote.add_argument("--docs-updated", action="append", default=[])
    promote.add_argument("--ingest", action="store_true")
    promote.set_defaults(func=cmd_promote)

    reindex = subparsers.add_parser("reindex")
    _add_endpoint_args(reindex)
    reindex.add_argument("paths", nargs="*", type=Path)
    reindex.add_argument("--batch-size", type=int, default=8)
    reindex.add_argument("--exclude", action="append", default=[])
    reindex.set_defaults(func=cmd_reindex)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def cmd_bootstrap_db(args: argparse.Namespace) -> int:
    if not args.admin_password:
        raise SystemExit("admin password is required")
    specs = _selected_database_specs(args.target)
    asyncio.run(_bootstrap_databases(args, specs))
    print(json.dumps({"status": "ok", "databases": [spec.database for spec in specs]}))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    endpoint = _resolve_endpoint(args)
    payload: dict[str, Any] = {
        "query": args.query,
        "mode": args.mode,
        "top_k": args.top_k,
        "chunk_top_k": args.chunk_top_k,
        "include_references": True,
        "stream": False,
        "user_prompt": args.user_prompt,
    }
    if args.context_only:
        payload["only_need_context"] = True

    response = _post_json(endpoint, "/query", payload, args.timeout)
    print(_extract_query_text(response))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    endpoint = _resolve_endpoint(args)
    result = _ingest_paths(
        endpoint,
        args.paths,
        args.batch_size,
        args.timeout,
        args.exclude,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_capture_discussion(args: argparse.Namespace) -> int:
    config = _load_config(args)
    endpoint = _resolve_endpoint(args, config)
    notes_dir = args.notes_dir or endpoint.notes_dir or _default_notes_dir(args, config)
    authority = args.authority or ("confirmed" if args.status == "accepted" else "candidate")
    note = build_discussion_note(
        title=args.title,
        scope="personal" if args.personal else "project",
        project=None if args.personal else _project_name(args, config),
        decision=args.decision,
        why=args.why,
        changes=args.changes,
        status=args.status,
        authority=authority,
        source=args.source,
        docs_updated=args.docs_updated,
        supersedes=args.supersedes,
    )
    note_path = write_discussion_note(notes_dir, note, args.title)
    result: dict[str, Any] = {"status": "written", "path": str(note_path)}
    if args.ingest:
        result["ingest"] = _ingest_paths(endpoint, [note_path], 1, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    endpoint = _resolve_endpoint(args)
    note_text = args.note.read_text(encoding="utf-8")
    metadata, _ = parse_front_matter(note_text)
    docs_updated = args.docs_updated or metadata.get("docs_updated", [])
    updated = update_front_matter(
        note_text,
        {
            "status": "accepted",
            "authority": "confirmed",
            "promoted_at": now_iso(),
            "docs_updated": docs_updated,
        },
    )
    args.note.write_text(updated, encoding="utf-8")
    result: dict[str, Any] = {"status": "promoted", "path": str(args.note)}
    if args.ingest:
        result["ingest"] = _ingest_paths(endpoint, [args.note], 1, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    config = _load_config(args)
    endpoint = _resolve_endpoint(args, config)
    paths = args.paths or _default_reindex_paths(endpoint)
    if not paths:
        raise SystemExit("no paths provided and no notes/canonical dirs configured")
    result = _ingest_paths(
        endpoint,
        paths,
        args.batch_size,
        args.timeout,
        args.exclude,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _add_endpoint_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="auto")
    parser.add_argument("--personal", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-env")
    parser.add_argument("--timeout", type=float, default=120.0)


def _resolve_endpoint(
    args: argparse.Namespace, config: MemoryConfig | None = None
) -> MemoryEndpoint:
    if args.base_url:
        return MemoryEndpoint(
            name="manual",
            base_url=args.base_url.rstrip("/"),
            api_key=args.api_key,
            api_key_env=args.api_key_env,
        )
    config = config or _load_config(args)
    try:
        endpoint = config.endpoint_for(
            None if args.project == "auto" else args.project,
            personal=args.personal,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not endpoint.base_url:
        raise SystemExit(f"base_url is not configured for memory endpoint {endpoint.name}")
    return endpoint


def _load_config(args: argparse.Namespace) -> MemoryConfig:
    return load_memory_config(args.config)


def _project_name(args: argparse.Namespace, config: MemoryConfig) -> str | None:
    return config.default_project if args.project == "auto" else args.project


def _default_notes_dir(args: argparse.Namespace, config: MemoryConfig) -> Path:
    if args.personal:
        return Path.home() / ".codex" / "memory" / "personal" / "discussions"
    project_name = _project_name(args, config) or "project"
    return config.project_root / "_inbox" / "memory-candidates" / project_name


def _default_reindex_paths(endpoint: MemoryEndpoint) -> list[Path]:
    paths = []
    if endpoint.notes_dir:
        paths.append(endpoint.notes_dir)
    paths.extend(endpoint.canonical_dirs)
    return paths


def _ingest_paths(
    endpoint: MemoryEndpoint,
    paths: list[Path],
    batch_size: int,
    timeout: float,
    extra_exclude_globs: list[str] | None = None,
) -> dict[str, Any]:
    exclude_globs = [*endpoint.exclude_globs, *(extra_exclude_globs or [])]
    documents = list(_iter_documents(paths, exclude_globs, endpoint.project_root))
    batches = []
    for index in range(0, len(documents), batch_size):
        batch = documents[index : index + batch_size]
        payload = {
            "texts": [content for _, content in batch],
            "file_sources": [str(path) for path, _ in batch],
        }
        batches.append(_post_json(endpoint, "/documents/texts", payload, timeout))
    return {"documents": len(documents), "batches": batches}


def _iter_documents(
    paths: list[Path],
    exclude_globs: list[str] | None = None,
    project_root: Path | None = None,
) -> list[tuple[Path, str]]:
    documents: list[tuple[Path, str]] = []
    exclude_globs = exclude_globs or []
    for path in paths:
        if path.is_dir():
            candidates = sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in TEXT_EXTENSIONS
            )
        else:
            candidates = [path]
        for candidate in candidates:
            if candidate.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            if _is_excluded(candidate, exclude_globs, project_root):
                continue
            documents.append((candidate, candidate.read_text(encoding="utf-8")))
    return documents


def _is_excluded(
    path: Path, exclude_globs: list[str], project_root: Path | None = None
) -> bool:
    if not exclude_globs:
        return False

    absolute = path.resolve().as_posix()
    relative_paths: list[str] = []
    if project_root is not None:
        try:
            relative_paths.append(path.resolve().relative_to(project_root.resolve()).as_posix())
        except ValueError:
            pass
    try:
        relative_paths.append(path.resolve().relative_to(Path.cwd().resolve()).as_posix())
    except ValueError:
        pass

    for pattern in exclude_globs:
        normalized = pattern.replace(os.sep, "/")
        if normalized.startswith("/"):
            if fnmatch.fnmatch(absolute, normalized):
                return True
            continue
        for relative in relative_paths:
            if fnmatch.fnmatch(relative, normalized):
                return True
        if fnmatch.fnmatch(absolute, f"**/{normalized}"):
            return True
        if fnmatch.fnmatch(absolute, normalized):
            return True
    return False


def _post_json(
    endpoint: MemoryEndpoint, route: str, payload: dict[str, Any], timeout: float
) -> Any:
    api_key = endpoint.resolved_api_key()
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = request.Request(endpoint.base_url + route, data=body, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"LightRAG request failed: {exc.code} {details}") from exc
    except TimeoutError as exc:
        raise SystemExit(f"LightRAG request timed out after {timeout:g}s") from exc
    except error.URLError as exc:
        raise SystemExit(f"LightRAG request failed: {exc.reason}") from exc
    return json.loads(response_body)


def _extract_query_text(response: Any) -> str:
    if isinstance(response, dict):
        if isinstance(response.get("response"), str):
            return response["response"]
        llm_response = response.get("llm_response")
        if isinstance(llm_response, dict) and isinstance(llm_response.get("content"), str):
            return llm_response["content"]
    return json.dumps(response, ensure_ascii=False, indent=2)


def _selected_database_specs(target: str) -> list[DatabaseSpec]:
    specs = []
    if target in {"all", "personal"}:
        specs.append(
            DatabaseSpec(
                database=_env("MEMORY_PERSONAL_DATABASE", "rag_personal"),
                user=_env("MEMORY_PERSONAL_USER", "rag_personal"),
                password=_required_env("MEMORY_PERSONAL_PASSWORD"),
            )
        )
    if target in {"all", "limbal"}:
        specs.append(
            DatabaseSpec(
                database=_env("MEMORY_LIMBAL_DATABASE", "rag_project_limbal"),
                user=_env("MEMORY_LIMBAL_USER", "rag_project_limbal"),
                password=_required_env("MEMORY_LIMBAL_PASSWORD"),
            )
        )
    return specs


async def _bootstrap_databases(
    args: argparse.Namespace, specs: list[DatabaseSpec]
) -> None:
    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise SystemExit("bootstrap-db requires asyncpg; install offline-storage extras") from exc

    admin = await _connect_with_retry(
        asyncpg,
        args,
        host=args.host,
        port=args.port,
        user=args.admin_user,
        password=args.admin_password,
        database=args.admin_database,
    )
    try:
        for spec in specs:
            _validate_identifier(spec.database)
            _validate_identifier(spec.user)
            await _ensure_role(admin, spec)
            await _ensure_database(admin, spec)
            await admin.execute(
                f"ALTER DATABASE {_quote_ident(spec.database)} "
                f"SET session_preload_libraries = {_quote_literal('age')}"
            )
    finally:
        await admin.close()

    for spec in specs:
        target = await _connect_with_retry(
            asyncpg,
            args,
            host=args.host,
            port=args.port,
            user=args.admin_user,
            password=args.admin_password,
            database=spec.database,
        )
        try:
            await target.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await target.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE")
            await target.execute(f"GRANT ALL ON SCHEMA public TO {_quote_ident(spec.user)}")
            await target.execute(
                f"GRANT USAGE ON SCHEMA ag_catalog TO {_quote_ident(spec.user)}"
            )
            await target.execute(
                f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ag_catalog "
                f"TO {_quote_ident(spec.user)}"
            )
        finally:
            await target.close()


async def _connect_with_retry(
    asyncpg_module: Any, args: argparse.Namespace, **kwargs: Any
) -> Any:
    last_error: Exception | None = None
    for attempt in range(args.connect_retries):
        try:
            return await asyncpg_module.connect(**kwargs)
        except Exception as exc:  # pragma: no cover - integration behavior
            last_error = exc
            if attempt + 1 >= args.connect_retries:
                break
            await asyncio.sleep(args.connect_delay)
    raise last_error or RuntimeError("PostgreSQL connection failed")


async def _ensure_role(conn: Any, spec: DatabaseSpec) -> None:
    role_exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", spec.user)
    quoted_user = _quote_ident(spec.user)
    quoted_password = _quote_literal(spec.password)
    if role_exists:
        await conn.execute(f"ALTER ROLE {quoted_user} LOGIN PASSWORD {quoted_password}")
    else:
        await conn.execute(f"CREATE ROLE {quoted_user} LOGIN PASSWORD {quoted_password}")


async def _ensure_database(conn: Any, spec: DatabaseSpec) -> None:
    database_exists = await conn.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", spec.database
    )
    if not database_exists:
        await conn.execute(
            f"CREATE DATABASE {_quote_ident(spec.database)} OWNER {_quote_ident(spec.user)}"
        )
    await conn.execute(
        f"GRANT ALL PRIVILEGES ON DATABASE {_quote_ident(spec.database)} "
        f"TO {_quote_ident(spec.user)}"
    )


def _validate_identifier(value: str) -> None:
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"unsafe PostgreSQL identifier: {value!r}")


def _quote_ident(value: str) -> str:
    _validate_identifier(value)
    return f'"{value}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
