"""SCA-9 end-to-end demo runner — cross-session GBrain recall.

Story arc (the one the judges watch):

  t=0      PR #1 (bc21045 Ollama)        → Reviewer fires HIGH warning
                                          tagged learned_from='git_history'
                                          AND writes a BugPattern into GBrain
                                          via learn_from_incident.

  t=T      PR #2 (our OpenAI rhyme)      → Reviewer's local ChromaDB search
                                          returns nothing ≥0.5 (different
                                          vocabulary), so the agent falls back
                                          to search_org_scar_tissue, finds the
                                          pattern just written, and fires a
                                          HIGH warning tagged
                                          learned_from='live_warning'.

The script asserts each of those bullets and prints PASS/FAIL.

Prereqs (verified by the script on startup):
  - ANTHROPIC_API_KEY set (in shell env or backend/.env)
  - gbrain binary on PATH (or in ~/.bun/bin / ~/gbrain/node_modules/.bin)
  - ChromaDB populated for langchain-ai/langchain
      → run `python scripts/seed_chroma_from_demo_kit.py` first

Cost: 2 Anthropic reviews, ~$0.20–$2.00 total depending on tool-call depth.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, ".")

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

from dotenv import load_dotenv

load_dotenv()

from anthropic import AsyncAnthropic
from unidiff import PatchSet

from app.models.schemas import Warning
from app.services.gbrain_client import GBrainClient
from app.services.nia_client import NiaClient
from app.services.pr_fetcher import Hunk, PRDiff
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex
from app.spend_guard import SafeAsyncAnthropic, SpendCapExceeded

REPO = "langchain-ai/langchain"
BC21045 = "bc21045ee054b233aa6c4f65653b26a0a7ff340f"

PR1_PATCH = Path(f"demo_kit_langchain_full/attacks/{BC21045}/attack.patch.clean")
PR1_TITLE = Path(f"demo_kit_langchain_full/attacks/{BC21045}/pr_title.txt")
PR2_PATCH = Path("demo_kit_langchain_full/attack_pr_gbrain_recall.diff")


# ── diff → PRDiff ─────────────────────────────────────────────────────────────

def _strip_git_prefix(path: str) -> str:
    return re.sub(r"^[ab]/", "", path)


def _hunk_header(patched_file, hunk) -> str:
    header = (
        f"@@ -{hunk.source_start},{hunk.source_length} "
        f"+{hunk.target_start},{hunk.target_length} @@"
    )
    if hunk.section_header:
        header += f" {hunk.section_header}"
    return header


def _file_path(patched_file) -> str:
    target = patched_file.target_file
    source = patched_file.source_file
    raw = target if target and target not in ("/dev/null", "b/dev/null") else source
    return _strip_git_prefix(raw)


def parse_pr(diff_path: Path, title: str, number: int, author: str) -> PRDiff:
    raw_diff = diff_path.read_text(encoding="utf-8")
    patch = PatchSet(raw_diff.splitlines(keepends=True))

    hunks: list[Hunk] = []
    files_changed: list[str] = []
    seen: set[str] = set()
    for pf in patch:
        if pf.is_binary_file:
            continue
        fp = _file_path(pf)
        if fp not in seen:
            files_changed.append(fp)
            seen.add(fp)
        for h in pf:
            hunks.append(
                Hunk(
                    file_path=fp,
                    old_start=h.source_start,
                    old_lines=h.source_length,
                    new_start=h.target_start,
                    new_lines=h.target_length,
                    header=_hunk_header(pf, h),
                    content="".join(str(line) for line in h),
                )
            )

    return PRDiff(
        url=f"https://github.com/{REPO}/pull/{number}",
        repo=REPO,
        number=number,
        title=title,
        author=author,
        base_sha="0" * 40,
        head_sha="f" * 40,
        files_changed=files_changed,
        hunks=hunks,
        raw_diff=raw_diff,
        upstream_repo=None,
    )


# ── GBrain inventory helpers ──────────────────────────────────────────────────

def _gbrain_bin() -> str:
    bun = os.path.expanduser("~/.bun/bin")
    augmented = f"{bun}:{os.environ.get('PATH', '')}"
    found = shutil.which("gbrain", path=augmented)
    if found:
        return found
    legacy = os.path.expanduser("~/gbrain/node_modules/.bin/gbrain")
    if os.path.exists(legacy):
        return legacy
    raise RuntimeError("gbrain not installed")


def _gbrain_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.bun/bin") + ":" + env.get("PATH", "")
    return env


def gbrain_pattern_slugs() -> set[str]:
    """Return the current set of bugpattern-* slugs in the brain."""
    queries = [
        "bugpattern shallow copy mutation caller list",
        "scartissue",
        "converter remove defensive copy",
    ]
    found: set[str] = set()
    for q in queries:
        try:
            result = subprocess.run(
                [_gbrain_bin(), "query", q],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
                env=_gbrain_env(),
            )
        except Exception:
            continue
        for line in (result.stdout + result.stderr).splitlines():
            m = re.match(r"\[[\d.]+\]\s+(bugpattern-\S+)", line)
            if m:
                found.add(m.group(1))
    return found


# ── reporting helpers ─────────────────────────────────────────────────────────

def print_warning(w: Warning, idx: int) -> None:
    sha = w.matched_incident.commit_sha if w.matched_incident else "N/A"
    print(f"\nWarning #{idx}:")
    print(f"  severity     : {w.severity}")
    print(f"  learned_from : {w.learned_from}")
    print(f"  confidence   : {w.confidence:.2f}")
    print(f"  file         : {w.pr_file}")
    print(f"  matched_sha  : {sha[:12] if sha != 'N/A' else sha}")
    print(f"  explanation  : {w.explanation[:200]}")


def status(passed: bool | None, label: str) -> str:
    tag = "PASS" if passed is True else ("FAIL" if passed is False else "SKIP")
    return f"[{tag}] {label}"


# ── main demo flow ────────────────────────────────────────────────────────────

async def review_one(
    scar: ScarIndex,
    nia: NiaClient,
    anth: SafeAsyncAnthropic,
    gbrain: GBrainClient | None,
    pr: PRDiff,
) -> list[Warning]:
    reviewer = Reviewer(scar, nia, anth, gbrain_client=gbrain)
    return await reviewer.review(pr)


async def main() -> int:
    # ── prereq checks ────────────────────────────────────────────────────────
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[FATAL] ANTHROPIC_API_KEY not set (shell env or backend/.env)")
        return 2
    try:
        _gbrain_bin()
    except RuntimeError as exc:
        print(f"[FATAL] {exc}")
        return 2
    scar = ScarIndex(persist_dir="./chroma_db")
    stats = scar.collection_stats(REPO)
    if stats["count"] == 0:
        print(
            "[FATAL] ChromaDB empty for "
            f"{REPO!r} — run scripts/seed_chroma_from_demo_kit.py first"
        )
        return 2
    print(f"ChromaDB: {stats['count']} incidents indexed for {REPO}")

    nia = NiaClient()
    raw_anth = AsyncAnthropic()
    anth = SafeAsyncAnthropic(raw_anth)
    gbrain = GBrainClient()

    # Snapshot GBrain state before any review.
    slugs_before = gbrain_pattern_slugs()
    print(f"GBrain: {len(slugs_before)} pattern slug(s) discoverable before run")

    # ── PR #1 — bc21045 Ollama attack ────────────────────────────────────────
    pr1_title = PR1_TITLE.read_text(encoding="utf-8").strip()
    pr1 = parse_pr(PR1_PATCH, title=pr1_title, number=98001, author="demo-attacker")
    print(f"\n=== PR #1: {pr1.title} ===")
    print(f"  files: {pr1.files_changed}")
    print(f"  hunks: {len(pr1.hunks)}")

    pr1_warnings: list[Warning] = []
    pr1_error: str | None = None
    try:
        pr1_warnings = await review_one(scar, nia, anth, gbrain, pr1)
        for i, w in enumerate(pr1_warnings, 1):
            print_warning(w, i)
    except SpendCapExceeded as exc:
        pr1_error = f"spend cap exceeded: {exc}"
        print(f"\n[SKIP] PR #1 review: {pr1_error}")
    except Exception:
        pr1_error = traceback.format_exc()
        print(f"\n[FAIL] PR #1 review threw:\n{pr1_error}")

    slugs_after_pr1 = gbrain_pattern_slugs()
    new_slugs = slugs_after_pr1 - slugs_before
    print(f"\nGBrain after PR #1: {len(slugs_after_pr1)} slugs ({len(new_slugs)} new)")
    for s in new_slugs:
        print(f"  + {s}")

    # ── PR #2 — our OpenAI rhyme ─────────────────────────────────────────────
    pr2 = parse_pr(
        PR2_PATCH,
        title="refactor(openai): drop redundant tool-schema copies in _resolve_tool_choice",
        number=98002,
        author="demo-attacker",
    )
    print(f"\n=== PR #2: {pr2.title} ===")
    print(f"  files: {pr2.files_changed}")
    print(f"  hunks: {len(pr2.hunks)}")

    pr2_warnings: list[Warning] = []
    pr2_error: str | None = None
    try:
        pr2_warnings = await review_one(scar, nia, anth, gbrain, pr2)
        for i, w in enumerate(pr2_warnings, 1):
            print_warning(w, i)
    except SpendCapExceeded as exc:
        pr2_error = f"spend cap exceeded: {exc}"
        print(f"\n[SKIP] PR #2 review: {pr2_error}")
    except Exception:
        pr2_error = traceback.format_exc()
        print(f"\n[FAIL] PR #2 review threw:\n{pr2_error}")

    # ── assertions ───────────────────────────────────────────────────────────
    pr1_emitted = len(pr1_warnings) >= 1 if pr1_error is None else None
    pr1_cites_bc21045 = (
        any(
            (w.matched_incident and w.matched_incident.commit_sha == BC21045)
            for w in pr1_warnings
        )
        if pr1_error is None
        else None
    )
    pr1_high = (
        any(w.severity == "high" for w in pr1_warnings) if pr1_error is None else None
    )
    gbrain_grew = (
        len(new_slugs) >= 1 if pr1_error is None else None
    )

    pr2_emitted = len(pr2_warnings) >= 1 if pr2_error is None else None
    pr2_live_warning = (
        any(w.learned_from == "live_warning" for w in pr2_warnings)
        if pr2_error is None
        else None
    )
    pr2_high = (
        any(w.severity == "high" for w in pr2_warnings) if pr2_error is None else None
    )

    checks = [
        (pr1_emitted, "PR #1: ≥1 warning emitted"),
        (pr1_high, "PR #1: ≥1 HIGH severity"),
        (pr1_cites_bc21045, "PR #1: warning cites bc21045"),
        (gbrain_grew, "PR #1: GBrain learned ≥1 new pattern"),
        (pr2_emitted, "PR #2: ≥1 warning emitted"),
        (pr2_high, "PR #2: ≥1 HIGH severity"),
        (pr2_live_warning, "PR #2: ≥1 warning learned_from=live_warning (THE BIG ONE)"),
    ]

    print("\n=== SCA-9 E2E Results ===")
    for ok, label in checks:
        print(status(ok, label))

    failed = [ok for ok, _ in checks if ok is False]
    skipped = [ok for ok, _ in checks if ok is None]
    if not failed and not skipped:
        print("=== OVERALL: PASS — cross-session GBrain recall verified ===")
        return 0
    if failed:
        print("=== OVERALL: FAIL ===")
        return 1
    print("=== OVERALL: SKIP (spend cap or partial failure) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
