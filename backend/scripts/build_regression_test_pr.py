"""
Regression test: build a synthetic "reintroduce the bug" PR from a known fix commit
and verify the reviewer emits at least one warning.

Usage (from backend/):
    python -m scripts.build_regression_test_pr
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path

# Allow running directly or as a module from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import logging
import anthropic

from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRDiff, _parse_hunks, _unique_files
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FIX_SHA = "cdbe6c34f97415bcc182a130e02d18e3f678fd15"
REPO = "langchain-ai/langchain"
CLONE_PATH = Path("/tmp/scartissue/langchain-ai_langchain")

# ── diff inversion ────────────────────────────────────────────────────────────

_HUNK_RE = re.compile(r"^(@@ -)(\d+)(?:,(\d+))?( \+)(\d+)(?:,(\d+))?( @@.*)")


def _invert_hunk_header(line: str) -> str:
    """@@ -old_start,old_lines +new_start,new_lines @@ → swap old ↔ new."""
    m = _HUNK_RE.match(line.rstrip("\n"))
    if not m:
        return line
    _, os_, ol, _, ns, nl, rest = m.groups()
    ol = ol if ol is not None else "1"
    nl = nl if nl is not None else "1"
    return f"@@ -{ns},{nl} +{os_},{ol} {rest.lstrip()}\n"


def invert_diff(diff_text: str) -> str:
    """
    Invert a unified diff so it represents undoing the change:
    - Swap + and - content lines (skipping +++ / --- file headers)
    - Swap old/new ranges in @@ hunk headers
    - Leave diff --git, index, --- a/…, +++ b/… headers unchanged
      (unidiff only needs correct hunk lines to parse; file headers are metadata)
    """
    out: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("@@"):
            out.append(_invert_hunk_header(line))
        elif line.startswith("---") or line.startswith("+++"):
            # file header lines — pass through unchanged
            out.append(line)
        elif line.startswith("+"):
            out.append("-" + line[1:])
        elif line.startswith("-"):
            out.append("+" + line[1:])
        else:
            out.append(line)
    return "".join(out)


# ── git helpers ───────────────────────────────────────────────────────────────

def get_fix_diff(fix_sha: str, clone_path: Path) -> str:
    """Return the pure unified diff for fix_sha (no commit header)."""
    result = subprocess.run(
        ["git", "show", fix_sha, "--format="],
        capture_output=True,
        cwd=clone_path,
        check=True,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    # git show --format="" still emits a leading blank line; strip it
    lines = raw.splitlines(keepends=True)
    # drop leading blank lines before first "diff --git"
    start = next((i for i, l in enumerate(lines) if l.startswith("diff --git")), 0)
    return "".join(lines[start:])


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"\n{'='*65}")
    print("ScarTissue regression test — synthetic reintroduce-bug PR")
    print(f"Fix commit : {FIX_SHA[:12]}")
    print(f"Repo       : {REPO}")
    print(f"{'='*65}\n")

    # 1. Load scar index and get the incident
    scar = ScarIndex(persist_dir="./chroma_db")
    incident = scar.get_by_sha(REPO, FIX_SHA)
    if incident is None:
        print("ERROR: incident not found in scar index. Run scar_index.py first.")
        sys.exit(1)

    print(f"Incident loaded from scar index:")
    print(f"  message     : {incident.commit_message.splitlines()[0]}")
    print(f"  buggy parent: {incident.buggy_parent_sha[:12]}")
    print(f"  files       : {incident.files_changed}\n")

    # 2. Get the fix diff from the local clone
    print(f"Getting fix diff from {CLONE_PATH} …")
    fix_diff = get_fix_diff(FIX_SHA, CLONE_PATH)
    print(f"Fix diff: {len(fix_diff.splitlines())} lines\n")

    # 3. Invert the diff
    inverted_diff = invert_diff(fix_diff)
    print("Inverted diff (first 60 lines):")
    print("─" * 60)
    for line in inverted_diff.splitlines()[:60]:
        print(line)
    print("─" * 60 + "\n")

    # 4. Parse hunks
    hunks = _parse_hunks(inverted_diff)
    files_changed = _unique_files(hunks)
    print(f"Parsed {len(hunks)} hunk(s) across {len(files_changed)} file(s): {files_changed}\n")

    if not hunks:
        print("ERROR: no hunks parsed from inverted diff — check inversion logic.")
        sys.exit(1)

    # 5. Build the synthetic PRDiff
    synthetic_pr = PRDiff(
        url=f"synthetic://regression-test/{FIX_SHA[:12]}",
        repo=REPO,
        number=99999,
        title="refactor(mistralai): simplify retry exception handling",
        author="test-contributor",
        base_sha=FIX_SHA,
        head_sha=incident.buggy_parent_sha,
        files_changed=files_changed,
        hunks=hunks,
        raw_diff=inverted_diff,
    )

    print("Synthetic PR:")
    print(f"  title  : {synthetic_pr.title}")
    print(f"  base   : {synthetic_pr.base_sha[:12]}  (the fix)")
    print(f"  head   : {synthetic_pr.head_sha[:12]}  (the buggy parent)")
    print(f"  hunks  : {len(synthetic_pr.hunks)}")
    print()

    # 6. Build reviewer
    nia = NiaClient()
    claude = anthropic.AsyncAnthropic()
    reviewer = Reviewer(scar_index=scar, nia=nia, anthropic_client=claude)

    # 7. Run the agent loop with full trace
    tool_calls: list[tuple[str, str]] = []   # (tool_name, input_summary)

    async def on_progress(msg: str) -> None:
        print(f"  [progress] {msg}")

    from app.models.schemas import Warning

    async def on_warning(w: Warning) -> None:
        sha = w.matched_incident.commit_sha[:7] if w.matched_incident else "unknown"
        print(f"\n{'★'*60}")
        print(f"  WARNING EMITTED")
        print(f"  severity   : {w.severity.upper()}  confidence={w.confidence:.2f}")
        print(f"  file       : {w.pr_file}")
        print(f"  hunk       : {w.pr_hunk}")
        print(f"  commit     : {sha}")
        print(f"  explanation: {w.explanation}")
        if w.proposed_fix:
            print(f"  proposed   : {w.proposed_fix}")
        print(f"{'★'*60}\n")

    # Monkey-patch the reviewer to also record tool calls for the trace
    original_exec = reviewer._exec_tool

    async def traced_exec(pr, name, inp):
        summary = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in inp.items())
        tool_calls.append((name, summary))
        print(f"  [tool] {name}({summary[:120]})")
        return await original_exec(pr, name, inp)

    reviewer._exec_tool = traced_exec  # type: ignore[method-assign]

    print(f"{'='*65}")
    print("Running Claude agent loop …")
    print(f"{'='*65}\n")

    t0 = time.monotonic()
    warnings = await reviewer.review(
        synthetic_pr,
        on_warning=on_warning,
        on_progress=on_progress,
    )
    elapsed = time.monotonic() - t0

    # 8. Final summary
    print(f"\n{'='*65}")
    print("FINAL SUMMARY")
    print(f"{'='*65}")
    print(f"  Elapsed   : {elapsed:.1f}s")
    print(f"  Tool calls: {len(tool_calls)}")
    for i, (name, summary) in enumerate(tool_calls, 1):
        print(f"    {i:2d}. {name}  {summary[:80]}")
    print(f"  Warnings  : {len(warnings)}")
    for i, w in enumerate(warnings, 1):
        sha = w.matched_incident.commit_sha[:7] if w.matched_incident else "N/A"
        print(f"    {i}. {w.severity.upper()} conf={w.confidence:.2f}  [{sha}]  {w.pr_file}")
        print(f"       {w.explanation}")
    if len(warnings) == 0:
        print("\n  ⚠  ZERO WARNINGS — retrieval or reviewer prompt may need tuning.")
    else:
        print(f"\n  ✓  Reviewer correctly flagged a reintroduced bug pattern.")


if __name__ == "__main__":
    asyncio.run(main())
