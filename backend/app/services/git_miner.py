from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import git

from app.models.schemas import Incident
from app.services.bugfix_classifier import (
    BugFixClassifier,
    extract_issue_refs,
    is_revert,
    keyword_classify,
)

_MAX_DIFF_CHARS = 8_000
_MAX_DIFF_LINES = 500


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_repo(repo: str) -> tuple[str, str]:
    """Return (owner, name) from 'owner/repo' or a full GitHub URL."""
    s = repo.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    parts = s.split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse repo string: {repo!r}")
    return parts[-2], parts[-1]


def _decode(value: Optional[str | bytes], fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ── main class ────────────────────────────────────────────────────────────────

class GitMiner:
    """Clones a GitHub repo shallowly and extracts incident (bug-fix) commits."""

    def __init__(self, clone_root: str = "/tmp/scartissue") -> None:
        self.clone_root = Path(clone_root)
        self.clone_root.mkdir(parents=True, exist_ok=True)

    # ── public API ─────────────────────────────────────────────────────────────

    def mine(
        self,
        repo: str,
        max_commits: int = 3000,
        classifier: BugFixClassifier | None = None,
    ) -> list[Incident]:
        """Clone (or update) a repo and return all incident commits found.

        Stage 1 keyword classifier (extended set + negative pattern subtraction)
        runs in this loop. If a classifier is passed, it then runs in batched
        mode over the candidates as a precision filter and populates
        symptom_summary on each surviving Incident.
        """
        owner, name = _parse_repo(repo)
        git_repo = self._get_repo(owner, name)

        candidates: list[Incident] = []
        processed = 0

        print(f"[miner] Walking up to {max_commits} commits…")

        for commit in git_repo.iter_commits(max_count=max_commits):
            processed += 1
            if processed % 100 == 0:
                print(
                    f"[miner] {processed} commits processed | "
                    f"{len(candidates)} candidates so far"
                )

            message = _decode(commit.message)

            is_revert_commit = is_revert(message)

            # Skip merge commits unless they are explicit reverts
            if len(commit.parents) > 1 and not is_revert_commit:
                continue

            # Skip root commit (no parent to diff against)
            if not commit.parents:
                continue

            if not keyword_classify(message):
                continue

            parent = commit.parents[0]

            # Fast line-count check before pulling the full diff. _count_changed_lines
            # returns -1 on error so we skip rather than fall through with n_lines=0
            # and silently index a commit whose real diff is far above the cap.
            n_lines = self._count_changed_lines(git_repo, parent, commit)
            if n_lines < 0 or n_lines > _MAX_DIFF_LINES:
                continue

            diff_text = self._get_diff(git_repo, parent, commit)

            # Skip empty diffs (force-push edge case) and skip commits whose
            # diff extraction failed — _get_diff returns None on failure so we
            # don't index error strings as if they were real fix diffs.
            if diff_text is None or not diff_text.strip():
                continue

            try:
                files_changed = list(commit.stats.files.keys())
            except Exception:
                files_changed = []

            author = _decode(getattr(commit.author, "name", None))

            candidates.append(
                Incident(
                    commit_sha=commit.hexsha,
                    commit_message=message,
                    commit_date=datetime.fromtimestamp(
                        commit.authored_date, tz=timezone.utc
                    ),
                    author=author,
                    files_changed=files_changed,
                    functions_changed=[],
                    fix_diff=diff_text,
                    buggy_parent_sha=parent.hexsha,
                    issue_refs=extract_issue_refs(message),
                    symptom_summary=None,
                )
            )

        print(
            f"[miner] Stage-1 keyword pass: {processed} commits processed | "
            f"{len(candidates)} candidates"
        )

        if classifier is None:
            return candidates

        triples = [(c.commit_sha, c.commit_message, c.fix_diff) for c in candidates]
        classified = classifier.classify_many(triples)

        incidents: list[Incident] = []
        rejected = 0
        for cand in candidates:
            verdict = classified.get(cand.commit_sha)
            if verdict is None or not verdict.is_bug_fix:
                rejected += 1
                continue
            # Re-construct with the LLM-supplied symptom summary so it flows
            # into the embedding text downstream.
            incidents.append(cand.model_copy(update={"symptom_summary": verdict.summary}))

        print(
            f"[miner] Stage-2 LLM pass: {len(incidents)} confirmed bug-fix commits, "
            f"{rejected} rejected as non-functional."
        )
        return incidents

    # ── kept as public helpers (used by tests / routes) ────────────────────────

    def is_incident(self, message: str) -> bool:
        return keyword_classify(message)

    def extract_issue_refs(self, message: str) -> list[int]:
        return extract_issue_refs(message)

    # ── private ────────────────────────────────────────────────────────────────

    def _get_repo(self, owner: str, name: str) -> git.Repo:
        dest = self.clone_root / f"{owner}_{name}"
        url = f"https://github.com/{owner}/{name}.git"

        if dest.exists():
            print(f"[miner] Reusing clone at {dest}")
            try:
                r = git.Repo(dest)
                print("[miner] Fetching latest commits…")
                try:
                    r.remotes.origin.fetch()
                except git.GitCommandError as e:
                    print(f"[miner] Warning: fetch failed ({e}), using existing history")
                return r
            except git.InvalidGitRepositoryError:
                print("[miner] Corrupt clone detected — removing and re-cloning…")
                shutil.rmtree(dest)

        print(f"[miner] Cloning {url} (depth=5000)…")
        return git.Repo.clone_from(url, dest, depth=5000)

    def _count_changed_lines(
        self, repo: git.Repo, parent: git.Commit, commit: git.Commit
    ) -> int:
        """Return total insertions+deletions via --numstat (fast, no diff body).

        Returns -1 if the count could not be obtained, so callers can skip the
        commit instead of treating "unknown" as "zero" and bypassing size caps.
        """
        try:
            numstat = repo.git.diff(parent.hexsha, commit.hexsha, "--numstat")
            total = 0
            for line in numstat.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0] != "-" and parts[1] != "-":
                    try:
                        total += int(parts[0]) + int(parts[1])
                    except ValueError:
                        pass
            return total
        except Exception as exc:
            print(f"[miner] numstat failed for {commit.hexsha[:12]}: {exc}")
            return -1

    def _get_diff(
        self, repo: git.Repo, parent: git.Commit, commit: git.Commit
    ) -> str | None:
        """Return the unified diff as a UTF-8 string, truncated if needed.

        Returns None when extraction fails so the caller can skip the commit.
        Previously this returned an "[error getting diff: …]" string that then
        got embedded as if it were a real fix.

        Uses subprocess so that binary-content and encoding edge cases are
        handled by explicit decode(errors='replace') rather than GitPython's
        internal heuristics.
        """
        try:
            result = subprocess.run(
                ["git", "diff", "--unified=3", parent.hexsha, commit.hexsha],
                capture_output=True,
                cwd=repo.working_dir,
            )
            raw = result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[miner] diff failed for {commit.hexsha[:12]}: {exc}")
            return None

        # Mark binary file lines legibly
        lines: list[str] = []
        for line in raw.splitlines(keepends=True):
            if line.startswith("Binary files "):
                lines.append("[binary file]\n")
            else:
                lines.append(line)
        raw = "".join(lines)

        if len(raw) > _MAX_DIFF_CHARS:
            raw = raw[:_MAX_DIFF_CHARS] + "\n... [truncated]"

        return raw


# ── quick sanity-check ────────────────────────────────────────────────────────

if __name__ == "__main__":
    miner = GitMiner()
    results = miner.mine("langchain-ai/langchain", max_commits=500)

    print(f"\n{'=' * 60}")
    print(f"Total incidents found: {len(results)}")
    print(f"{'=' * 60}")

    for i, inc in enumerate(results[:3], 1):
        print(f"\n--- Incident {i} ---")
        print(f"SHA:     {inc.commit_sha[:12]}")
        print(f"Author:  {inc.author}")
        print(f"Date:    {inc.commit_date.strftime('%Y-%m-%d')}")
        print(f"Message: {inc.commit_message.splitlines()[0][:80]}")
        print(f"Files:   {inc.files_changed[:5]}")
        print(f"Issues:  {inc.issue_refs}")
        diff_head = inc.fix_diff[:400].replace("\n", "\n         ")
        print(f"Diff:\n         {diff_head}")
