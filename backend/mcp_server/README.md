# ScarTissue MCP Server

ScarTissue is a pull request review system that mines a repository's historical bug-fix commits, indexes those incidents as "scar tissue," and uses that history to warn when a new PR appears to reintroduce an old failure pattern. This MCP server exposes the existing ScarTissue backend services over stdio so Claude Code, Codex CLI, and Gemini CLI can call them as tools.

## Installation

1. Clone the repository.
2. `cd backend`
3. `uv pip install -e .`
4. Populate `backend/.env` with `ANTHROPIC_API_KEY`, `NIA_API_KEY`, `GITHUB_TOKEN`, and `CHROMA_PERSIST_DIR`.

## Client Config

### Claude Code

Add this to `~/.claude/mcp.json` or workspace `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "scartissue": {
      "command": "scartissue-mcp",
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "NIA_API_KEY": "nia_...",
        "GITHUB_TOKEN": "ghp_...",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/scartissue/backend/chroma_db"
      }
    }
  }
}
```

### Codex CLI

Add this to `~/.codex/mcp_servers.toml`:

```toml
[scartissue]
command = "scartissue-mcp"

[scartissue.env]
ANTHROPIC_API_KEY = "sk-ant-..."
NIA_API_KEY = "nia_..."
GITHUB_TOKEN = "ghp_..."
CHROMA_PERSIST_DIR = "/absolute/path/to/scartissue/backend/chroma_db"
```

### Gemini CLI

Add this to `~/.gemini/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "scartissue": {
      "command": "scartissue-mcp",
      "args": [],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "NIA_API_KEY": "nia_...",
        "GITHUB_TOKEN": "ghp_...",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/scartissue/backend/chroma_db"
      }
    }
  }
}
```

## Tools

- `scartissue_index_repo`: Mine a repository's git history and index historical incidents for later review.
- `scartissue_review_pr`: Review a GitHub PR against previously indexed scar tissue.
- `scartissue_search_scar_tissue`: Search indexed incident history for bug-fix examples related to a query.
- `scartissue_list_indexed_repos`: List indexed repositories with incident counts and last-indexed timestamps.

## Canonical Workflow

Example conversation:

User:
`review this PR: https://github.com/owner/repo/pull/123`

Agent action:
Calls `scartissue_review_pr` with:

```json
{
  "pr_url": "https://github.com/owner/repo/pull/123"
}
```

Inline result shown by the agent:

```json
{
  "pr_url": "https://github.com/owner/repo/pull/123",
  "pr_title": "Preserve streaming callback cleanup on retries",
  "pr_author": "octocat",
  "warnings": [
    {
      "pr_file": "libs/core/streaming.py",
      "pr_hunk": "@@ -48,7 +48,6 @@",
      "matched_incident": {
        "commit_sha": "abc123...",
        "commit_message": "fix: always close streaming iterator on retry failure",
        "commit_date": "2025-02-10T12:34:56Z",
        "author": "octocat",
        "files_changed": [
          "libs/core/streaming.py"
        ],
        "functions_changed": [],
        "fix_diff": "...",
        "buggy_parent_sha": "def456...",
        "issue_refs": [
          12345
        ],
        "symptom_summary": null
      },
      "severity": "high",
      "explanation": "This change removes the cleanup path that commit abc123 restored after retry failures leaked open streaming iterators.",
      "confidence": 0.92,
      "proposed_fix": "Restore the iterator cleanup in the retry failure branch before re-raising."
    }
  ],
  "total_warnings": 1
}
```

## Notes

- Index a repository once with `scartissue_index_repo` before reviewing PRs from that repository.
- This server uses stdio transport, so all logs are written to `stderr` only. Writing to `stdout` would break the MCP protocol.
