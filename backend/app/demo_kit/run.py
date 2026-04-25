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
        pr_diff = _build_pr_diff(attack_dir, attack_repo, number, title, sha)
        print(f"[demo-kit] Reviewing staged attack {sha[:12]}: {title}")

        started = time.perf_counter()
        warnings = await reviewer.review(pr_diff)
        latency = time.perf_counter() - started
        caught = bool(warnings)
        correct = any(w.matched_incident.commit_sha == sha for w in warnings)
        top_warning = warnings[0].explanation if warnings else ""

        rows.append(
            {
                "repo": attack_repo,
                "attack_sha": sha,
                "pr_title": title,
                "caught": caught,
                "matched_correct_commit": correct,
                "top_warning_text": top_warning,
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
) -> PRDiff:
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


def _print_table(rows: list[dict]) -> None:
    table = Table(title="ScarTissue Demo Results")
    table.add_column("Attack SHA")
    table.add_column("PR Title")
    table.add_column("Caught")
    table.add_column("Matched")
    table.add_column("Top Warning")
    table.add_column("Latency")

    for row in rows:
        table.add_row(
            row["attack_sha"][:12],
            row["pr_title"][:48],
            "yes" if row["caught"] else "no",
            "yes" if row["matched_correct_commit"] else "no",
            row["top_warning_text"][:72],
            f'{row["latency_seconds"]:.3f}s',
        )
    Console().print(table)


if __name__ == "__main__":
    app()
