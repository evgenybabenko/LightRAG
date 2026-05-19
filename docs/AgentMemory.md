# Agent Decision Memory

This document describes the local Codex decision-memory workflow backed by
LightRAG. It is intentionally separate from the regular API server deployment:
memory uses a dedicated Compose stack and separate PostgreSQL databases.

## Topology

The v1 stack uses one PostgreSQL container and multiple LightRAG API instances:

- `lightrag-postgres`: shared PostgreSQL runtime.
- `rag_personal`: private personal memory database.
- `rag_project_limbal`: shared Limbal project memory database.
- `lightrag-personal`: API on `9631`.
- `lightrag-project-limbal`: API on `9632`.
- `lightrag-memory-bootstrap`: one-shot database/user/extension bootstrap job.

Project boundaries are PostgreSQL databases plus API instances. Do not use
`WORKSPACE` as the project boundary for this workflow; each instance keeps
`WORKSPACE=default`.

## Setup

Create local env files from the templates:

```bash
cp .env.memory.example .env.memory
cp .env.memory.personal.example .env.memory.personal
cp .env.memory.limbal.example .env.memory.limbal
```

Generate bcrypt account values with:

```bash
lightrag-hash-password --username personal
lightrag-hash-password --username limbal
```

Start the stack:

```bash
docker compose --env-file .env.memory -f docker-compose.memory.yml up -d --build
```

The bootstrap job is idempotent. It creates the configured roles/databases,
installs `vector` and `age` in each memory database, grants `ag_catalog` access,
and sets `session_preload_libraries=age` for each memory database. The preload
keeps API database users isolated without making them PostgreSQL superusers.

## Authentication

LightRAG already has two useful auth layers for this workflow:

- Web UI login is configured with `AUTH_ACCOUNTS` and `TOKEN_SECRET`. When
  accounts are configured, `TOKEN_SECRET` must be a real non-default secret.
- Agent/CLI calls use `LIGHTRAG_API_KEY`; `lightrag-memory` sends it as the
  `X-API-Key` header through the endpoint `api_key_env` setting.

Use different `AUTH_ACCOUNTS`, `TOKEN_SECRET`, and `LIGHTRAG_API_KEY` values for
personal and Limbal instances. Docker Compose v2 may interpolate bcrypt dollar
signs in env files; if that happens, escape `$` as `$$`. Synology
`docker-compose` v1 passes env files literally in this deployment, so the NAS
env files use raw bcrypt strings.

## Synology Deployment

Synology is the preferred long-running host for this memory stack. Use the same
Compose file in Container Manager or over SSH with Docker Compose.

Recommended Synology settings:

- Keep `MEMORY_POSTGRES_BIND=127.0.0.1`; the database should not be exposed to
  the LAN.
- Set `MEMORY_PERSONAL_BIND=0.0.0.0` and `MEMORY_LIMBAL_BIND=0.0.0.0` only if
  the APIs are protected by VPN, firewall rules, or a reverse proxy.
- Point local Codex configs at the NAS, for example
  `http://synology-host:9631` and `http://synology-host:9632`.
- Public access is handled by the shared Synology CloudPub edge stack described
  in `docs/SynologyEdgeCloudPub.md`. The LightRAG memory stack does not run its
  own CloudPub container.
- Replace `host.docker.internal` LLM endpoints with an address reachable from
  containers on Synology, or keep the provided `extra_hosts` mapping if the
  model gateway runs on the same host.
- Back up the `lightrag_memory_postgres` volume. The API storage volumes are
  useful caches, but PostgreSQL is the source of truth for this workflow.

For Synology's older `docker-compose` v1, use
`docker-compose.memory.synology.yml`. It expects prebuilt images through
`LIGHTRAG_MEMORY_IMAGE` and `LIGHTRAG_POSTGRES_IMAGE`, so the NAS deployment
directory only needs env files, compose YAML, and optional transient image tar
files during image transfer. It does not need a full source repository checkout.

The current NAS layout is `/volume1/docker/lightrag/`.

Recommended first start sequence on Synology:

```bash
/usr/local/bin/docker load -i lightrag-postgres-age.tar
/usr/local/bin/docker load -i lightrag-memory.tar
export COMPOSE_HTTP_TIMEOUT=300
/usr/local/bin/docker-compose --env-file .env.memory \
  -f docker-compose.memory.synology.yml up -d lightrag-postgres
/usr/local/bin/docker-compose --env-file .env.memory \
  -f docker-compose.memory.synology.yml run --rm lightrag-memory-bootstrap
/usr/local/bin/docker-compose --env-file .env.memory \
  -f docker-compose.memory.synology.yml up -d lightrag-personal lightrag-project-limbal
```

## CLI

The `lightrag-memory` CLI provides the agent-facing workflow:

```bash
lightrag-memory bootstrap-db
lightrag-memory query --project limbal "what did we decide about auth?"
lightrag-memory query --project limbal --timeout 15 \
  "what is the source-of-truth order?"
lightrag-memory ingest --project limbal openspec AGENTS.md README.md \
  --exclude "openspec/changes/archive/**"
lightrag-memory capture-discussion --project limbal \
  --title "API key rotation policy" \
  --decision "..." \
  --status accepted \
  --ingest
lightrag-memory promote path/to/note.md --project limbal --ingest
lightrag-memory reindex --project limbal --timeout 120
```

Configuration is discovered from `.codex-memory.toml` in the current directory
or any parent directory. If no project config exists, the CLI falls back to
`~/.codex/memory/config.toml`.

Project endpoints can declare curated reindex sources and exclusions:

```toml
[projects.limbal]
base_url = "http://127.0.0.1:9632"
api_key_env = "LIGHTRAG_LIMBAL_API_KEY"
notes_dir = "_inbox/memory-candidates"
canonical_dirs = [
  "README.md",
  "repos/main-app/AGENTS.md",
  "repos/main-app/README.md",
  "repos/main-app/openspec/specs",
  "repos/main-app/openspec/changes",
  "repos/main-app/wiki",
]
exclude_globs = [
  "repos/main-app/openspec/changes/archive/**",
  "repos/main-app/docs/research/forks/**/reports/**",
]
```

`reindex` ingests `notes_dir` first, then `canonical_dirs`. Use
`exclude_globs` for generated or historical files that are useful in git but
too noisy or stale for default retrieval. Extra `--exclude` values are merged
with the endpoint configuration for one-off runs. `--timeout` controls each
HTTP request to LightRAG; short query timeouts keep agent workflows from
blocking when an endpoint is unhealthy, while reindex jobs can use a larger
timeout.

## Skill Flow

Memory is read by skills, not on every ordinary request. Skills should trigger
when the request is about decisions, architecture, project conventions, risks,
tests, or avoiding regressions.

Read flow:

1. Determine the project from the current directory and `.codex-memory.toml`.
2. Read local source-of-truth files first: `AGENTS.md`, README, OpenSpec/docs.
3. Read a project wiki index when one exists; treat it as derived orientation.
4. Query personal memory and project memory for a short context pack.
5. Cross-check the returned context against actual code with `rg` and file reads.

Capture flow:

1. If the discussion changes a feature, rule, architecture, API, deployment
   model, or testing expectation, the agent proposes a concise memory note.
2. If documentation conflicts with the new decision, the agent asks whether to
   update the docs/specs immediately.
3. After confirmation, update docs/specs in the same task and create an accepted
   discussion note.
4. Without confirmation, store only a `candidate` note; candidate notes do not
   override documentation.

## Source Priority

Use this priority order:

1. Current user request.
2. Current code and local project files.
3. Confirmed fresh discussion decisions.
4. Project docs/specs/AGENTS/OpenSpec.
5. Accepted project memory.
6. Derived wiki pages and other compiled summaries.
7. Personal memory.
8. General model knowledge.

On conflict, the agent must show the conflict explicitly and ask whether to
update documentation before making the new discussion authoritative.

## Note Format

Decision notes are Markdown files with simple front matter:

```markdown
---
scope: "project"
project: "limbal"
type: "discussion-decision"
status: "accepted"
authority: "confirmed"
created_at: "2026-04-24T00:00:00+03:00"
source: "codex-thread"
docs_updated:
  - "openspec/specs/example/spec.md"
---

# Short Title

## Решение

...
```

Use `status: candidate` until the user confirms the decision and any required
documentation changes.
