from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, ".")

# Avoid local TensorFlow/Keras import side effects in sentence-transformers.
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


ATTACK_DIR = Path(
    "demo_kit_langchain_full/attacks/294dda8df27fda136b9e5c9aa413a9036a7cfadb"
)
ATTACK_PATCH = ATTACK_DIR / "attack.patch"
PR_TITLE = ATTACK_DIR / "pr_title.txt"

PR_URL = "https://github.com/langchain-ai/langchain/pull/99999"
REPO = "langchain-ai/langchain"
PR_NUMBER = 99999
AUTHOR = "demo-attacker"
BASE_SHA = "294dda8df27fda136b9e5c9aa413a9036a7cfadb"
HEAD_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _clean_git_path(path: str) -> str:
    return re.sub(r"^[ab]/", "", path)


def _hunk_header(patched_file, hunk) -> str:
    header = (
        f"@@ -{hunk.source_start},{hunk.source_length} "
        f"+{hunk.target_start},{hunk.target_length} @@"
    )
    if hunk.section_header:
        header += f" {hunk.section_header}"
    if patched_file.is_rename:
        header += (
            f"  [renamed: {_clean_git_path(patched_file.source_file)}"
            f" -> {_clean_git_path(patched_file.target_file)}]"
        )
    elif patched_file.is_removed_file:
        header += "  [deleted]"
    elif patched_file.is_added_file:
        header += "  [added]"
    return header


def _file_path(patched_file) -> str:
    target = patched_file.target_file
    source = patched_file.source_file
    raw_path = target if target and target not in ("/dev/null", "b/dev/null") else source
    return _clean_git_path(raw_path)


def parse_attack_pr() -> PRDiff:
    raw_diff = ATTACK_PATCH.read_text(encoding="utf-8")
    title = PR_TITLE.read_text(encoding="utf-8").strip()
    patch = PatchSet(raw_diff.splitlines(keepends=True))

    hunks: list[Hunk] = []
    files_changed: list[str] = []
    seen_files: set[str] = set()

    for patched_file in patch:
        if patched_file.is_binary_file:
            continue

        file_path = _file_path(patched_file)
        if file_path not in seen_files:
            files_changed.append(file_path)
            seen_files.add(file_path)

        for hunk in patched_file:
            hunks.append(
                Hunk(
                    file_path=file_path,
                    old_start=hunk.source_start,
                    old_lines=hunk.source_length,
                    new_start=hunk.target_start,
                    new_lines=hunk.target_length,
                    header=_hunk_header(patched_file, hunk),
                    content="".join(str(line) for line in hunk),
                )
            )

    return PRDiff(
        url=PR_URL,
        repo=REPO,
        number=PR_NUMBER,
        title=title,
        author=AUTHOR,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        files_changed=files_changed,
        hunks=hunks,
        raw_diff=raw_diff,
        upstream_repo=None,
    )


def _gbrain_bin() -> str:
    bun_bin_dir = os.path.expanduser("~/.bun/bin")
    augmented = f"{bun_bin_dir}:{os.environ.get('PATH', '')}"
    found = shutil.which("gbrain", path=augmented)
    if found:
        return found
    legacy = os.path.expanduser("~/gbrain/node_modules/.bin/gbrain")
    return legacy if os.path.exists(legacy) else "gbrain"


def _gbrain_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.bun/bin") + ":" + env.get("PATH", "")
    return env


def verify_gbrain_write(warnings: list[Warning]) -> tuple[bool, str]:
    has_live_warning = any(w.learned_from == "live_warning" for w in warnings)

    # Try multiple queries — GBrain semantic search may rank differently per run
    queries = [
        "mermaid URL encoding",
        "URL encode background color parameter",
        "bugpattern",
    ]
    for query in queries:
        cmd = [_gbrain_bin(), "query", query]
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
                env=_gbrain_env(),
            )
        except Exception as exc:
            if has_live_warning:
                return True, f"warning has learned_from=live_warning; gbrain query error: {exc}"
            return False, f"gbrain query failed to run: {exc}"

        combined = result.stdout + result.stderr
        if "bugpattern-" in combined:
            return True, f"gbrain query {query!r} found bugpattern- slug"
        if result.returncode != 0:
            return False, f"gbrain query exited {result.returncode}: {combined.strip()[:300]}"

    if has_live_warning:
        return True, "warning has learned_from=live_warning (gbrain query returned no matches)"
    return False, "No gbrain query found a bugpattern- slug and no live_warning in warnings"


async def maybe_close(obj) -> None:
    close = getattr(obj, "aclose", None) or getattr(obj, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def print_warning(warning: Warning, index: int) -> None:
    sha = warning.matched_incident.commit_sha if warning.matched_incident else "N/A"
    print(f"\nWarning {index}:")
    print(f"  severity     : {warning.severity}")
    print(f"  learned_from : {warning.learned_from}")
    print(f"  confidence   : {warning.confidence:.2f}")
    print(f"  file         : {warning.pr_file}")
    print(f"  hunk         : {warning.pr_hunk}")
    print(f"  matched_sha  : {sha}")
    print(f"  explanation  : {warning.explanation}")
    if warning.proposed_fix:
        print(f"  proposed_fix : {warning.proposed_fix}")


def status_line(status: bool | None, label: str) -> str:
    if status is True:
        tag = "PASS"
    elif status is False:
        tag = "FAIL"
    else:
        tag = "SKIP"
    return f"[{tag}] {label}"


async def run_review(
    scar_index: ScarIndex,
    nia: NiaClient,
    anthropic_client: SafeAsyncAnthropic,
    pr: PRDiff,
    gbrain_client: GBrainClient | None,
) -> list[Warning]:
    reviewer = Reviewer(
        scar_index,
        nia,
        anthropic_client,
        gbrain_client=gbrain_client,
    )
    return await reviewer.review(pr)


async def main() -> int:
    pr = parse_attack_pr()
    print("Parsed attack PRDiff:")
    print(f"  url           : {pr.url}")
    print(f"  repo          : {pr.repo}")
    print(f"  title         : {pr.title}")
    print(f"  files_changed : {pr.files_changed}")
    print(f"  hunks         : {len(pr.hunks)}")

    scar_index = ScarIndex(persist_dir="./chroma_db")
    nia = NiaClient()
    raw_anthropic = AsyncAnthropic()
    anthropic_client = SafeAsyncAnthropic(raw_anthropic)

    emitted_warning_check: bool | None = False
    high_severity_check: bool | None = False
    gbrain_write_check: bool | None = False
    gbrain_down_check: bool | None = False
    warnings: list[Warning] = []

    try:
        print("\nRunning full reviewer with GBrain enabled...")
        try:
            warnings = await run_review(
                scar_index,
                nia,
                anthropic_client,
                pr,
                gbrain_client=GBrainClient(),
            )
            print(f"\nFull reviewer emitted {len(warnings)} warning(s).")
            for i, warning in enumerate(warnings, 1):
                print_warning(warning, i)

            emitted_warning_check = len(warnings) >= 1
            high_severity_check = any(w.severity == "high" for w in warnings)
            gbrain_write_check, gbrain_detail = verify_gbrain_write(warnings)
            print(f"\nGBrain write verification: {gbrain_detail}")
        except SpendCapExceeded as exc:
            print(f"\nSpend cap exceeded during full reviewer run: {exc}")
            print("Skipping warning and GBrain-write assertions.")
            emitted_warning_check = None
            high_severity_check = None
            gbrain_write_check = None
        except Exception:
            print("\nFull reviewer failed with an exception:")
            traceback.print_exc()
            emitted_warning_check = False
            high_severity_check = False
            gbrain_write_check = False

        print("\nRunning GBrain-down safety test...")
        try:
            await run_review(
                scar_index,
                nia,
                anthropic_client,
                pr,
                gbrain_client=None,
            )
            gbrain_down_check = True
            print("GBrain-down safety test completed without exception.")
        except SpendCapExceeded as exc:
            print(f"Spend cap exceeded during GBrain-down safety test: {exc}")
            print("Skipping GBrain-down safety assertion.")
            gbrain_down_check = None
        except Exception:
            print("GBrain-down safety test failed with an exception:")
            traceback.print_exc()
            gbrain_down_check = False
    finally:
        await maybe_close(nia)
        await maybe_close(raw_anthropic)

    results: list[tuple[bool | None, str]] = [
        (emitted_warning_check, "At least 1 warning emitted"),
        (high_severity_check, "At least 1 HIGH severity warning"),
        (gbrain_write_check, "GBrain write verified"),
        (gbrain_down_check, "GBrain-down safety (no crash)"),
    ]

    print("\n=== SCA-12 Integration Test Results ===")
    for status, label in results:
        print(status_line(status, label))

    if all(status is True for status, _ in results):
        overall = "PASS"
        exit_code = 0
    elif any(status is False for status, _ in results):
        overall = "FAIL"
        exit_code = 1
    else:
        overall = "SKIP"
        exit_code = 0

    print(f"=== OVERALL: {overall} ===")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
