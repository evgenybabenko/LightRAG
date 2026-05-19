"""Helpers for Codex decision-memory note files."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import re
import unicodedata
from typing import Any


FRONT_MATTER_DELIMITER = "---"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "memory-note") -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
    return slug or fallback


def format_front_matter(metadata: dict[str, Any]) -> str:
    lines = [FRONT_MATTER_DELIMITER]
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_format_scalar(item)}")
        else:
            lines.append(f"{key}: {_format_scalar(value)}")
    lines.append(FRONT_MATTER_DELIMITER)
    return "\n".join(lines)


def build_discussion_note(
    *,
    title: str,
    scope: str,
    project: str | None,
    decision: str,
    why: str = "",
    changes: str = "",
    status: str = "candidate",
    authority: str = "candidate",
    source: str = "codex-thread",
    created_at: str | None = None,
    docs_updated: list[str] | None = None,
    supersedes: list[str] | None = None,
    superseded_by: str | None = None,
) -> str:
    metadata = {
        "scope": scope,
        "project": project,
        "type": "discussion-decision",
        "status": status,
        "authority": authority,
        "created_at": created_at or now_iso(),
        "source": source,
        "docs_updated": docs_updated or [],
        "supersedes": supersedes or [],
        "superseded_by": superseded_by,
    }
    sections = [
        f"# {title.strip()}",
        "## Решение",
        decision.strip(),
        "## Почему",
        why.strip() or "Не указано.",
        "## Что Меняется",
        changes.strip() or "Не указано.",
        "## Обновленная Документация",
        _format_docs_section(docs_updated),
    ]
    return f"{format_front_matter(metadata)}\n\n" + "\n\n".join(sections) + "\n"


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        return {}, text

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_DELIMITER:
            end_index = index
            break

    if end_index is None:
        return {}, text

    metadata = _parse_simple_yaml(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :]).lstrip("\n")
    return metadata, body


def update_front_matter(text: str, updates: dict[str, Any]) -> str:
    metadata, body = parse_front_matter(text)
    metadata.update(updates)
    return f"{format_front_matter(metadata)}\n\n{body.rstrip()}\n"


def note_filename(title: str, created_at: str | None = None) -> str:
    timestamp = created_at or now_iso()
    date_part = re.sub(r"[^0-9]", "", timestamp[:10]) or "undated"
    return f"{date_part}-{slugify(title)}.md"


def write_discussion_note(notes_dir: Path, note_text: str, title: str) -> Path:
    notes_dir.mkdir(parents=True, exist_ok=True)
    metadata, _ = parse_front_matter(note_text)
    created_at = str(metadata.get("created_at") or now_iso())
    path = notes_dir / note_filename(title, created_at)
    path.write_text(note_text, encoding="utf-8")
    return path


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _parse_simple_yaml(lines: list[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("  - ") and current_list_key:
            metadata.setdefault(current_list_key, []).append(_parse_scalar(line[4:]))
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not raw_value:
            metadata[key] = []
            current_list_key = key
        else:
            metadata[key] = _parse_scalar(raw_value)
            current_list_key = None
    return metadata


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
    return value


def _format_docs_section(docs_updated: list[str] | None) -> str:
    if not docs_updated:
        return "Пока не обновлялась."
    return "\n".join(f"- `{path}`" for path in docs_updated)
