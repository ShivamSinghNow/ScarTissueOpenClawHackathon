"""
GBrain client — wraps the gbrain CLI (v0.18.2, PGLite local engine).

API surface confirmed by SCA-5 spike (2026-05-16):
  - Engine:  PGLite local at ~/.gbrain/brain.pglite
  - Write:   gbrain put <slug>  (stdin = markdown with YAML frontmatter)
  - Read:    gbrain get <slug>
  - Search:  gbrain query <question>  (hybrid RRF, ~250ms p50)
             gbrain search <query>    (keyword tsvector, ~480ms p50)
  - Slugs:   kebab-case, must be unique — use bugpattern-{uuid4}
  - Metadata: YAML frontmatter fields preserved and returned on get
  - No --metadata JSON flag; structured data goes in frontmatter or body

Verdict: Approach B (GBrain Live Immune System) is GO.
Full implementation lives here. SCA-10/SCA-11 add the agent tools that call it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

GBRAIN_TIMEOUT = 3.0  # seconds — agent loop must never stall on GBrain


@dataclass
class BugPattern:
    trigger_condition: str
    bad_change_shape: str
    protective_invariant: str
    fix_shape: str
    source_commit: str
    source_repo: str
    confidence: float
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    affected_symbols: list[str] = field(default_factory=list)
    source_pr: str | None = None
    learned_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    learned_from: str = "live_warning"  # "git_history" | "live_warning" | "merge_webhook"


def _gbrain_bin() -> str:
    """Resolve the gbrain binary path."""
    import os
    # Augment PATH with bun's bin dir so shutil.which finds bun-linked scripts
    bun_bin_dir = os.path.expanduser("~/.bun/bin")
    augmented_path = f"{bun_bin_dir}:{os.environ.get('PATH', '')}"
    if path := shutil.which("gbrain", path=augmented_path):
        return path
    # Legacy location from gstack-gbrain-install
    legacy = os.path.expanduser("~/gbrain/node_modules/.bin/gbrain")
    if os.path.exists(legacy):
        return legacy
    raise RuntimeError(
        "gbrain binary not found. Run: ~/.claude/skills/gstack/bin/gstack-gbrain-install"
    )


def _subprocess_env() -> dict:
    """Build env for gbrain subprocesses — ensures bun is on PATH."""
    import os
    env = os.environ.copy()
    bun_bin_dir = os.path.expanduser("~/.bun/bin")
    env["PATH"] = f"{bun_bin_dir}:{env.get('PATH', '')}"
    return env


def _pattern_to_markdown(pattern: BugPattern) -> str:
    """Serialize BugPattern to gbrain markdown page (YAML frontmatter + body)."""
    symbols_yaml = "\n".join(f"  - {s}" for s in pattern.affected_symbols)
    symbols_block = f"\naffected_symbols:\n{symbols_yaml}" if symbols_yaml else ""

    return f"""---
title: "BugPattern: {pattern.trigger_condition[:60]}"
tags:
  - scartissue
  - bugpattern
source_repo: {pattern.source_repo}
source_commit: {pattern.source_commit}
confidence: {pattern.confidence}
learned_from: {pattern.learned_from}
learned_at: {pattern.learned_at}
pattern_id: {pattern.id}{symbols_block}
---

## BugPattern: {pattern.id}

**trigger_condition:** {pattern.trigger_condition}

**bad_change_shape:** {pattern.bad_change_shape}

**protective_invariant:** {pattern.protective_invariant}

**fix_shape:** {pattern.fix_shape}

**source_pr:** {pattern.source_pr or "n/a"}
"""


def _markdown_to_pattern(slug: str, content: str) -> BugPattern | None:
    """Parse gbrain get output back into a BugPattern. Returns None on parse failure."""
    try:
        fm: dict = {}
        body = content

        # Extract YAML frontmatter between --- delimiters
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                import yaml
                fm = yaml.safe_load(content[3:end]) or {}
                body = content[end + 4:]

        def _extract(key: str, pattern: str) -> str:
            m = re.search(rf"\*\*{key}:\*\*\s*(.+)", body)
            return m.group(1).strip() if m else fm.get(pattern, "")

        return BugPattern(
            id=fm.get("pattern_id", slug),
            trigger_condition=_extract("trigger_condition", "trigger_condition"),
            bad_change_shape=_extract("bad_change_shape", "bad_change_shape"),
            protective_invariant=_extract("protective_invariant", "protective_invariant"),
            fix_shape=_extract("fix_shape", "fix_shape"),
            affected_symbols=fm.get("affected_symbols") or [],
            source_commit=str(fm.get("source_commit", "")),
            source_repo=str(fm.get("source_repo", "")),
            source_pr=_extract("source_pr", "source_pr") or None,
            confidence=float(fm.get("confidence", 0.0)),
            learned_at=str(fm.get("learned_at", "")),
            learned_from=str(fm.get("learned_from", "git_history")),
        )
    except Exception:
        logger.debug("Failed to parse BugPattern from slug=%s", slug, exc_info=True)
        return None


async def _run(args: list[str], stdin: str | None = None) -> str:
    """Run a gbrain CLI command with timeout. Raises on non-zero exit."""
    cmd = [_gbrain_bin(), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_subprocess_env(),
    )
    stdin_bytes = stdin.encode() if stdin else None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_bytes), timeout=GBRAIN_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"gbrain {args[0]} timed out after {GBRAIN_TIMEOUT}s")

    if proc.returncode != 0:
        raise RuntimeError(
            f"gbrain {args[0]} failed (exit {proc.returncode}): {stderr.decode()[:200]}"
        )
    return stdout.decode()


class GBrainClient:
    """
    Async client for GBrain cross-repo BugPattern memory.

    Every public method is GBrain-down safe: exceptions are caught by the
    caller (reviewer.py tool handlers) and fall through to ChromaDB.
    """

    async def write_pattern(self, pattern: BugPattern) -> str:
        """
        Write a BugPattern to GBrain. Returns the slug (pattern ID).
        Raises on failure — caller wraps in try/except.
        """
        slug = f"bugpattern-{pattern.id}"
        content = _pattern_to_markdown(pattern)
        await _run(["put", slug], stdin=content)
        logger.info("GBrain write OK: slug=%s repo=%s", slug, pattern.source_repo)
        return slug

    async def ingest_fix_commits(self, repo: str, commits: list[dict]) -> int:
        written = 0
        for commit in commits:
            try:
                message = commit["message"]
                if not re.search(r"\b(fix|bugfix|hotfix|revert)\b", message, re.IGNORECASE):
                    continue

                pattern = BugPattern(
                    trigger_condition=message.splitlines()[0],
                    bad_change_shape="Fix commit: " + message[:200],
                    protective_invariant="See fix commit for invariant",
                    fix_shape=message,
                    source_commit=commit["id"],
                    source_repo=repo,
                    confidence=0.6,
                    learned_from="merge_webhook",
                )
                await self.write_pattern(pattern)
                written += 1
            except Exception:
                commit_id = commit.get("id") if isinstance(commit, dict) else None
                logger.exception(
                    "Failed to learn BugPattern from merge commit id=%s repo=%s",
                    commit_id,
                    repo,
                )
                continue

        return written

    async def search(self, query: str, top_k: int = 5) -> list[BugPattern]:
        """
        Hybrid semantic search across org BugPatterns.
        Returns [] on any error (GBrain-down safe at call site).
        Uses `gbrain query` (hybrid RRF, ~250ms p50).
        """
        try:
            raw = await _run(["query", query])
        except Exception as exc:
            logger.warning("GBrain search failed (%s), returning []", exc)
            return []

        patterns: list[BugPattern] = []
        # gbrain query returns lines like: [score] slug -- excerpt
        for line in raw.splitlines():
            m = re.match(r"\[[\d.]+\]\s+(\S+)", line)
            if not m:
                continue
            slug = m.group(1)
            try:
                page_raw = await _run(["get", slug])
                pattern = _markdown_to_pattern(slug, page_raw)
                if pattern:
                    patterns.append(pattern)
            except Exception:
                continue
            if len(patterns) >= top_k:
                break

        return patterns
