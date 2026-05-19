"""Configuration discovery for Codex decision-memory tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


CONFIG_FILE_NAME = ".codex-memory.toml"
DEFAULT_GLOBAL_CONFIG = Path.home() / ".codex" / "memory" / "config.toml"


@dataclass
class MemoryEndpoint:
    name: str
    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    notes_dir: Path | None = None
    canonical_dirs: list[Path] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    project_root: Path | None = None

    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env)
        return None


@dataclass
class MemoryConfig:
    path: Path | None
    project_root: Path
    default_project: str | None
    personal: MemoryEndpoint | None
    projects: dict[str, MemoryEndpoint]

    def endpoint_for(self, project: str | None, personal: bool = False) -> MemoryEndpoint:
        if personal:
            if self.personal is None:
                raise ValueError("personal memory endpoint is not configured")
            return self.personal
        project_name = project or self.default_project
        if not project_name:
            raise ValueError("project is required when default_project is not set")
        try:
            return self.projects[project_name]
        except KeyError as exc:
            raise ValueError(f"memory project '{project_name}' is not configured") from exc


def load_memory_config(path: Path | None = None, start: Path | None = None) -> MemoryConfig:
    config_path = path or find_memory_config(start or Path.cwd()) or DEFAULT_GLOBAL_CONFIG
    project_root = config_path.parent if config_path.exists() else (start or Path.cwd())
    data = _read_toml(config_path) if config_path.exists() else {}

    default_project = data.get("default_project")
    personal = None
    if isinstance(data.get("personal"), dict):
        personal = _endpoint_from_mapping("personal", data["personal"], project_root)

    projects: dict[str, MemoryEndpoint] = {}
    for project_name, project_data in data.get("projects", {}).items():
        if isinstance(project_data, dict):
            projects[project_name] = _endpoint_from_mapping(
                project_name, project_data, project_root
            )

    return MemoryConfig(
        path=config_path if config_path.exists() else None,
        project_root=project_root,
        default_project=default_project,
        personal=personal,
        projects=projects,
    )


def find_memory_config(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate_dir in [current, *current.parents]:
        candidate = candidate_dir / CONFIG_FILE_NAME
        if candidate.exists():
            return candidate
    return None


def expand_path(raw_path: str | None, base_dir: Path) -> Path | None:
    if not raw_path:
        return None
    expanded = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if expanded.is_absolute():
        return expanded
    return base_dir / expanded


def _endpoint_from_mapping(
    name: str, data: dict[str, Any], project_root: Path
) -> MemoryEndpoint:
    canonical_dirs = [
        path
        for raw_path in data.get("canonical_dirs", [])
        if (path := expand_path(str(raw_path), project_root)) is not None
    ]
    exclude_globs = [str(raw_glob) for raw_glob in data.get("exclude_globs", [])]
    notes_dir = expand_path(data.get("notes_dir"), project_root)
    return MemoryEndpoint(
        name=name,
        base_url=str(data.get("base_url", "")).rstrip("/"),
        api_key=data.get("api_key"),
        api_key_env=data.get("api_key_env"),
        notes_dir=notes_dir,
        canonical_dirs=canonical_dirs,
        exclude_globs=exclude_globs,
        project_root=project_root,
    )


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as config_file:
        return tomllib.load(config_file)
