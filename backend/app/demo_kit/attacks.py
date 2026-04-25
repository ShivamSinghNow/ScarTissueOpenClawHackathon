from __future__ import annotations

import difflib
import json
import subprocess
from pathlib import Path

from app.models.schemas import Incident

from .claude import ClaudeClient
from .curation import IncidentScore

ATTACK_FILES = {
    "metadata.json",
    "attack.patch",
    "pr_title.txt",
    "pr_description.md",
    "expected_warning.md",
}


def stage_attacks(
    *,
    repo: str,
    scores: list[IncidentScore],
    output_dir: Path,
    claude: ClaudeClient | None,
    dry_run: bool,
) -> list[Path]:
    attacks_dir = output_dir / "attacks"
    attacks_dir.mkdir(parents=True, exist_ok=True)
    source_repo = ensure_source_clone(repo)
    staged: list[Path] = []

    for index, score in enumerate(scores, 1):
        incident = score.incident
        attack_dir = attacks_dir / incident.commit_sha
        if _is_complete_attack(attack_dir):
            print(f"[demo-kit] Skipping existing attack {incident.commit_sha[:12]}.")
            staged.append(attack_dir)
            continue

        print(f"[demo-kit] Generating attack {index}/{len(scores)} for {incident.commit_sha[:12]}...")
        attack_dir.mkdir(parents=True, exist_ok=True)
        target_file = choose_target_file(source_repo, incident)
        if target_file is None:
            print(f"[demo-kit] Warning: no HEAD file found for {incident.commit_sha[:12]}; skipping.")
            continue
        head_content = read_head_file(source_repo, target_file)
        generated = (
            _dry_run_attack_content(head_content, incident)
            if dry_run
            else generate_attack_content(claude or ClaudeClient(), incident, target_file, head_content)
        )
        patch = make_unified_diff(target_file, head_content, generated)
        collateral = (
            _dry_run_collateral(incident, score, target_file)
            if dry_run
            else generate_collateral(claude or ClaudeClient(), incident, score, target_file)
        )
        write_attack_files(attack_dir, repo, score, target_file, patch, collateral)
        staged.append(attack_dir)
    return staged


def ensure_source_clone(repo: str, clone_root: Path = Path("/tmp/scartissue_demo_sources")) -> Path:
    owner, name = repo.split("/", 1)
    dest = clone_root / f"{owner}_{name}"
    url = f"https://github.com/{repo}.git"
    clone_root.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"[demo-kit] Reusing source clone {dest}.")
        subprocess.run(["git", "fetch", "--all", "--prune"], cwd=dest, check=False)
        subprocess.run(["git", "checkout", _default_branch(dest)], cwd=dest, check=False)
        subprocess.run(["git", "pull", "--ff-only"], cwd=dest, check=False)
        return dest
    print(f"[demo-kit] Cloning source repo {url} to {dest}.")
    subprocess.run(["git", "clone", url, str(dest)], check=True)
    return dest


def choose_target_file(repo_path: Path, incident: Incident) -> str | None:
    for file_path in incident.files_changed:
        if file_path.startswith(("docs/", "test", "tests/")):
            continue
        if _head_file_exists(repo_path, file_path):
            return file_path
    for file_path in incident.files_changed:
        if _head_file_exists(repo_path, file_path):
            return file_path
    return None


def read_head_file(repo_path: Path, file_path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"HEAD:{file_path}"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )
    return result.stdout


def generate_attack_content(
    claude: ClaudeClient,
    incident: Incident,
    target_file: str,
    head_content: str,
) -> str:
    prompt = f"""\
Generate a realistic-looking refactor of this file that would plausibly be submitted by a new contributor, and which would re-introduce the bug that commit {incident.commit_sha} originally fixed. The refactor should look innocent, not adversarial. Output only the modified file content.

Commit message:
{incident.commit_message}

Full fix diff:
{incident.fix_diff}

Target file at HEAD: {target_file}
```text
{head_content}
```
"""
    return claude.complete(
        system="You produce only complete modified source file contents. No markdown fences.",
        prompt=prompt,
        max_tokens=12000,
        temperature=0.35,
    )


def generate_collateral(
    claude: ClaudeClient,
    incident: Incident,
    score: IncidentScore,
    target_file: str,
) -> dict[str, str]:
    response = claude.complete_json(
        system=(
            "Write realistic pull request demo collateral for a synthetic attack PR. "
            "The expected_warning must be the ScarTissue reviewer warning for the attack, "
            "not a release note: one concise paragraph explaining that the PR reintroduces "
            "the historical bug fixed by the provided commit. Return JSON only with keys "
            "pr_title, pr_description, expected_warning."
        ),
        prompt=json.dumps(
            {
                "commit_sha": incident.commit_sha,
                "commit_message": incident.commit_message,
                "target_file": target_file,
                "bug_type": score.bug_type,
                "why_juicy": score.juicy_reason,
                "instruction": (
                    "Generate collateral for a harmless-looking contributor PR that reintroduces "
                    "this old bug. The expected warning should cite the commit SHA and describe "
                    "the broken invariant or missing safeguard."
                ),
            }
        ),
        max_tokens=2500,
        temperature=0.4,
    )
    return {
        "pr_title": str(response.get("pr_title") or f"Refactor {Path(target_file).name}"),
        "pr_description": str(response.get("pr_description") or "Small cleanup with no intended behavior changes."),
        "expected_warning": str(response.get("expected_warning") or _default_expected_warning(incident, score, target_file)),
    }


def make_unified_diff(file_path: str, before: str, after: str) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="\n",
        )
    )


def write_attack_files(
    attack_dir: Path,
    repo: str,
    score: IncidentScore,
    target_file: str,
    patch: str,
    collateral: dict[str, str],
) -> None:
    incident = score.incident
    metadata = {
        "repo": repo,
        "original_incident_sha": incident.commit_sha,
        "commit_message": incident.commit_message,
        "target_file": target_file,
        "why_this_is_juicy": score.juicy_reason,
        "score": score.to_metadata(),
    }
    (attack_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (attack_dir / "attack.patch").write_text(patch, encoding="utf-8")
    (attack_dir / "pr_title.txt").write_text(collateral["pr_title"].strip() + "\n", encoding="utf-8")
    (attack_dir / "pr_description.md").write_text(collateral["pr_description"].strip() + "\n", encoding="utf-8")
    (attack_dir / "expected_warning.md").write_text(collateral["expected_warning"].strip() + "\n", encoding="utf-8")


def _is_complete_attack(attack_dir: Path) -> bool:
    return attack_dir.is_dir() and all((attack_dir / name).exists() for name in ATTACK_FILES)


def _head_file_exists(repo_path: Path, file_path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{file_path}"],
        cwd=repo_path,
        capture_output=True,
    )
    return result.returncode == 0


def _default_branch(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip().removeprefix("origin/")
    return branch or "main"


def _dry_run_attack_content(head_content: str, incident: Incident) -> str:
    marker = f"# ScarTissue dry-run attack for {incident.commit_sha[:12]}\n"
    return marker + head_content


def _dry_run_collateral(incident: Incident, score: IncidentScore, target_file: str) -> dict[str, str]:
    title = f"Refactor {Path(target_file).name} control flow"
    description = (
        "This is a generated dry-run attack PR description.\n\n"
        "The change is intended to look like a harmless cleanup while preserving the demo plan."
    )
    return {
        "pr_title": title,
        "pr_description": description,
        "expected_warning": _default_expected_warning(incident, score, target_file),
    }


def _default_expected_warning(incident: Incident, score: IncidentScore, target_file: str) -> str:
    return (
        f"ScarTissue should warn that `{target_file}` appears to reintroduce the "
        f"{score.bug_type} pattern fixed by `{incident.commit_sha}`: {score.juicy_reason}"
    )
