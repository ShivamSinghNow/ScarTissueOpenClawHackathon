from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import typer
import anthropic
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRDiff, _parse_hunks
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex

app = typer.Typer(help="Run ScarTissue against staged demo attacks.")


@app.command()
def main(
    repo: str | None = typer.Option(None, help="Repo owner/name; inferred from metadata when omitted."),
    demo_dir: Path = typer.Option(Path("demo_kit"), help="Demo package directory."),
) -> None:
    load_dotenv()
    asyncio.run(_run(repo=repo, demo_dir=demo_dir))


async def _run(repo: str | None, demo_dir: Path) -> None:
    attacks = sorted((demo_dir / "attacks").glob("*"))
    print(f"[demo-kit] Found {len(attacks)} staged attacks.")
    reviewer = _build_reviewer()
    rows = []

    for number, attack_dir in enumerate(attacks, 1):
        if not attack_dir.is_dir():
            continue
        metadata = json.loads((attack_dir / "metadata.json").read_text(encoding="utf-8"))
        attack_repo = repo or metadata.get("repo") or "unknown/repo"
        sha = metadata["original_incident_sha"]
        title = (attack_dir / "pr_title.txt").read_text(encoding="utf-8").strip()
        patch_text = (attack_dir / "attack.patch").read_text(encoding="utf-8")

        # D2 guard: warn if the patch is a no-op or contains a leftover code
        # fence (signature of the broken attack generator). These attacks are
        # not testing what they claim to test, so flag them in the row.
        is_noop = not patch_text.strip()
        has_fence = "+```" in patch_text
        pr_diff = _build_pr_diff(attack_dir, attack_repo, number, title, sha, patch_text)
        print(f"[demo-kit] Reviewing staged attack {sha[:12]}: {title}")

        started = time.perf_counter()
        warnings = await reviewer.review(pr_diff)
        latency = time.perf_counter() - started
        caught = bool(warnings)
        sha_match = any(
            w.matched_incident and w.matched_incident.commit_sha == sha for w in warnings
        )
        # Stricter correctness: the warning's explanation should reference the
        # original bug, not just happen to cite the right sha while flagging
        # a syntax error introduced by a corrupted attack patch. We require
        # the matching warning's explanation to overlap with key tokens from
        # the original commit subject.
        explanation_aligned = _explanation_matches_incident(
            warnings, sha, metadata.get("commit_message", "")
        )

        rows.append(
            {
                "repo": attack_repo,
                "attack_sha": sha,
                "pr_title": title,
                "caught": caught,
                "matched_correct_commit": sha_match,
                "explanation_aligned": explanation_aligned,
                "patch_noop": is_noop,
                "patch_has_leftover_fence": has_fence,
                "top_warning_text": warnings[0].explanation if warnings else "",
                "latency_seconds": round(latency, 3),
            }
        )

    _print_table(rows)
    (demo_dir / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"[demo-kit] Wrote results to {demo_dir / 'results.json'}.")


def _build_reviewer() -> Reviewer:
    try:
        return Reviewer()
    except TypeError:
        return Reviewer(
            scar_index=ScarIndex(),
            nia=NiaClient(),
            anthropic_client=anthropic.AsyncAnthropic(),
        )


def _build_pr_diff(
    attack_dir: Path,
    repo: str,
    number: int,
    title: str,
    sha: str,
    raw_diff: str | None = None,
) -> PRDiff:
    if raw_diff is None:
        raw_diff = (attack_dir / "attack.patch").read_text(encoding="utf-8")
    hunks = _parse_hunks(raw_diff)
    files_changed = list(dict.fromkeys(hunk.file_path for hunk in hunks))
    return PRDiff(
        url=f"file://{attack_dir}",
        repo=repo,
        number=number,
        title=title,
        author="demo-contributor",
        base_sha="HEAD",
        head_sha=f"demo-{sha[:12]}",
        files_changed=files_changed,
        hunks=hunks,
        raw_diff=raw_diff,
    )


_STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "to", "for", "on", "with", "by",
    "is", "was", "are", "be", "this", "that", "from", "as", "at", "it", "fix",
    "fixes", "fixed", "bugfix", "hotfix", "patch", "regression", "feat",
    "refactor", "core", "test", "tests",
}


def _tokens(text: str) -> set[str]:
    import re
    return {
        t.lower()
        for t in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text or "")
        if t.lower() not in _STOP_WORDS
    }


def _explanation_matches_incident(warnings, sha: str, commit_message: str) -> bool:
    """Return True iff a warning matches the right sha AND its explanation
    shares non-trivial vocabulary with the original commit subject.

    Without this check, the reviewer can score 'caught + matched_correct_commit'
    while actually flagging a different bug (e.g. a syntax error introduced by
    a corrupted attack patch) that just happens to live in the same file as
    the original fix.
    """
    subject = (commit_message or "").splitlines()[0] if commit_message else ""
    subject_tokens = _tokens(subject)
    if not subject_tokens:
        return False
    for w in warnings:
        if not (w.matched_incident and w.matched_incident.commit_sha == sha):
            continue
        if _tokens(w.explanation) & subject_tokens:
            return True
    return False


def _print_table(rows: list[dict]) -> None:
    table = Table(title="ScarTissue Demo Results")
    table.add_column("Attack SHA")
    table.add_column("PR Title")
    table.add_column("Caught")
    table.add_column("Matched")
    table.add_column("Aligned")
    table.add_column("Patch")
    table.add_column("Top Warning")
    table.add_column("Latency")

    for row in rows:
        flags = []
        if row.get("patch_noop"):
            flags.append("noop")
        if row.get("patch_has_leftover_fence"):
            flags.append("fence")
        patch_status = ", ".join(flags) if flags else "ok"
        table.add_row(
            row["attack_sha"][:12],
            row["pr_title"][:48],
            "yes" if row["caught"] else "no",
            "yes" if row["matched_correct_commit"] else "no",
            "yes" if row.get("explanation_aligned") else "no",
            patch_status,
            row["top_warning_text"][:72],
            f'{row["latency_seconds"]:.3f}s',
        )
    Console().print(table)


if __name__ == "__main__":
    app()
