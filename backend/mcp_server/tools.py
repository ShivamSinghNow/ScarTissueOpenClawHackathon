from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import chromadb
from mcp import types
from mcp.server import Server

from app.services.git_miner import GitMiner
from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRFetcher
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolServices:
    scar_index: ScarIndex
    nia_client: NiaClient
    reviewer: Reviewer
    pr_fetcher: PRFetcher
    git_miner: GitMiner


INDEX_REPO_TOOL = types.Tool(
    name="scartissue_index_repo",
    description=(
        "Mine a repository's git history for historical bug fixes and index them for future "
        "reviews. Run this once per repo before using scartissue_review_pr. Takes a few "
        "minutes on large repos."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "GitHub repo in owner/repo format",
            },
            "max_commits": {
                "type": "integer",
                "default": 3000,
            },
        },
        "required": ["repo"],
    },
)

REVIEW_PR_TOOL = types.Tool(
    name="scartissue_review_pr",
    description=(
        "Review a GitHub pull request for reintroduction of historical bug patterns. Requires "
        "the target repo to be indexed first. Returns warnings with severity, matched prior "
        "commits, and suggested fixes."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "pr_url": {"type": "string"},
        },
        "required": ["pr_url"],
    },
)

SEARCH_SCAR_TISSUE_TOOL = types.Tool(
    name="scartissue_search_scar_tissue",
    description=(
        "Search a repo's indexed scar tissue for historical bug fixes matching a query. "
        "Useful for ad-hoc exploration without reviewing a full PR."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "query": {"type": "string"},
            "top_k": {
                "type": "integer",
                "default": 5,
            },
        },
        "required": ["repo", "query"],
    },
)

LIST_INDEXED_REPOS_TOOL = types.Tool(
    name="scartissue_list_indexed_repos",
    description=(
        "List all repositories indexed in the scar tissue store with incident counts and "
        "last-indexed timestamps."
    ),
    inputSchema={
        "type": "object",
        "properties": {},
    },
)

TOOLS = [
    INDEX_REPO_TOOL,
    REVIEW_PR_TOOL,
    SEARCH_SCAR_TISSUE_TOOL,
    LIST_INDEXED_REPOS_TOOL,
]


@contextmanager
def _stdout_to_stderr() -> Any:
    with redirect_stdout(sys.stderr):
        yield


def _json_text_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
        structuredContent=payload,
        isError=False,
    )


def _error_result(tool_name: str, exc: Exception) -> types.CallToolResult:
    logger.exception("%s failed", tool_name)
    payload = {
        "error": str(exc),
        "tool": tool_name,
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=True,
    )


def _best_effort_repo_name(collection_name: str) -> str:
    if "_" not in collection_name:
        return collection_name
    owner, repo_tail = collection_name.split("_", 1)
    return f"{owner}/{repo_tail}"


async def _handle_index_repo(args: dict[str, Any], services: ToolServices) -> types.CallToolResult:
    repo = args["repo"]
    max_commits = int(args.get("max_commits", 3000))
    start = time.perf_counter()
    try:
        with _stdout_to_stderr():
            incidents = services.git_miner.mine(repo, max_commits=max_commits)
            indexed = services.scar_index.index_incidents(repo, incidents)
        return _json_text_result(
            {
                "repo": repo,
                "incidents_indexed": indexed,
                "duration_seconds": round(time.perf_counter() - start, 2),
            }
        )
    except Exception as exc:
        return _error_result(INDEX_REPO_TOOL.name, exc)


async def _handle_review_pr(args: dict[str, Any], services: ToolServices) -> types.CallToolResult:
    pr_url = args["pr_url"]
    try:
        with _stdout_to_stderr():
            pr = services.pr_fetcher.fetch(pr_url)
        warnings = await services.reviewer.review(pr)
        payload = {
            "pr_url": pr_url,
            "pr_title": pr.title,
            "pr_author": pr.author,
            "warnings": [warning.model_dump(mode="json") for warning in warnings],
            "total_warnings": len(warnings),
        }
        return _json_text_result(payload)
    except Exception as exc:
        return _error_result(REVIEW_PR_TOOL.name, exc)


async def _handle_search_scar_tissue(
    args: dict[str, Any], services: ToolServices
) -> types.CallToolResult:
    repo = args["repo"]
    query = args["query"]
    top_k = int(args.get("top_k", 5))
    try:
        hits = services.scar_index.search(repo=repo, query=query, top_k=top_k)
        payload = {
            "repo": repo,
            "query": query,
            "results": [
                {
                    "commit_sha": incident.commit_sha,
                    "commit_message_first_line": incident.commit_message.splitlines()[0] if incident.commit_message else "",
                    "commit_date": incident.commit_date.isoformat(),
                    "files_changed": incident.files_changed,
                    "similarity": round(similarity, 4),
                }
                for incident, similarity in hits
            ],
        }
        return _json_text_result(payload)
    except Exception as exc:
        return _error_result(SEARCH_SCAR_TISSUE_TOOL.name, exc)


async def _handle_list_indexed_repos(
    _args: dict[str, Any], services: ToolServices
) -> types.CallToolResult:
    try:
        persist_dir = os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
        client = chromadb.PersistentClient(path=persist_dir)
        repos: list[dict[str, Any]] = []
        for collection in client.list_collections():
            best_effort_repo = _best_effort_repo_name(collection.name)
            stats = services.scar_index.collection_stats(best_effort_repo)
            repos.append(
                {
                    "repo": best_effort_repo,
                    "incidents": stats["count"],
                    "last_indexed": stats["last_indexed"],
                }
            )

        repos.sort(key=lambda item: item["repo"])
        return _json_text_result({"repos": repos})
    except Exception as exc:
        return _error_result(LIST_INDEXED_REPOS_TOOL.name, exc)


def register_tools(server: Server, services: ToolServices) -> None:
    handlers: dict[str, Callable[[dict[str, Any], ToolServices], Awaitable[types.CallToolResult]]] = {
        INDEX_REPO_TOOL.name: _handle_index_repo,
        REVIEW_PR_TOOL.name: _handle_review_pr,
        SEARCH_SCAR_TISSUE_TOOL.name: _handle_search_scar_tissue,
        LIST_INDEXED_REPOS_TOOL.name: _handle_list_indexed_repos,
    }

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        handler = handlers.get(name)
        if handler is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
        return await handler(arguments, services)
