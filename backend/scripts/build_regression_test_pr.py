"""
Regression test: build a synthetic "reintroduce the bug" PR from a known fix
commit (by inverting its diff) and verify the reviewer emits at least one
warning whose matched_incident.commit_sha equals the fix sha.

Usage (from backend/):
    python -m scripts.build_regression_test_pr <repo> <fix_sha> [--clone-path PATH]

Examples:
    python -m scripts.build_regression_test_pr encode/httpx 89599a9b...
    python -m scripts.build_regression_test_pr langchain-ai/langchain cdbe6c34f...

Exits non-zero if the reviewer fails to emit a warning matching the fix sha,
so this can drive CI / Phase C measurement loops.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Allow running directly or as a module from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import anthropic

from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRDiff, _parse_hunks, _unique_files
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex
from app.models.schemas import Warning

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


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
    """Invert a unified diff so it represents undoing the change."""
    out: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("@@"):
            out.append(_invert_hunk_header(line))
        elif line.startswith("---") or line.startswith("+++"):
            out.append(line)
        elif line.startswith("+"):
            out.append("-" + line[1:])
        elif line.startswith("-"):
            out.append("+" + line[1:])
        else:
            out.append(line)
    return "".join(out)


# ── git helpers ───────────────────────────────────────────────────────────────


def _default_clone_path(repo: str) -> Path:
    safe = repo.replace("/", "_")
    return Path("/tmp/scartissue") / safe


def get_fix_diff(fix_sha: str, clone_path: Path) -> str:
    """Return the pure unified diff for fix_sha (no commit header)."""
    result = subprocess.run(
        ["git", "show", fix_sha, "--format="],
        capture_output=True,
        cwd=clone_path,
        check=True,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    lines = raw.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.startswith("diff --git")), 0)
    return "".join(lines[start:])


# ── main ──────────────────────────────────────────────────────────────────────


async def _run(repo: str, fix_sha: str, clone_path: Path, persist_dir: str) -> int:
    print(f"\n{'='*65}")
    print("ScarTissue regression test — synthetic reintroduce-bug PR")
    print(f"Fix commit : {fix_sha[:12]}")
    print(f"Repo       : {repo}")
    print(f"Clone      : {clone_path}")
    print(f"{'='*65}\n")

    if not (clone_path / ".git").exists():
        print(f"ERROR: no git clone at {clone_path}. Clone the repo first.")
        return 2

    scar = ScarIndex(persist_dir=persist_dir)
    incident = scar.get_by_sha(repo, fix_sha)
    if incident is None:
        print(f"ERROR: incident {fix_sha[:12]} not in scar index for {repo}.")
        print("Run /index for this repo first.")
        return 2

    print(f"Incident loaded:")
    print(f"  message     : {incident.commit_message.splitlines()[0][:100]}")
    print(f"  buggy parent: {incident.buggy_parent_sha[:12]}")
    print(f"  files       : {incident.files_changed[:5]}\n")

    print(f"Getting fix diff from {clone_path} …")
    try:
        fix_diff = get_fix_diff(fix_sha, clone_path)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: git show failed: {exc.stderr.decode('utf-8', errors='replace')}")
        return 2
    print(f"Fix diff: {len(fix_diff.splitlines())} lines\n")

    inverted_diff = invert_diff(fix_diff)
    hunks = _parse_hunks(inverted_diff)
    files_changed = _unique_files(hunks)
    print(f"Inverted diff: {len(hunks)} hunk(s) across {len(files_changed)} file(s)\n")

    if not hunks:
        print("ERROR: no hunks parsed from inverted diff — check inversion logic.")
        return 2

    synthetic_pr = PRDiff(
        url=f"synthetic://regression-test/{fix_sha[:12]}",
        repo=repo,
        number=99999,
        title=f"reintroduce: undo fix {fix_sha[:12]}",
        author="regression-harness",
        base_sha=fix_sha,
        head_sha=incident.buggy_parent_sha,
        files_changed=files_changed,
        hunks=hunks,
        raw_diff=inverted_diff,
    )

    nia = NiaClient()
    claude = anthropic.AsyncAnthropic()
    reviewer = Reviewer(scar_index=scar, nia=nia, anthropic_client=claude)

    tool_calls: list[tuple[str, str]] = []

    async def on_progress(msg: str) -> None:
        print(f"  [progress] {msg}")

    async def on_warning(w: Warning) -> None:
        sha = w.matched_incident.commit_sha[:7] if w.matched_incident else "unknown"
        marker = " ★" if w.matched_incident and w.matched_incident.commit_sha == fix_sha else ""
        print(f"\n  [warning]{marker} {w.severity.upper()} conf={w.confidence:.2f} sha={sha}")
        print(f"            {w.explanation[:150]}")

    original_exec = reviewer._exec_tool

    async def traced_exec(pr, name, inp, observed_similarity=None):
        summary = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in inp.items())
        tool_calls.append((name, summary))
        print(f"  [tool] {name}({summary[:120]})")
        return await original_exec(pr, name, inp, observed_similarity=observed_similarity)

    reviewer._exec_tool = traced_exec  # type: ignore[method-assign]

    print(f"{'='*65}")
    print("Running review …")
    print(f"{'='*65}\n")

    t0 = time.monotonic()
    warnings = await reviewer.review(
        synthetic_pr,
        on_warning=on_warning,
        on_progress=on_progress,
    )
    elapsed = time.monotonic() - t0

    matched = [w for w in warnings if w.matched_incident and w.matched_incident.commit_sha == fix_sha]

    print(f"\n{'='*65}")
    print("FINAL")
    print(f"{'='*65}")
    print(f"  Elapsed     : {elapsed:.1f}s")
    print(f"  Tool calls  : {len(tool_calls)}")
    print(f"  Warnings    : {len(warnings)}")
    print(f"  Matched fix : {len(matched)}")
    for i, w in enumerate(warnings, 1):
        sha = w.matched_incident.commit_sha[:7] if w.matched_incident else "N/A"
        marker = " ★" if w in matched else ""
        print(f"    {i}. {w.severity.upper()} conf={w.confidence:.2f} [{sha}]{marker} {w.pr_file}")

    if not matched:
        print("\n  ✗  Reviewer did NOT match the fix commit — retrieval or prompt may need tuning.")
        return 1
    print(f"\n  ✓  Reviewer correctly flagged the reintroduced fix ({len(matched)} matching warning(s)).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="GitHub repo as owner/name")
    parser.add_argument("fix_sha", help="Full commit sha of the fix to invert")
    parser.add_argument(
        "--clone-path",
        type=Path,
        help="Local clone of the repo. Defaults to /tmp/scartissue/<owner>_<name>",
    )
    parser.add_argument(
        "--persist-dir",
        default=os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db"),
        help="ChromaDB persistence directory (default: $CHROMA_PERSIST_DIR or ./chroma_db)",
    )
    args = parser.parse_args()

    clone_path = args.clone_path or _default_clone_path(args.repo)
    code = asyncio.run(_run(args.repo, args.fix_sha, clone_path, args.persist_dir))
    sys.exit(code)


if __name__ == "__main__":
    main()
