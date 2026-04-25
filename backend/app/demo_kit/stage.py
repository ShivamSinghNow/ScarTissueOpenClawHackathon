from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv

from .attacks import stage_attacks
from .claude import ClaudeClient
from .curation import curate_incidents, load_or_mine_incidents, write_curation_plan
from .setup_script import write_setup_script

app = typer.Typer(help="Stage ScarTissue wow-moment demo attacks.")


@app.command()
def main(
    repo: str = typer.Argument(..., help="GitHub repo as owner/name."),
    max_incidents: int = typer.Option(10, help="Number of top incidents to keep."),
    max_commits: int = typer.Option(3000, help="Maximum commits for initial mining."),
    skip_mining: bool = typer.Option(False, help="Use an already-populated scar index."),
    dry_run: bool = typer.Option(False, help="Skip Claude calls and produce a deterministic plan."),
    output_dir: Path = typer.Option(Path("demo_kit"), help="Output package directory."),
) -> None:
    load_dotenv()
    print(f"[demo-kit] Staging demo package for {repo}.")
    output_dir.mkdir(parents=True, exist_ok=True)
    incidents = load_or_mine_incidents(repo, max_commits=max_commits, skip_mining=skip_mining)
    if not incidents:
        typer.echo("[demo-kit] No indexed incidents found; run without --skip-mining first.")
        raise typer.Exit(1)

    claude = None if dry_run else ClaudeClient()
    scores = curate_incidents(
        repo=repo,
        incidents=incidents,
        claude=claude,
        max_incidents=max_incidents,
        dry_run=dry_run,
        ratings_cache_path=output_dir / "claude_ratings.json",
    )
    write_curation_plan(output_dir, scores)
    staged = stage_attacks(repo=repo, scores=scores, output_dir=output_dir, claude=claude, dry_run=dry_run)
    write_setup_script(output_dir, repo)
    print(f"[demo-kit] Complete. Staged {len(staged)} attacks under {output_dir / 'attacks'}.")


if __name__ == "__main__":
    app()
