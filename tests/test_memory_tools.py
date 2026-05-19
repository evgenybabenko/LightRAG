from pathlib import Path

import pytest

from lightrag.tools.memory_config import find_memory_config, load_memory_config
from lightrag.tools.memory_cli import _iter_documents, _quote_ident, _selected_database_specs
from lightrag.tools.memory_notes import (
    build_discussion_note,
    note_filename,
    parse_front_matter,
    update_front_matter,
)

pytestmark = pytest.mark.offline


def test_build_discussion_note_contains_metadata_and_sections() -> None:
    note = build_discussion_note(
        title="API Auth Decision",
        scope="project",
        project="limbal",
        decision="Use shared project API keys for v1.",
        why="Current LightRAG auth has no project RBAC.",
        changes="Document the shared-key workflow.",
        status="accepted",
        authority="confirmed",
        created_at="2026-04-24T00:00:00+03:00",
        docs_updated=["openspec/specs/auth/spec.md"],
        supersedes=["20260420-old-auth-policy.md"],
    )

    metadata, body = parse_front_matter(note)

    assert metadata["scope"] == "project"
    assert metadata["project"] == "limbal"
    assert metadata["status"] == "accepted"
    assert metadata["docs_updated"] == ["openspec/specs/auth/spec.md"]
    assert metadata["supersedes"] == ["20260420-old-auth-policy.md"]
    assert "## Решение" in body
    assert "Use shared project API keys for v1." in body


def test_update_front_matter_promotes_candidate() -> None:
    note = build_discussion_note(
        title="Candidate",
        scope="personal",
        project=None,
        decision="Prefer explicit context packs.",
        status="candidate",
        created_at="2026-04-24T00:00:00+03:00",
    )

    updated = update_front_matter(
        note, {"status": "accepted", "authority": "confirmed"}
    )
    metadata, _ = parse_front_matter(updated)

    assert metadata["status"] == "accepted"
    assert metadata["authority"] == "confirmed"


def test_note_filename_uses_date_and_slug() -> None:
    assert (
        note_filename("API Auth Decision", "2026-04-24T00:00:00+03:00")
        == "20260424-api-auth-decision.md"
    )


def test_load_memory_config_discovers_project_config(tmp_path: Path) -> None:
    root = tmp_path / "project"
    nested = root / "repos" / "main-app"
    nested.mkdir(parents=True)
    (root / ".codex-memory.toml").write_text(
        """
default_project = "limbal"

[personal]
base_url = "http://127.0.0.1:9631"
api_key_env = "LIGHTRAG_PERSONAL_API_KEY"
notes_dir = "~/.codex/memory/personal/discussions"

[projects.limbal]
base_url = "http://127.0.0.1:9632"
api_key_env = "LIGHTRAG_LIMBAL_API_KEY"
notes_dir = "_inbox/memory-candidates"
canonical_dirs = ["repos/main-app/openspec", "repos/main-app/AGENTS.md"]
exclude_globs = ["repos/main-app/openspec/changes/archive/**"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config_path = find_memory_config(nested)
    config = load_memory_config(config_path)
    endpoint = config.endpoint_for("limbal")

    assert config.default_project == "limbal"
    assert endpoint.base_url == "http://127.0.0.1:9632"
    assert endpoint.notes_dir == root / "_inbox" / "memory-candidates"
    assert root / "repos" / "main-app" / "openspec" in endpoint.canonical_dirs
    assert endpoint.exclude_globs == ["repos/main-app/openspec/changes/archive/**"]
    assert endpoint.project_root == root


def test_iter_documents_skips_configured_exclude_globs(tmp_path: Path) -> None:
    root = tmp_path / "project"
    current = root / "repos" / "main-app" / "openspec" / "specs" / "api"
    archived = (
        root
        / "repos"
        / "main-app"
        / "openspec"
        / "changes"
        / "archive"
        / "2026-04-01-old-change"
    )
    current.mkdir(parents=True)
    archived.mkdir(parents=True)
    (current / "spec.md").write_text("current", encoding="utf-8")
    (archived / "proposal.md").write_text("archived", encoding="utf-8")

    documents = _iter_documents(
        [root / "repos" / "main-app" / "openspec"],
        ["repos/main-app/openspec/changes/archive/**"],
        root,
    )

    assert [path.name for path, _ in documents] == ["spec.md"]

    suffix_documents = _iter_documents(
        [root / "repos" / "main-app" / "openspec"],
        ["openspec/changes/archive/**"],
        root,
    )

    assert [path.name for path, _ in suffix_documents] == ["spec.md"]


def test_postgres_identifier_quoting_rejects_unsafe_values() -> None:
    assert _quote_ident("rag_project_limbal") == '"rag_project_limbal"'

    with pytest.raises(ValueError):
        _quote_ident("rag-project-limbal")


def test_selected_database_specs_require_passwords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_PERSONAL_PASSWORD", raising=False)

    with pytest.raises(SystemExit):
        _selected_database_specs("personal")

    monkeypatch.setenv("MEMORY_PERSONAL_PASSWORD", "secret")
    specs = _selected_database_specs("personal")

    assert specs[0].database == "rag_personal"
    assert specs[0].user == "rag_personal"
    assert specs[0].password == "secret"
