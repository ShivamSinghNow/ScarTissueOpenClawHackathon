from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import anthropic

from app.models.schemas import Incident, Warning
from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRDiff
from app.services.scar_index import ScarIndex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.gbrain_client import GBrainClient, BugPattern

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
TEMPERATURE = 0.3
MAX_ITERATIONS = 20

_SYSTEM = """\
You are ScarTissue, a code review agent. Your job is to detect when a pull request is about to reintroduce a bug pattern that was previously fixed in this repo's history.

You have six tools: search_scar_tissue, get_incident_details, search_current_code, emit_warning, learn_from_incident, search_org_scar_tissue.

Workflow for each hunk in the PR:
1. Understand what the hunk changes. Identify the pattern it introduces or removes.
2. Call search_scar_tissue with a descriptive query about the change.
2b. If search_scar_tissue returns no matches with confidence >= 0.5, call search_org_scar_tissue before concluding no pattern exists — cross-repo GBrain patterns catch things the local index misses. When a GBrain match triggers a warning, pass learned_from="live_warning" to emit_warning.
3. Examine the returned candidates. Most will be false positives due to surface-level similarity. Filter ruthlessly.
4. For any candidate that looks genuinely related, call get_incident_details to read the full fix.
5. Ask: does this PR undo, weaken, or rhyme with the protective change that past fix made?
6. If yes and you are confident (confidence >= 0.5), call emit_warning with severity, explanation, and when possible a proposed_fix.
7. When useful, call search_current_code to verify whether a safeguard is still present in the broader code.

Quality bar:
- A great warning cites a specific prior commit, explains the connection in one clear sentence, and leaves no doubt that the author should look at it.
- Do not emit warnings below 0.5 confidence. Silence is better than noise.
- Do not emit more than one warning per hunk.
- Severity: high = likely to reintroduce a known incident; medium = pattern match worth reviewing; low = do not emit.
- When you can confidently suggest a small code change that preserves the protective pattern, include it as proposed_fix.

Output:
- Emit all findings via emit_warning. Do not write prose explanations outside tool calls.
- When you have reviewed every significant hunk, respond with a single short final message summarizing how many warnings you emitted.
8. After emitting a HIGH severity warning, call learn_from_incident exactly once per review session for the single highest-confidence finding. Only for HIGH severity. Do not call it for medium or low warnings — don't learn from noise.
"""

_TOOLS: list[dict] = [
    {
        "name": "search_scar_tissue",
        "description": (
            "Search the repo's historical bug fix index for incidents similar to a PR change. "
            "Returns up to 5 past incidents with their commit shas, messages, and fix diffs. "
            "Use this as your primary signal for detecting regressions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A description of the code change or pattern to search for. Be specific about what the change does.",
                },
                "pr_file": {
                    "type": "string",
                    "description": "The file path this query relates to, for later warning attribution.",
                },
            },
            "required": ["query", "pr_file"],
        },
    },
    {
        "name": "get_incident_details",
        "description": (
            "Retrieve the full incident record for a specific commit sha found via search_scar_tissue. "
            "Use this when you need the full fix diff to understand what was fixed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"commit_sha": {"type": "string"}},
            "required": ["commit_sha"],
        },
    },
    {
        "name": "search_current_code",
        "description": (
            "Search the current state of the repo's codebase via Nia. Use this to verify whether a "
            "protective pattern from a past fix is still present in the current code, or to understand "
            "surrounding context. Only call this when it would meaningfully change your confidence in a warning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "emit_warning",
        "description": (
            "Emit a warning that the PR is about to reintroduce a historical bug pattern. "
            "Only emit warnings with medium or high confidence. Each warning must cite a specific prior "
            "commit and explain the connection in one sentence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_file": {"type": "string"},
                "pr_hunk_header": {
                    "type": "string",
                    "description": "The @@ header line of the hunk this warning targets.",
                },
                "matched_commit_sha": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                "explanation": {
                    "type": "string",
                    "description": "One sentence explaining how this PR change rhymes with the past fix.",
                },
                "confidence": {"type": "number", "description": "0.0 to 1.0"},
                "proposed_fix": {
                    "type": "string",
                    "description": (
                        "Optional. A short code snippet or specific instruction the author should "
                        "apply to avoid reintroducing the bug. Omit if you cannot confidently suggest one."
                    ),
                },
                "learned_from": {
                    "type": "string",
                    "enum": ["git_history", "live_warning", "merge_webhook"],
                    "description": (
                        "Optional. Set to 'live_warning' when this warning was triggered by a "
                        "cross-repo GBrain pattern from search_org_scar_tissue. Omit or leave "
                        "as 'git_history' for local ChromaDB matches."
                    ),
                },
            },
            "required": [
                "pr_file",
                "pr_hunk_header",
                "matched_commit_sha",
                "severity",
                "explanation",
                "confidence",
            ],
        },
    },
    {
        "name": "learn_from_incident",
        "description": (
            "Write a new BugPattern to the GBrain cross-repo memory when you have fired a "
            "high-confidence warning. Only call this after emit_warning for HIGH severity findings. "
            "Normalizes the incident into a structured pattern so future PRs in any repo can match it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "trigger_condition": {"type": "string", "description": "What code shape triggers the bug."},
                "bad_change_shape": {"type": "string", "description": "What the dangerous PR diff looks like."},
                "protective_invariant": {"type": "string", "description": "What must stay true to stay safe."},
                "fix_shape": {"type": "string", "description": "What the fix looks like in code."},
                "affected_symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Function or class names involved.",
                },
                "source_commit": {"type": "string", "description": "The matched_commit_sha from the warning you just emitted."},
                "confidence": {"type": "number", "description": "Your confidence 0.0-1.0 in this pattern."},
            },
            "required": [
                "trigger_condition", "bad_change_shape", "protective_invariant",
                "fix_shape", "source_commit", "confidence",
            ],
        },
    },
    {
        "name": "search_org_scar_tissue",
        "description": (
            "Search GBrain for BugPatterns learned across ALL repos in your organization, "
            "not just the current repo's ChromaDB index. Use this after search_scar_tissue "
            "returns no strong matches — cross-repo patterns often catch things the local "
            "index misses."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic description of the code pattern to search for.",
                },
                "pr_file": {
                    "type": "string",
                    "description": "The file path this query relates to.",
                },
            },
            "required": ["query", "pr_file"],
        },
    },
]


def _build_user_message(pr: PRDiff) -> str:
    files_str = ", ".join(pr.files_changed)
    hunks_parts: list[str] = []
    for hunk in pr.hunks:
        hunks_parts.append(f"=== {hunk.file_path} ===\n{hunk.header}\n{hunk.content}")
    hunks_str = "\n\n".join(hunks_parts)

    repo_line = f"Repo: {pr.repo}"
    if pr.upstream_repo:
        repo_line += f"  (fork of {pr.upstream_repo} — scar-tissue lookups run against the upstream)"

    return (
        f"Review this pull request for historical bug reintroduction.\n\n"
        f"{repo_line}\n"
        f"PR: #{pr.number} - {pr.title}\n"
        f"Author: {pr.author}\n\n"
        f"Changed files: {files_str}\n\n"
        f"Hunks to review:\n\n"
        f"{hunks_str}\n\n"
        f"Review each hunk systematically. Emit warnings via the emit_warning tool."
    )


def _blocks_to_params(content: list) -> list[dict]:
    """Serialize SDK content blocks to plain dicts for the messages list."""
    params = []
    for block in content:
        if not hasattr(block, "type"):
            continue
        if block.type == "text":
            params.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            params.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return params


# Dedup key: (pr_file, pr_hunk_header, matched_commit_sha)
_DedupKey = tuple[str, str, str]


class Reviewer:
    def __init__(
        self,
        scar_index: ScarIndex,
        nia: NiaClient,
        anthropic_client: anthropic.Anthropic,
        gbrain_client: "GBrainClient | None" = None,
    ) -> None:
        self._scar = scar_index
        self._nia = nia
        # Accept either Anthropic or AsyncAnthropic — review() is always async
        self._claude = anthropic_client
        self._gbrain = gbrain_client

    # ── tool execution ────────────────────────────────────────────────────────

    async def _exec_tool(
        self, pr: PRDiff, name: str, inp: dict[str, Any]
    ) -> Any:
        # Forks share scar tissue with their upstream — query the indexed root.
        index_repo = pr.upstream_repo or pr.repo

        if name == "search_scar_tissue":
            hits = self._scar.search(repo=index_repo, query=inp["query"], top_k=5)
            return [
                {
                    "commit_sha": inc.commit_sha,
                    "commit_message": inc.commit_message.splitlines()[0][:120],
                    "commit_date": inc.commit_date.isoformat(),
                    "files_changed": inc.files_changed[:10],
                    "similarity": round(score, 4),
                }
                for inc, score in hits
            ]

        if name == "get_incident_details":
            sha = inp["commit_sha"]
            inc = self._scar.get_by_sha(index_repo, sha)
            if inc is None:
                return {"error": f"No incident found for sha {sha!r}"}
            return json.loads(inc.model_dump_json())

        if name == "search_current_code":
            try:
                result = await self._nia.search(inp["query"], repositories=[index_repo])
                # Trim to top 3 hits regardless of response shape
                for key in ("results", "hits", "items", "matches"):
                    if isinstance(result.get(key), list):
                        result[key] = result[key][:3]
                        break
                return result
            except Exception as exc:
                msg = str(exc).lower()
                if any(tok in msg for tok in ("404", "not found", "not indexed", "unauthorized")):
                    return {"error": "Nia has not yet indexed this repo, skip this tool for now"}
                return {"error": f"Nia search failed: {exc}"}

        if name == "search_org_scar_tissue":
            if self._gbrain is None:
                return []
            try:
                patterns = await self._gbrain.search(inp["query"], top_k=5)
                return [
                    {
                        "pattern_id": p.id,
                        "trigger_condition": p.trigger_condition,
                        "bad_change_shape": p.bad_change_shape,
                        "protective_invariant": p.protective_invariant,
                        "fix_shape": p.fix_shape,
                        "source_repo": p.source_repo,
                        "source_commit": p.source_commit,
                        "confidence": round(p.confidence, 4),
                        "learned_from": p.learned_from,
                        "learned_at": p.learned_at,
                    }
                    for p in patterns
                ]
            except Exception as exc:
                logger.warning("[search_org_scar_tissue] GBrain search failed: %s", exc)
                return []

        if name == "learn_from_incident":
            if self._gbrain is None:
                return {"ok": False, "reason": "GBrain not configured — pattern not saved"}
            try:
                from app.services.gbrain_client import BugPattern
                pattern = BugPattern(
                    trigger_condition=inp["trigger_condition"],
                    bad_change_shape=inp["bad_change_shape"],
                    protective_invariant=inp["protective_invariant"],
                    fix_shape=inp["fix_shape"],
                    affected_symbols=inp.get("affected_symbols", []),
                    source_commit=inp["source_commit"],
                    source_repo=pr.upstream_repo or pr.repo,
                    confidence=float(inp["confidence"]),
                    learned_from="live_warning",
                )
                slug = await self._gbrain.write_pattern(pattern)
                logger.info("[learn_from_incident] wrote pattern slug=%s", slug)
                return {"ok": True, "pattern_id": slug, "learned_from": "live_warning"}
            except Exception as exc:
                logger.warning("[learn_from_incident] GBrain write failed: %s", exc)
                return {"error": f"GBrain write failed: {exc}"}

        return {"error": f"Unknown tool: {name!r}"}

    # ── main agent loop ───────────────────────────────────────────────────────

    async def review(
        self,
        pr: PRDiff,
        on_warning: Optional[Callable[[Warning], Awaitable[None]]] = None,
        on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> list[Warning]:
        """Run the Claude agent loop. Returns deduplicated warnings sorted by confidence."""

        dedup: dict[_DedupKey, Warning] = {}

        async def _progress(msg: str) -> None:
            logger.info("[reviewer] %s", msg)
            if on_progress:
                await on_progress(msg)

        await _progress(f"Reviewing PR #{pr.number} — {pr.title}")

        messages: list[dict] = [
            {"role": "user", "content": _build_user_message(pr)}
        ]

        for iteration in range(MAX_ITERATIONS):
            await _progress(f"Iteration {iteration + 1}: calling {MODEL}…")

            response = await self._claude.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=_SYSTEM,
                tools=_TOOLS,
                tool_choice={"type": "auto"},
                messages=messages,
            )

            logger.debug(
                "stop_reason=%s blocks=%d",
                response.stop_reason,
                len(response.content),
            )

            # Commit assistant turn (serialized to plain dicts)
            messages.append({
                "role": "assistant",
                "content": _blocks_to_params(response.content),
            })

            tool_blocks = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "end_turn" and not tool_blocks:
                await _progress("Agent finished cleanly.")
                break

            # Execute tools and build the tool_result user turn
            tool_results: list[dict] = []
            for block in tool_blocks:
                logger.info(
                    "[tool] %s  input=%s",
                    block.name,
                    json.dumps(block.input)[:200],
                )

                if block.name == "emit_warning":
                    try:
                        inp = block.input
                        sha = inp["matched_commit_sha"]
                        matched = self._scar.get_by_sha(pr.upstream_repo or pr.repo, sha)
                        w = Warning(
                            pr_file=inp["pr_file"],
                            pr_hunk=inp["pr_hunk_header"],
                            matched_incident=matched,
                            severity=inp["severity"],
                            explanation=inp["explanation"],
                            confidence=float(inp["confidence"]),
                            proposed_fix=inp.get("proposed_fix"),
                            learned_from=inp.get("learned_from", "git_history"),
                        )
                        key: _DedupKey = (w.pr_file, w.pr_hunk, sha)
                        existing = dedup.get(key)
                        if existing is None or w.confidence > existing.confidence:
                            dedup[key] = w
                        if on_warning:
                            await on_warning(w)
                        result: Any = {"ok": True, "warning_id": uuid.uuid4().hex[:8]}
                    except Exception as exc:
                        logger.warning("emit_warning construction failed: %s", exc)
                        result = {"error": str(exc)}
                else:
                    try:
                        result = await self._exec_tool(pr, block.name, block.input)
                    except Exception as exc:
                        logger.warning("Tool %s raised: %s", block.name, exc)
                        result = {"error": str(exc)}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        else:
            logger.warning(
                "[reviewer] Safety cap reached (%d iterations). Returning %d warning(s).",
                MAX_ITERATIONS,
                len(dedup),
            )

        warnings = sorted(dedup.values(), key=lambda w: w.confidence, reverse=True)
        await _progress(f"Done — {len(warnings)} warning(s) emitted.")
        return warnings


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from app.services.pr_fetcher import PRFetcher

    PR_URL = "https://github.com/langchain-ai/langchain/pull/35238"

    # ── build dependencies ────────────────────────────────────────────────────
    scar = ScarIndex(persist_dir="./chroma_db")
    stats = scar.collection_stats("langchain-ai/langchain")
    print(f"\nScar index stats: {stats}")
    if stats["count"] == 0:
        print("⚠  Index empty — run scar_index.py first to populate it.")

    nia = NiaClient()
    if not __import__("os").environ.get("NIA_API_KEY"):
        logger.warning("NIA_API_KEY not set — search_current_code will gracefully no-op")

    claude = anthropic.AsyncAnthropic()

    reviewer = Reviewer(scar_index=scar, nia=nia, anthropic_client=claude)

    fetcher = PRFetcher()
    print(f"\nFetching {PR_URL} …")
    pr = fetcher.fetch(PR_URL)
    print(f"PR fetched: #{pr.number} — {pr.title}")
    print(f"  Files  : {len(pr.files_changed)}  Hunks: {len(pr.hunks)}\n")

    # ── run agent ─────────────────────────────────────────────────────────────
    async def _run() -> None:
        async def _on_warning(w: Warning) -> None:
            sha = w.matched_incident.commit_sha[:7] if w.matched_incident else "unknown"
            print(f"\n{'─'*60}")
            print(f"  [WARNING] {w.severity.upper()}  confidence={w.confidence:.2f}")
            print(f"  File   : {w.pr_file}")
            print(f"  Hunk   : {w.pr_hunk}")
            print(f"  Commit : {sha}")
            print(f"  Explain: {w.explanation}")
            if w.proposed_fix:
                print(f"  Fix    : {w.proposed_fix}")

        async def _on_progress(msg: str) -> None:
            print(f"  [progress] {msg}")

        warnings = await reviewer.review(pr, on_warning=_on_warning, on_progress=_on_progress)

        print(f"\n{'='*60}")
        print(f"Total warnings: {len(warnings)}")
        for i, w in enumerate(warnings, 1):
            sha = w.matched_incident.commit_sha[:7] if w.matched_incident else "N/A"
            print(f"  {i}. {w.severity.upper()} [{sha}] conf={w.confidence:.2f}  {w.pr_file}")

    asyncio.run(_run())
