# ScarTissue

PR review agent that warns when a pull request is about to reintroduce a historical bug pattern.

## How it works

1. **Index** a repo — mines git history for fix/bugfix commits and stores them as embeddings in ChromaDB
2. **Review** a PR — fetches the diff, finds similar historical incidents via vector search, runs a Claude agent loop, emits targeted warnings

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI, Python 3.11, uvicorn |
| LLM | Anthropic Claude (`claude-sonnet-4-20250514`) with tool use |
| Vector store | ChromaDB + `sentence-transformers/all-MiniLM-L6-v2` |
| Live code search | Nia API (`https://apigcp.trynia.ai/v2`) |
| Git ops | GitPython |
| GitHub API | PyGithub |
| Frontend | Next.js 15, TypeScript, Tailwind v4 |

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

## API

| Method | Path | Body | Description |
|---|---|---|---|
| `POST` | `/index` | `{ "repo": "owner/repo" }` | Mine git history and build scar index |
| `POST` | `/review` | `{ "pr_url": "https://github.com/..." }` | Analyze PR, return warnings |
| `GET` | `/health` | — | Liveness check |

## Environment variables (`backend/.env`)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `NIA_API_KEY` | Nia API bearer token |
| `GITHUB_TOKEN` | GitHub personal access token (for PR fetching) |
| `CHROMA_PERSIST_DIR` | ChromaDB persistence path (default: `./chroma_db`) |
