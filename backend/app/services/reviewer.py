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

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5-20250929"
MAX_TOKENS = 4096
TEMPERATURE = 0.3
MAX_ITERATIONS = 20

# Confidence grounding: blend the model's self-reported confidence with the
# best cosine similarity we ever returned for the matched commit. Pure model
# confidence is freely invented; pure similarity is too coarse. Weighting
# leans on the model (it sees the diff) but anchors on retrieval signal.
_CONFIDENCE_MODEL_WEIGHT = 0.6
_CONFIDENCE_SIMILARITY_WEIGHT = 0.4

_SYSTEM = """\
You are ScarTissue, a code review agent. Your job is to detect when a pull request is about to reintroduce a bug pattern that was previously fixed in this repo's history.

You have four tools: search_scar_tissue, get_incident_details, search_current_code, emit_warning.

Workflow for each hunk in the PR:
1. Understand what the hunk changes. Identify the pattern it introduces or removes.
2. Call search_scar_tissue with a descriptive query about the change.
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
"""

_TOOLS: list[dict] = [
    {
        "name": "search_scar_tissue",
        "description": (
            "Search the repo's historical bug fix index for incidents similar to a PR change. "
            "Returns {results: [...up to 5 incidents], indexed_incidents: int, reason?: str}. "
            "Each result has commit_sha, commit_message, commit_date, files_changed, similarity. "
            "An empty results list with reason='repo_not_indexed' means the repo has no scar "
            "tissue indexed yet — do not emit warnings in that case. An empty results list with "
            "reason='no_hits_above_similarity_floor' means nothing in the index is close enough "
            "to confidently flag — prefer silence over speculation. Use similarity scores to "
            "calibrate confidence: high similarity (>0.6) means strong embedding match; medium "
            "(0.4–0.6) means worth investigating; below 0.4 is filtered out automatically."
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
]


def _build_user_message(pr: PRDiff) -> str:
    files_str = ", ".join(pr.files_changed)
    hunks_parts: list[str] = []
    for hunk in pr.hunks:
        hunks_parts.append(f"=== {hunk.file_path} ===\n{hunk.header}\n{hunk.content}")
    hunks_str = "\n\n".join(hunks_parts)

    return (
        f"Review this pull request for historical bug reintroduction.\n\n"
        f"Repo: {pr.repo}\n"
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
    ) -> None:
        self._scar = scar_index
        self._nia = nia
        # Accept either Anthropic or AsyncAnthropic — review() is always async
        self._claude = anthropic_client

    # ── tool execution ────────────────────────────────────────────────────────

    async def _exec_tool(
        self,
        pr: PRDiff,
        name: str,
        inp: dict[str, Any],
        observed_similarity: dict[str, float] | None = None,
    ) -> Any:
        if name == "search_scar_tissue":
            # Bias retrieval toward historical commits that touched the same
            # path the PR is editing. Without this, retrieval is pure cosine
            # over commit message + diff text and surfaces unrelated fixes
            # whose phrasing happens to overlap.
            pr_file = inp.get("pr_file")
            pr_files = [pr_file] if pr_file else None
            stats = self._scar.collection_stats(pr.repo)
            hits = self._scar.search(
                repo=pr.repo,
                query=inp["query"],
                top_k=5,
                pr_files=pr_files,
            )
            results = [
                {
                    "commit_sha": inc.commit_sha,
                    "commit_message": inc.commit_message.splitlines()[0][:120],
                    "commit_date": inc.commit_date.isoformat(),
                    "files_changed": inc.files_changed[:10],
                    "similarity": round(score, 4),
                }
                for inc, score in hits
            ]
            if observed_similarity is not None:
                for inc, score in hits:
                    prev = observed_similarity.get(inc.commit_sha, 0.0)
                    if score > prev:
                        observed_similarity[inc.commit_sha] = score
            if results:
                return {"results": results, "indexed_incidents": stats["count"]}
            # Distinguish "nothing similar enough" from "repo not indexed" so the
            # agent doesn't conflate them and emit confident warnings on noise.
            if stats["count"] == 0:
                return {
                    "results": [],
                    "indexed_incidents": 0,
                    "reason": "repo_not_indexed",
                }
            return {
                "results": [],
                "indexed_incidents": stats["count"],
                "reason": "no_hits_above_similarity_floor",
            }

        if name == "get_incident_details":
            sha = inp["commit_sha"]
            inc = self._scar.get_by_sha(pr.repo, sha)
            if inc is None:
                return {"error": f"No incident found for sha {sha!r}"}
            return json.loads(inc.model_dump_json())

        if name == "search_current_code":
            try:
                result = await self._nia.search(inp["query"], repositories=[pr.repo])
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
        # Best cosine similarity ever returned for each commit_sha during this
        # review. Used to ground emit_warning's self-reported confidence.
        observed_similarity: dict[str, float] = {}

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
                        matched = self._scar.get_by_sha(pr.repo, sha)
                        model_confidence = float(inp["confidence"])
                        # Ground the agent's self-reported confidence in the
                        # cosine similarity we actually observed for this sha.
                        # If the agent cited a sha we never returned (it found
                        # the incident via get_incident_details after a search
                        # on a different file, etc.), fall back to model-only.
                        sim = observed_similarity.get(sha)
                        if sim is not None:
                            grounded = (
                                _CONFIDENCE_MODEL_WEIGHT * model_confidence
                                + _CONFIDENCE_SIMILARITY_WEIGHT * sim
                            )
                        else:
                            grounded = model_confidence
                            logger.info(
                                "emit_warning sha=%s had no observed similarity; "
                                "using model confidence as-is",
                                sha[:12],
                            )
                        # Clamp into the schema range and round for clean display.
                        grounded = max(0.0, min(1.0, grounded))
                        w = Warning(
                            pr_file=inp["pr_file"],
                            pr_hunk=inp["pr_hunk_header"],
                            matched_incident=matched,
                            severity=inp["severity"],
                            explanation=inp["explanation"],
                            confidence=round(grounded, 4),
                            proposed_fix=inp.get("proposed_fix"),
                        )
                        key: _DedupKey = (w.pr_file, w.pr_hunk, sha)
                        existing = dedup.get(key)
                        if existing is None or w.confidence > existing.confidence:
                            dedup[key] = w
                        if on_warning:
                            await on_warning(w)
                        result: Any = {
                            "ok": True,
                            "warning_id": uuid.uuid4().hex[:8],
                            "grounded_confidence": w.confidence,
                            "model_confidence": model_confidence,
                            "observed_similarity": sim,
                        }
                    except Exception as exc:
                        logger.warning("emit_warning construction failed: %s", exc)
                        result = {"error": str(exc)}
                else:
                    try:
                        result = await self._exec_tool(
                            pr, block.name, block.input,
                            observed_similarity=observed_similarity,
                        )
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
