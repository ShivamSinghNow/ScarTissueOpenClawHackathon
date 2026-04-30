# ScarTissue MCP

ScarTissue is a PR review agent that warns when a pull request appears to reintroduce a historical bug pattern from the same repository. It mines prior bug-fix commits, stores them as searchable "scar tissue," and exposes the review workflow as an MCP stdio server for Claude Code, Codex CLI, Gemini CLI, and other MCP clients.

## Install

```bash
pip install scartissue-mcp
```

After installation, start the MCP stdio server with:

```bash
scartissue-mcp
```

## Configuration

Set these environment variables in your MCP client config:

| Variable | Required | Description |
|---|---:|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key used by the review agent. |
| `NIA_API_KEY` | Yes | Nia API key used for current-code search. |
| `GITHUB_TOKEN` | Yes | GitHub token used to fetch pull requests and mine repositories. |
| `CHROMA_PERSIST_DIR` | Yes | Absolute path to the local ChromaDB directory where scar tissue indexes are stored. |

## Claude Code

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
        "CHROMA_PERSIST_DIR": "/absolute/path/to/scartissue/chroma_db"
      }
    }
  }
}
```

## Codex CLI

Add this to `~/.codex/mcp_servers.toml`:

```toml
[scartissue]
command = "scartissue-mcp"

[scartissue.env]
ANTHROPIC_API_KEY = "sk-ant-..."
NIA_API_KEY = "nia_..."
GITHUB_TOKEN = "ghp_..."
CHROMA_PERSIST_DIR = "/absolute/path/to/scartissue/chroma_db"
```

## Gemini CLI

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
        "CHROMA_PERSIST_DIR": "/absolute/path/to/scartissue/chroma_db"
      }
    }
  }
}
```

## Tools

- `scartissue_index_repo`: mine a repository's git history and index historical bug-fix incidents.
- `scartissue_review_pr`: review a GitHub pull request against previously indexed scar tissue.
- `scartissue_search_scar_tissue`: search indexed incident history for bug-fix examples related to a query.
- `scartissue_list_indexed_repos`: list indexed repositories with incident counts and last-indexed timestamps.

Index a repository once with `scartissue_index_repo` before reviewing PRs from that repository.

## Links

- Web UI: [https://scartissue.dev](https://scartissue.dev)
- GitHub: [https://github.com/ShivamSinghNow/scartissue](https://github.com/ShivamSinghNow/scartissue)
