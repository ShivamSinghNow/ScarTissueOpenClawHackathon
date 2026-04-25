from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import httpx

from app.models.schemas import Incident

from .claude import ClaudeClient

_VISCERAL_TYPES = {
    "async bug",
    "race condition",
    "memory leak",
    "retry/idempotency issue",
    "streaming glitch",
    "auth failure",
    "off-by-one",
}


class IssueStatusResolver(Protocol):
    def __call__(self, repo: str, issue_number: int) -> str | None: ...


@dataclass(frozen=True)
class IncidentScore:
    incident: Incident
    demo_score: float
    clarity_score: float
    issue_closed: bool
    focused_fix: bool
    recent_fix: bool
    bug_type: str
    visceral: bool
    juicy_reason: str
    changed_lines: int

    def to_metadata(self) -> dict:
        return {
            "demo_score": round(self.demo_score, 3),
            "clarity_score": self.clarity_score,
            "issue_closed": self.issue_closed,
            "focused_fix": self.focused_fix,
            "recent_fix": self.recent_fix,
            "bug_type": self.bug_type,
            "visceral": self.visceral,
            "juicy_reason": self.juicy_reason,
            "changed_lines": self.changed_lines,
        }


def curate_incidents(
    *,
    repo: str,
    incidents: list[Incident],
    claude: ClaudeClient | None,
    max_incidents: int,
    dry_run: bool,
    issue_resolver: IssueStatusResolver | None = None,
    ratings_cache_path: Path | None = None,
) -> list[IncidentScore]:
    print(f"[demo-kit] Scoring {len(incidents)} incidents for demo value...")
    claude_ratings = (
        _dry_run_ratings(incidents)
        if dry_run
        else _rate_with_claude(incidents, claude, ratings_cache_path=ratings_cache_path)
    )
    resolver = issue_resolver or resolve_github_issue_state

    scored: list[IncidentScore] = []
    now = datetime.now(tz=timezone.utc)
    for incident in incidents:
        rating = claude_ratings.get(incident.commit_sha, {})
        clarity = _coerce_float(rating.get("clarity_score"), default=5.0)
        bug_type = str(rating.get("bug_type") or "other").strip().lower()
        visceral = bool(rating.get("visceral")) or bug_type in _VISCERAL_TYPES
        issue_closed = _has_closed_issue(repo, incident, resolver)
        changed_lines = count_changed_lines(incident.fix_diff)
        focused = 1 <= len(incident.files_changed) <= 3 and changed_lines < 200
        recent = _as_aware_utc(incident.commit_date) >= now - timedelta(days=730)

        score = 0.0
        score += (clarity / 10.0) * 30.0
        score += 15.0 if issue_closed else 0.0
        score += 20.0 if focused else 0.0
        score += 10.0 if recent else 0.0
        score += 25.0 if visceral else 0.0

        reason = str(rating.get("juicy_reason") or "").strip()
        if not reason:
            reason = _fallback_juicy_reason(incident, bug_type, issue_closed, focused, recent)

        scored.append(
            IncidentScore(
                incident=incident,
                demo_score=score,
                clarity_score=clarity,
                issue_closed=issue_closed,
                focused_fix=focused,
                recent_fix=recent,
                bug_type=bug_type,
                visceral=visceral,
                juicy_reason=reason,
                changed_lines=changed_lines,
            )
        )

    scored.sort(key=lambda item: item.demo_score, reverse=True)
    top = scored[:max_incidents]
    print(f"[demo-kit] Curated top {len(top)} incidents.")
    for rank, item in enumerate(top, 1):
        first = item.incident.commit_message.splitlines()[0][:90]
        print(f"[demo-kit] {rank:02d}. {item.incident.commit_sha[:12]} score={item.demo_score:.1f} {first}")
    return top


def load_or_mine_incidents(repo: str, *, max_commits: int, skip_mining: bool) -> list[Incident]:
    from app.services.git_miner import GitMiner
    from app.services.scar_index import ScarIndex

    if not skip_mining:
        print(f"[demo-kit] Mining {repo} for up to {max_commits} commits...")
        incidents = GitMiner().mine(repo, max_commits=max_commits)
        print(f"[demo-kit] Indexing {len(incidents)} incidents into ScarIndex...")
        ScarIndex().index_incidents(repo, incidents)

    incidents = load_all_indexed_incidents(repo)
    print(f"[demo-kit] Loaded {len(incidents)} incidents from scar index.")
    return incidents


def load_all_indexed_incidents(repo: str, persist_dir: str | None = None) -> list[Incident]:
    import chromadb

    persist = persist_dir or os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
    client = chromadb.PersistentClient(path=persist)
    collection_name = repo.replace("/", "_").replace("-", "_").lower()
    try:
        collection = client.get_collection(collection_name)
    except Exception:
        return []

    count = collection.count()
    if count == 0:
        return []

    result = collection.get(limit=count, include=["metadatas"])
    incidents: list[Incident] = []
    for metadata in result.get("metadatas") or []:
        try:
            incidents.append(Incident.model_validate_json(metadata["incident_json"]))
        except Exception:
            continue
    return incidents


def write_curation_plan(output_dir: Path, scores: list[IncidentScore]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = [
        {"commit_sha": score.incident.commit_sha, **score.to_metadata()}
        for score in scores
    ]
    (output_dir / "curation_plan.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def count_changed_lines(diff_text: str) -> int:
    total = 0
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            total += 1
    return total


def resolve_github_issue_state(repo: str, issue_number: int) -> str | None:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    try:
        response = httpx.get(url, headers=headers, timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return str(response.json().get("state") or "")
    except Exception as exc:
        print(f"[demo-kit] Warning: could not resolve issue #{issue_number}: {exc}")
        return None


def _rate_with_claude(
    incidents: list[Incident],
    claude: ClaudeClient | None,
    *,
    ratings_cache_path: Path | None,
) -> dict[str, dict]:
    if claude is None:
        claude = ClaudeClient()

    ratings: dict[str, dict] = _load_rating_cache(ratings_cache_path)
    batch_size = 6
    for start in range(0, len(incidents), batch_size):
        batch = incidents[start : start + batch_size]
        missing = [incident for incident in batch if incident.commit_sha not in ratings]
        if not missing:
            print(f"[demo-kit] Reusing cached Claude ratings {start + 1}-{start + len(batch)}...")
            continue
        print(f"[demo-kit] Asking Claude to rate incidents {start + 1}-{start + len(batch)}...")
        payload = [
            {
                "commit_sha": incident.commit_sha,
                "commit_message": incident.commit_message[:700],
                "files_changed": incident.files_changed,
                "fix_diff_excerpt": incident.fix_diff[:1000],
            }
            for incident in missing
        ]
        response = claude.complete_json(
            system=(
                "You score bug-fix commits for live demos. Return JSON only: "
                '{"ratings":[{"commit_sha":"...","clarity_score":0-10,'
                '"bug_type":"async bug|race condition|memory leak|retry/idempotency issue|'
                'streaming glitch|auth failure|off-by-one|other","visceral":true,'
                '"juicy_reason":"one punchy sentence"}]}.'
            ),
            prompt=json.dumps({"incidents": payload}),
            max_tokens=1800,
        )
        for item in response.get("ratings", []):
            sha = item.get("commit_sha")
            if sha:
                ratings[str(sha)] = item
        _write_rating_cache(ratings_cache_path, ratings)
    return ratings


def _load_rating_cache(path: Path | None) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(key): value for key, value in data.items() if isinstance(value, dict)}
    except Exception as exc:
        print(f"[demo-kit] Warning: ignoring unreadable Claude rating cache {path}: {exc}")
    return {}


def _write_rating_cache(path: Path | None, ratings: dict[str, dict]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ratings, indent=2), encoding="utf-8")


def _dry_run_ratings(incidents: list[Incident]) -> dict[str, dict]:
    ratings = {}
    for incident in incidents:
        first = incident.commit_message.splitlines()[0].lower()
        bug_type = "other"
        for candidate in _VISCERAL_TYPES:
            if any(part in first for part in re.split(r"[/ -]", candidate)):
                bug_type = candidate
                break
        ratings[incident.commit_sha] = {
            "clarity_score": 7 if len(first.split()) >= 4 else 4,
            "bug_type": bug_type,
            "visceral": bug_type != "other",
            "juicy_reason": _fallback_juicy_reason(incident, bug_type, bool(incident.issue_refs), True, True),
        }
    return ratings


def _has_closed_issue(
    repo: str,
    incident: Incident,
    resolver: IssueStatusResolver,
) -> bool:
    for issue in incident.issue_refs:
        if resolver(repo, issue) == "closed":
            return True
    return False


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return default


def _fallback_juicy_reason(
    incident: Incident,
    bug_type: str,
    issue_closed: bool,
    focused: bool,
    recent: bool,
) -> str:
    traits = []
    if bug_type != "other":
        traits.append(bug_type)
    if issue_closed:
        traits.append("closed user-visible issue")
    if focused:
        traits.append("small focused fix")
    if recent:
        traits.append("recent regression-shaped change")
    joined = ", ".join(traits) if traits else "clear historical bug pattern"
    return f"{incident.commit_sha[:12]} is demo-worthy because it combines {joined}."
