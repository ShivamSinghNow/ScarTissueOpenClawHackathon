from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import httpx
import unidiff
from github import Github

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)
_MAX_DIFF_LINES = 3000


@dataclass
class Hunk:
    file_path: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str     # the @@ line
    content: str    # hunk body including +/- lines


@dataclass
class PRDiff:
    url: str
    repo: str                       # "owner/repo" — the slug the PR URL points at
    number: int
    title: str
    author: str
    base_sha: str
    head_sha: str
    files_changed: list[str]
    hunks: list[Hunk]
    raw_diff: str                   # full unified diff
    upstream_repo: str | None = None  # set when `repo` is a fork; the root ancestor


class PRFetcher:
    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN")
        self._gh = Github(self._token)
        headers = {"Accept": "application/vnd.github.v3.diff"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._http = httpx.Client(headers=headers, timeout=30.0)

    def parse_url(self, url: str) -> tuple[str, int]:
        """Returns (owner/repo, pr_number). Accepts github.com URLs."""
        m = _PR_URL_RE.search(url)
        if not m:
            raise ValueError(f"Cannot parse PR URL: {url!r}")
        return f"{m['owner']}/{m['repo']}", int(m["number"])

    def fetch(self, url: str) -> PRDiff:
        repo_slug, number = self.parse_url(url)
        owner, repo_name = repo_slug.split("/", 1)

        # Metadata via PyGithub
        gh_repo = self._gh.get_repo(repo_slug)
        pr = gh_repo.get_pull(number)

        # If this repo is a fork, resolve to the root ancestor so scar-tissue
        # lookups hit the upstream's index instead of the (un-indexed) fork.
        upstream_repo: str | None = None
        if gh_repo.fork:
            root = gh_repo.source or gh_repo.parent
            if root is not None and root.full_name.lower() != repo_slug.lower():
                upstream_repo = root.full_name

        # Unified diff via REST API with vnd.github.v3.diff
        diff_url = f"https://api.github.com/repos/{repo_slug}/pulls/{number}"
        resp = self._http.get(diff_url)
        resp.raise_for_status()
        raw_diff = resp.text

        # Enforce line limit
        lines = raw_diff.splitlines(keepends=True)
        if len(lines) > _MAX_DIFF_LINES:
            logger.warning(
                "PR #%d diff is %d lines; truncating to %d",
                number, len(lines), _MAX_DIFF_LINES,
            )
            raw_diff = "".join(lines[:_MAX_DIFF_LINES]) + "\n# [diff truncated]\n"

        hunks = _parse_hunks(raw_diff)
        files_changed = _unique_files(hunks)

        return PRDiff(
            url=url,
            repo=repo_slug,
            number=number,
            title=pr.title,
            author=pr.user.login,
            base_sha=pr.base.sha,
            head_sha=pr.head.sha,
            files_changed=files_changed,
            hunks=hunks,
            raw_diff=raw_diff,
            upstream_repo=upstream_repo,
        )


def _parse_hunks(raw_diff: str) -> list[Hunk]:
    try:
        patch = unidiff.PatchSet.from_string(raw_diff)
    except unidiff.errors.UnidiffParseError as exc:
        logger.warning("Could not parse diff: %s", exc)
        return []

    hunks: list[Hunk] = []
    for patched_file in patch:
        if patched_file.is_binary_file:
            continue

        # Resolve display path: prefer target, fall back to source (deleted files)
        target = patched_file.target_file
        source = patched_file.source_file

        if target and target not in ("/dev/null", "b/dev/null"):
            raw_path = target
        else:
            raw_path = source

        # Strip git's a/ b/ prefixes
        file_path = re.sub(r"^[ab]/", "", raw_path)

        # Note renames in the path string
        is_rename = patched_file.is_rename
        is_deleted = patched_file.is_removed_file
        is_added = patched_file.is_added_file

        for uh in patched_file:  # each Hunk in the PatchedFile
            header_line = (
                f"@@ -{uh.source_start},{uh.source_length} "
                f"+{uh.target_start},{uh.target_length} @@"
            )
            if uh.section_header:
                header_line += f" {uh.section_header}"
            if is_rename:
                src_clean = re.sub(r"^[ab]/", "", source)
                tgt_clean = re.sub(r"^[ab]/", "", target)
                header_line += f"  [renamed: {src_clean} → {tgt_clean}]"
            elif is_deleted:
                header_line += "  [deleted]"
            elif is_added:
                header_line += "  [added]"

            content = "".join(str(line) for line in uh)

            hunks.append(Hunk(
                file_path=file_path,
                old_start=uh.source_start,
                old_lines=uh.source_length,
                new_start=uh.target_start,
                new_lines=uh.target_length,
                header=header_line,
                content=content,
            ))

    return hunks


def _unique_files(hunks: list[Hunk]) -> list[str]:
    seen: dict[str, None] = {}
    for h in hunks:
        seen[h.file_path] = None
    return list(seen)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    TEST_PR = "https://github.com/langchain-ai/langchain/pull/35238"
    print(f"\nFetching {TEST_PR} …\n")

    fetcher = PRFetcher()
    result = fetcher.fetch(TEST_PR)

    print(f"Title  : {result.title}")
    print(f"Author : {result.author}")
    print(f"Repo   : {result.repo}  PR #{result.number}")
    print(f"Base   : {result.base_sha[:12]}  Head: {result.head_sha[:12]}")
    print(f"Files  : {len(result.files_changed)}")
    print(f"Hunks  : {len(result.hunks)}")
    print(f"Diff   : {len(result.raw_diff)} chars")

    if result.hunks:
        h = result.hunks[0]
        print(f"\nFirst hunk")
        print(f"  File   : {h.file_path}")
        print(f"  Header : {h.header}")
        print(f"  Lines  : {len(h.content.splitlines())} content lines")
