# ScarTissue

> Every codebase remembers its bugs.

ScarTissue is a PR review agent that warns when a pull request is about to reintroduce a bug pattern that the repo already fixed in its history. It mines a project's git log for fix/bugfix/revert commits, embeds those incidents into a vector store, and at PR time runs an agentic Claude loop that retrieves the most analogous prior incidents and emits inline GitHub comments (or an email) when the new diff "rhymes" with an old fix.

---

## Table of contents

1. [Why this exists](#why-this-exists)
2. [System architecture](#system-architecture)
3. [End-to-end flow diagrams](#end-to-end-flow-diagrams)
4. [Component reference](#component-reference)
5. [Technology stack](#technology-stack)
6. [Architectural decisions](#architectural-decisions)
7. [API surface](#api-surface)
8. [MCP server](#mcp-server)
9. [Quickstart](#quickstart)
10. [Environment variables](#environment-variables)
11. [Repo layout](#repo-layout)

---

## Why this exists

Static analyzers and LLM reviewers are great at catching *generic* mistakes, but they have no memory. They will let a PR reintroduce the exact race condition that took down production six months ago because the fix is invisible to anyone who wasn't there.

ScarTissue treats the git log as the source of truth for what a codebase has *learned the hard way*. Every `fix:` / `bugfix` / `revert` commit is treated as an "incident" — a structured record of a bug that was painful enough to fix. New PRs get reviewed against that history.

---

## System architecture

ScarTissue has three execution surfaces sitting on top of one shared review pipeline:

```
                           ┌──────────────────────────────────────────────────┐
                           │                  Surfaces                        │
                           ├────────────┬─────────────────┬───────────────────┤
                           │  Next.js   │  MCP server     │  REST clients     │
                           │  web app   │  (stdio JSON-   │  (curl, Claude    │
                           │  (UI)      │   RPC, Claude   │   API consumers,  │
                           │            │   Desktop, etc) │   GitHub Action)  │
                           └─────┬──────┴────────┬────────┴─────────┬─────────┘
                                 │               │                  │
                                 │   HTTP/JSON   │  stdio JSON-RPC  │ HTTP/JSON
                                 ▼               ▼                  ▼
                           ┌──────────────────────────────────────────────────┐
                           │           FastAPI app (backend/app)              │
                           │  /index   /review   /post-to-github              │
                           │  /email-review   /repos   /health                │
                           └────────────────────────┬─────────────────────────┘
                                                    │
                                                    ▼
                           ┌──────────────────────────────────────────────────┐
                           │              Core review pipeline                │
                           │                                                  │
                           │  PRFetcher ──► Reviewer (Claude agent loop) ──►  │
                           │                  │                               │
                           │                  ├── search_scar_tissue          │
                           │                  ├── get_incident_details        │
                           │                  ├── search_current_code         │
                           │                  └── emit_warning                │
                           └────────┬───────────────┬─────────────────┬───────┘
                                    │               │                 │
                                    ▼               ▼                 ▼
                            ┌──────────────┐ ┌────────────┐ ┌────────────────┐
                            │  ScarIndex   │ │ NiaClient  │ │  Anthropic API │
                            │  (ChromaDB + │ │ (live code │ │ Claude Sonnet  │
                            │  MiniLM-L6)  │ │  search)   │ │ 4.6 (tool use) │
                            └──────┬───────┘ └─────┬──────┘ └────────────────┘
                                   │               │
                                   ▼               ▼
                            ┌──────────────┐ ┌──────────────┐
                            │  GitMiner    │ │  trynia.ai   │
                            │  (clones +   │ │  v2 API      │
                            │  walks       │ └──────────────┘
                            │  history)    │
                            └──────┬───────┘
                                   │
                                   ▼
                            ┌──────────────┐    ┌──────────────────┐
                            │  GitHub.git  │    │  GitHub REST     │
                            │  (clone)     │    │  + AgentMail     │
                            └──────────────┘    │  (notifications) │
                                                └──────────────────┘
```

There are **two distinct write paths** into the system:

- **Indexing path** — `POST /index` → `GitMiner.mine()` → `ScarIndex.index_incidents()` → ChromaDB. Slow (minutes), per-repo, infrequent.
- **Review path** — `POST /review` → `PRFetcher.fetch()` → `Reviewer.review()` → Claude agent loop with tool calls back into the index. Fast, per-PR, high-volume.

Indexing is decoupled from review so that PRs against a long-indexed repo cost only an LLM round trip plus a vector lookup, not a full clone.

---

## End-to-end flow diagrams

### 1. Indexing a repo

```
POST /index { repo: "owner/name" }
        │
        ▼
┌────────────────┐
│   GitMiner     │   shallow clone (depth=5000) into /tmp/scartissue/
│   .mine()      │   walk commits, regex-match fix/bugfix/revert/closes #N
│                │   skip merge commits, oversized diffs (> 500 changed lines)
│                │   capture: sha, message, date, author, files,
│                │            full unified diff (truncated to 8KB),
│                │            buggy_parent_sha, issue_refs
└────────┬───────┘
         │  list[Incident]
         ▼
┌────────────────┐
│   ScarIndex    │   for each incident:
│   .index_      │     text = "Commit msg: … Files: … Fix diff: …"
│   incidents()  │     embedding = SentenceTransformer(MiniLM-L6).encode(text)
│                │   batch upsert into ChromaDB collection (per-repo)
│                │   collection metadata = { hnsw:space: "cosine",
│                │                            last_indexed: <iso ts> }
└────────┬───────┘
         │
         ▼
   ChromaDB (./chroma_db/)
```

### 2. Reviewing a PR

```
POST /review { pr_url }
        │
        ▼
┌────────────────┐
│   PRFetcher    │   parse owner/repo + PR number
│   .fetch()     │   PyGithub: title, author, base/head sha
│                │   GitHub REST (vnd.github.v3.diff): unified diff
│                │   unidiff: parse into Hunk[] (file, header, body)
│                │   ── if repo is a fork, resolve upstream root and
│                │      route scar-tissue queries to upstream's index
└────────┬───────┘
         │  PRDiff
         ▼
┌──────────────────────────────────────────────────────────────┐
│                       Reviewer.review()                       │
│                                                               │
│   build user message with all hunks                           │
│   loop up to MAX_ITERATIONS (20):                             │
│       claude.messages.create(                                 │
│           model = claude-sonnet-4-6,                          │
│           system = ScarTissue agent prompt,                   │
│           tools = [search_scar_tissue,                        │
│                    get_incident_details,                      │
│                    search_current_code,                       │
│                    emit_warning],                             │
│           tool_choice = auto,                                 │
│           messages)                                           │
│       if stop_reason == "end_turn" and no tool_use: break     │
│       for each tool_use block:                                │
│           dispatch _exec_tool() ──┐                           │
│           if name == emit_warning:│ build Warning, dedup by   │
│             append to dedup       │ (file, hunk, matched sha) │
│       append tool_result turn ────┘                           │
└────────┬──────────────────────────────────────────────────────┘
         │  list[Warning], sorted by confidence desc
         ▼
   Response (or live progress + on_warning callbacks)
```

The agent loop is a conventional Anthropic tool-use loop: Claude proposes tool calls, the server executes them locally, and the results go back as `tool_result` blocks until the model signals `end_turn`. The four tools are described inline in `backend/app/services/reviewer.py:50-138`.

### 3. Posting results

```
   list[Warning]
        │
        ├──► POST /post-to-github
        │       parse @@ hunk header → anchor line (start + length/2)
        │       fetch PR file patches → set of valid right-side line numbers
        │       split warnings into:
        │           - inline review comments (anchor line is in the diff)
        │           - issue-thread fallback comments (anchor not in diff)
        │       create one PR review (event=COMMENT) with the inline batch
        │       fall back to issue comments for the rest
        │       map GitHub error codes → 403 / 404 / 429 with retry-after
        │
        └──► POST /email-review
                render text + HTML email (severity-coloured cards)
                if dry_run: return html_preview
                else: AgentMail
                       inboxes.create(client_id=scartissue-pr-bot)
                       inboxes.messages.send(to, subject, text, html)
```

---

## Component reference

| Component | File | Responsibility |
|---|---|---|
| **GitMiner** | `backend/app/services/git_miner.py` | Clone (or fetch) the target repo, walk history, regex-classify incident commits, attach unified diff bodies bounded by size and line count. |
| **ScarIndex** | `backend/app/services/scar_index.py` | Wrap a `chromadb.PersistentClient`. One collection per repo, cosine HNSW. Embeds `commit_message + files_changed + fix_diff` with `sentence-transformers/all-MiniLM-L6-v2`. Stores the full `Incident` JSON in metadata so retrieval is loss-less. |
| **PRFetcher** | `backend/app/services/pr_fetcher.py` | Hybrid PyGithub (metadata) + raw `vnd.github.v3.diff` (diff body) fetch. Parses the unified diff with `unidiff` into `Hunk[]`. Detects forks and resolves the upstream root for cross-fork scar lookup. |
| **NiaClient** | `backend/app/services/nia_client.py` | Async `httpx` client for `apigcp.trynia.ai/v2`. Provides live, semantic search over the *current* repo state — used as a confidence-modifier tool, not a primary source. |
| **Reviewer** | `backend/app/services/reviewer.py` | The agent loop. Owns the system prompt, tool schemas, dedup map, optional streaming callbacks (`on_warning`, `on_progress`). |
| **FastAPI routes** | `backend/app/routes/*.py` | Thin adapters. `index.py`, `review.py`, `repos.py`, `github_post.py`, `email_review.py`. |
| **Pydantic schemas** | `backend/app/models/schemas.py` | `Incident`, `Warning`, request/response envelopes. Includes a `_sanitize_pr_url` validator that strips zero-width / bidi / NBSP characters that browsers paste into URLs. |
| **MCP server** | `backend/mcp_server/{server,tools}.py` | Stdio JSON-RPC server exposing `scartissue_index_repo`, `scartissue_review_pr`, `scartissue_search_scar_tissue`, `scartissue_list_indexed_repos` to MCP clients (Claude Desktop, Claude Code, etc.). Reuses the exact same service singletons as the HTTP API. |
| **Next.js frontend** | `frontend/app/` | App Router UI. Five proxy routes under `app/api/*` that simply forward JSON to the FastAPI backend (`BACKEND_URL`). Single-page `page.tsx` drives the index/review/post/email flows. |

---

## Technology stack

| Layer | Tech | Why |
|---|---|---|
| HTTP server | FastAPI 0.115+, uvicorn | First-class async, auto-generated OpenAPI, Pydantic-native. |
| Language runtime | Python 3.11+ | Required for `from __future__ import annotations`-free union syntax and modern typing. |
| LLM | Anthropic `claude-sonnet-4-6` via `AsyncAnthropic` | Tool-use loop, 4096 max_tokens, temperature 0.3, hard cap of 20 iterations. |
| Vector store | ChromaDB (persistent, cosine HNSW) | Embedded — no extra service to operate, on-disk persistence under `./chroma_db/`. |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` | 384-dim, runs locally on CPU, fast enough to embed thousands of commits in a single indexing pass. |
| Live code search | Nia API (`apigcp.trynia.ai/v2`) | Used by `search_current_code` to verify that an old protective pattern is still present. Soft-fails with a sentinel error when the repo is unindexed. |
| Git ops | GitPython + raw `git` subprocess | GitPython for traversal, raw subprocess for diffs (explicit `decode(errors='replace')` is more reliable than GitPython's heuristics on binary-ish blobs). |
| Diff parsing | `unidiff` | Hunk-level structure with rename/add/delete awareness. |
| GitHub integration | PyGithub + `httpx` | PyGithub for metadata + posting, `httpx` for raw `vnd.github.v3.diff`. |
| Email | AgentMail (`agentmail` Python SDK) | Per-review ephemeral inbox; supports HTML + plain-text bodies, message threading. |
| MCP | `mcp` Python SDK (stdio transport) | Exposes the same review pipeline to any MCP client. |
| Frontend | Next.js 15 (App Router) + React 19 + TypeScript 5 + Tailwind v4 | Single-page review console, BYO API keys flow, real-time progress UI. |
| Package management | `uv` (backend), `pnpm` (frontend) | Both significantly faster than pip / npm; `uv.lock` and `pnpm-lock.yaml` are committed. |

---

## Architectural decisions

These are the design calls that shape the codebase. Each one trades against an obvious alternative.

### 1. Git log as the corpus, not issue trackers

Bug trackers are noisy, lossy, and inconsistent across projects. Git history is universal and reliable: every project that has ever been fixed has `fix:`, `bugfix`, `Revert "…"`, or `Fixes #N` in its log. Mining the log gives a high-recall, low-cost corpus that needs no schema mapping per project. The downside — false positives from "fix typo" commits — is filtered by the LLM at review time rather than at index time.

### 2. Embed `(commit message + files + fix diff)`, not the diff alone

The bug pattern lives in the diff, but the *intent* lives in the message. Concatenating message + files + diff lets a query phrased as a description ("streaming response callback cleanup") match a fix that doesn't share any literal tokens with that description. Diff is truncated to 1500 chars per incident to stop a single huge refactor from dominating the embedding.

### 3. ChromaDB embedded, not a managed vector DB

A hackathon-scale tool should not require provisioning Pinecone/Weaviate/Qdrant. Chroma persists to a local directory, runs in-process, and the per-repo collection model maps perfectly to "one index per repo." If this graduated to multi-tenant SaaS, swapping in a hosted store would be a `ScarIndex` rewrite — every consumer goes through that one class.

### 4. Agent loop, not a single-shot prompt

A single-shot prompt with all hunks + all candidates inlined would be cheaper but bad at filtering. Most retrieved candidates *look* similar but aren't actual regressions. Letting Claude pull `get_incident_details` only on candidates it finds promising means it spends tokens reading full fix diffs *only when they matter*. The 20-iteration cap is a safety belt, not a target — typical PRs converge in 4–8 iterations.

### 5. Separate `search_scar_tissue` from `search_current_code`

Scar tissue is *historical* (what was fixed). Current code is *present* (whether the protection still exists). Conflating them confused the model in early iterations: it would emit warnings about long-removed code paths. Splitting the tools and writing the system prompt to call `search_current_code` only "when it would meaningfully change confidence" cut false positives sharply.

### 6. Fork resolution at fetch time

Many real PRs land on forks (`mycompany/langchain` ← `langchain-ai/langchain`). The fork has no scar index of its own; the upstream does. `PRFetcher` detects forks via `gh_repo.fork` and exposes `upstream_repo` on `PRDiff`; the `Reviewer` routes every scar-tissue lookup to `upstream_repo or repo`. This was added in the most recent commit (`feat/fork-resolution`) and is the reason scar-tissue works against PRs to forks of indexed repos.

### 7. Two output channels: GitHub PR comments and email

Inline PR review comments (`pr.create_review(event="COMMENT", comments=[…])`) are the high-signal channel — they appear next to the offending line. But not every warning anchors cleanly: the hunk header line may not be in the PR's `+`/` ` lines (e.g. context-only hunks). The route partitions warnings into anchored vs. unanchored and falls back to issue-thread comments for the latter. Email via AgentMail is a parallel channel for users who don't want write access to the repo.

### 8. Strip invisible characters from PR URLs

A surprising amount of breakage came from users pasting URLs out of Slack/Notion that contained zero-width spaces and bidi marks. The Pydantic validator on `pr_url` re-encodes through ASCII with `errors="ignore"` before parsing — cheap, and eliminates a category of "the URL works in my browser but your API rejects it" tickets.

### 9. MCP server reuses the same service singletons as HTTP

The MCP entrypoint (`mcp_server/server.py`) constructs `ScarIndex`, `NiaClient`, `Reviewer`, `PRFetcher`, `GitMiner` exactly once and passes them through `ToolServices`. There is no second code path. Anything fixed in the HTTP code is automatically fixed in the MCP code.

### 10. Frontend API routes are pure proxies

The Next.js app could call the FastAPI backend directly from the browser, but that would either expose the backend publicly or pin it to the same origin. Instead, every `/api/*` route in `frontend/app/api/*/route.ts` is a one-liner that forwards JSON to `BACKEND_URL`. This keeps secrets server-side, lets the backend stay on a private network, and means any backend response shape change is transparent to the frontend without a redeploy of the proxy.

---

## API surface

All routes return JSON.

| Method | Path | Body | Description |
|---|---|---|---|
| `POST` | `/index` | `{ repo, max_commits? }` | Mine git history and build the scar index for a repo. Returns `{ repo, incidents_found, duration_seconds, status }`. |
| `POST` | `/review` | `{ pr_url }` | Run the agent loop against a PR. Returns `{ pr_repo, upstream_repo?, pr_title, pr_author, warnings[], total_warnings, duration_seconds }`. |
| `POST` | `/post-to-github` | `{ pr_url, warnings[], dry_run? }` | Post warnings as inline PR review comments (with issue-comment fallback for un-anchorable hunks). Maps GitHub `403`/`404`/`429` cleanly. |
| `POST` | `/email-review` | `{ pr_url, pr_title, pr_repo, pr_author, recipient_email, warnings[], dry_run? }` | Send a styled HTML/plain-text review email via AgentMail. `dry_run=true` returns the rendered HTML for preview. |
| `GET` | `/repos` | — | List indexed repos with incident counts and `last_indexed` timestamp. |
| `GET` | `/health` | — | Liveness probe. |

OpenAPI/Swagger docs are auto-generated at `http://localhost:8000/docs`.

---

## MCP server

Run the same review pipeline as a Model Context Protocol server (stdio transport, JSON-RPC):

```bash
# from backend/, after `uv pip install -e .`
scartissue-mcp
```

Tools exposed:

- `scartissue_index_repo({ repo, max_commits? })`
- `scartissue_review_pr({ pr_url })`
- `scartissue_search_scar_tissue({ repo, query, top_k? })`
- `scartissue_list_indexed_repos({})`

Wire it into Claude Desktop / Claude Code by pointing the MCP client at the `scartissue-mcp` executable. The server redirects all incidental `print()` traffic to stderr so it doesn't corrupt the JSON-RPC stream on stdout.

---

## Quickstart

### Backend

```bash
cd backend

# with uv (recommended)
uv venv && source .venv/bin/activate
uv pip install -e .

# or with pip
pip install -e .

cp .env.example .env   # fill in your API keys
uvicorn app.main:app --reload
# → http://localhost:8000
```

### Frontend

```bash
cd frontend

# with pnpm (recommended)
pnpm install && pnpm dev

# or with npm
npm install && npm run dev
# → http://localhost:3000
```

The frontend talks to the backend via `BACKEND_URL` (defaults to `http://localhost:8000`).

### First run

```bash
# 1. Index a repo (runs once, ~minutes)
curl -X POST http://localhost:8000/index \
  -H 'Content-Type: application/json' \
  -d '{"repo": "langchain-ai/langchain", "max_commits": 3000}'

# 2. Review a PR
curl -X POST http://localhost:8000/review \
  -H 'Content-Type: application/json' \
  -d '{"pr_url": "https://github.com/langchain-ai/langchain/pull/35238"}'
```

---

## Environment variables

`backend/.env`:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Anthropic API key for the Claude agent loop. |
| `NIA_API_KEY` | optional | Nia bearer token. If unset, `search_current_code` gracefully no-ops. |
| `GITHUB_TOKEN` | yes | GitHub PAT — needed to fetch PR diffs and (with `repo` scope) to post review comments. |
| `CHROMA_PERSIST_DIR` | optional | ChromaDB on-disk path. Default `./chroma_db`. |
| `AGENTMAIL_API_KEY` | optional | Required only if you call `/email-review` without `dry_run`. |
| `AGENTMAIL_INBOX_CLIENT_ID` | optional | Stable inbox client id. Default `scartissue-pr-bot`. |

Frontend (optional): `BACKEND_URL` — defaults to `http://localhost:8000`.

---

## Repo layout

```
ScarTissueOpenClawHackathon/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI app + CORS + router wiring
│   │   ├── models/schemas.py      # Pydantic: Incident, Warning, requests, responses
│   │   ├── routes/
│   │   │   ├── index.py           # POST /index
│   │   │   ├── review.py          # POST /review
│   │   │   ├── repos.py           # GET  /repos
│   │   │   ├── github_post.py     # POST /post-to-github
│   │   │   └── email_review.py    # POST /email-review
│   │   └── services/
│   │       ├── git_miner.py       # Clone + walk + classify incidents
│   │       ├── scar_index.py      # ChromaDB wrapper + embeddings
│   │       ├── pr_fetcher.py      # PR diff fetch + hunk parse + fork detection
│   │       ├── nia_client.py      # Live code search client
│   │       └── reviewer.py        # Claude agent loop (the heart of the system)
│   ├── mcp_server/
│   │   ├── server.py              # stdio JSON-RPC entrypoint (`scartissue-mcp`)
│   │   └── tools.py               # MCP tool definitions + handlers
│   ├── chroma_db/                 # Persisted vector store (gitignored)
│   ├── pyproject.toml
│   └── .env.example
├── frontend/
│   ├── app/
│   │   ├── page.tsx               # Single-page review console
│   │   ├── layout.tsx
│   │   ├── globals.css
│   │   ├── api/                   # 1-line proxies to BACKEND_URL
│   │   │   ├── index/route.ts
│   │   │   ├── review/route.ts
│   │   │   ├── post-to-github/route.ts
│   │   │   ├── email-review/route.ts
│   │   │   └── repos/route.ts
│   │   └── components/            # IndexingStatus, DiffViewer, WarningCard
│   ├── package.json               # Next 15, React 19, Tailwind v4, TS 5
│   └── tsconfig.json
└── README.md
```
