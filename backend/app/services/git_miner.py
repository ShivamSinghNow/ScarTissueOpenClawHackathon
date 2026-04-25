from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import git

from app.models.schemas import Incident

# ── detection patterns ────────────────────────────────────────────────────────

_FIRST_LINE_RE = re.compile(
    r"\b(fix|fixes|fixed|bugfix|hotfix|patch|regression|revert)\b",
    re.IGNORECASE,
)
_CLOSING_RE = re.compile(
    r"(?:fixes|closes|resolves|fix)\s+#\d+",
    re.IGNORECASE,
)
_REVERT_HEADER_RE = re.compile(r'^Revert\s+"', re.IGNORECASE)
_REVERT_BODY_RE = re.compile(r"this reverts commit", re.IGNORECASE)
_ISSUE_RE = re.compile(r"(?:fixes|closes|resolves|fix)\s+#(\d+)", re.IGNORECASE)

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

    def mine(self, repo: str, max_commits: int = 3000) -> list[Incident]:
        """Clone (or update) a repo and return all incident commits found."""
        owner, name = _parse_repo(repo)
        git_repo = self._get_repo(owner, name)

        incidents: list[Incident] = []
        processed = 0

        print(f"[miner] Walking up to {max_commits} commits…")

        for commit in git_repo.iter_commits(max_count=max_commits):
            processed += 1
            if processed % 100 == 0:
                print(
                    f"[miner] {processed} commits processed | "
                    f"{len(incidents)} incidents found so far"
                )

            message = _decode(commit.message)

            is_revert = self._is_revert(message)

            # Skip merge commits unless they are explicit reverts
            if len(commit.parents) > 1 and not is_revert:
                continue

            # Skip root commit (no parent to diff against)
            if not commit.parents:
                continue

            if not self._is_incident(message):
                continue

            parent = commit.parents[0]

            # Fast line-count check before pulling the full diff
            n_lines = self._count_changed_lines(git_repo, parent, commit)
            if n_lines > _MAX_DIFF_LINES:
                continue

            diff_text = self._get_diff(git_repo, parent, commit)

            # Skip commits with empty diffs (force-push edge case)
            if not diff_text.strip():
                continue

            try:
                files_changed = list(commit.stats.files.keys())
            except Exception:
                files_changed = []

            author = _decode(getattr(commit.author, "name", None))

            incidents.append(
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
                    issue_refs=self._extract_issue_refs(message),
                    symptom_summary=None,
                )
            )

        print(
            f"[miner] Done. {processed} commits processed | "
            f"{len(incidents)} incidents extracted."
        )
        return incidents

    # ── kept as public helpers (used by tests / routes) ────────────────────────

    def is_incident(self, message: str) -> bool:
        return self._is_incident(message)

    def extract_issue_refs(self, message: str) -> list[int]:
        return self._extract_issue_refs(message)

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

    def _is_incident(self, message: str) -> bool:
        first_line = message.splitlines()[0] if message else ""
        return (
            bool(_FIRST_LINE_RE.search(first_line))
            or bool(_CLOSING_RE.search(message))
            or self._is_revert(message)
        )

    def _is_revert(self, message: str) -> bool:
        return bool(_REVERT_HEADER_RE.match(message)) or bool(
            _REVERT_BODY_RE.search(message)
        )

    def _extract_issue_refs(self, message: str) -> list[int]:
        return [int(n) for n in _ISSUE_RE.findall(message)]

    def _count_changed_lines(
        self, repo: git.Repo, parent: git.Commit, commit: git.Commit
    ) -> int:
        """Return total insertions+deletions via --numstat (fast, no diff body)."""
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
        except Exception:
            return 0

    def _get_diff(
        self, repo: git.Repo, parent: git.Commit, commit: git.Commit
    ) -> str:
        """Return the unified diff as a UTF-8 string, truncated if needed.

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
            return f"[error getting diff: {exc}]"

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
