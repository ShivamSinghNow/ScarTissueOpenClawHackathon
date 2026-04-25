from __future__ import annotations

import os
import re
import sys
from typing import Any

from fastapi import APIRouter, HTTPException
from github import Github, GithubException
from pydantic import BaseModel

from app.models.schemas import Warning

router = APIRouter()


class PostToGithubRequest(BaseModel):
    pr_url: str
    warnings: list[Warning]
    dry_run: bool = False


class PostedComment(BaseModel):
    pr_file: str
    line: int
    body_preview: str
    anchored: bool


class PostToGithubResponse(BaseModel):
    pr_url: str
    review_url: str | None
    total_comments: int
    posted: list[PostedComment]
    summary_comment: str
    skipped_duplicates: int = 0


_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<len>\d+))? @@")


def _parse_pr_url(pr_url: str) -> tuple[str, int]:
    match = _PR_URL_RE.search(pr_url)
    if not match:
        raise HTTPException(400, "Invalid GitHub PR URL")
    repo_slug = f"{match.group('owner')}/{match.group('repo')}"
    return repo_slug, int(match.group("number"))


def _hunk_key(header: str) -> tuple[int, int] | None:
    """Extract (new_start, new_len) from a hunk header, defaulting len to 1."""
    match = _HUNK_RE.search(header)
    if not match:
        return None
    return int(match.group("start")), int(match.group("len") or "1")


def _parse_patch_anchors(patch: str | None) -> dict[tuple[int, int], list[int]]:
    """Return {(new_start, new_len): [list of '+' line numbers]} for each hunk in a file's patch.

    GitHub rejects review comments whose `line` is not actually an added line
    inside one of the PR's hunks (422). We pre-compute the addable lines per
    hunk so the anchor picker can land on the *last* '+' line in the hunk
    matching the warning, instead of the midpoint of the hunk window which
    frequently lands on a context line.
    """
    out: dict[tuple[int, int], list[int]] = {}
    if not patch:
        return out

    current_key: tuple[int, int] | None = None
    new_line: int | None = None

    for raw in patch.splitlines():
        match = _HUNK_RE.search(raw)
        if match:
            new_start = int(match.group("start"))
            new_len = int(match.group("len") or "1")
            current_key = (new_start, new_len)
            new_line = new_start
            out.setdefault(current_key, [])
            continue
        if current_key is None or new_line is None:
            continue
        if not raw or raw.startswith("\\"):
            continue
        tag = raw[0]
        if tag == "+":
            out[current_key].append(new_line)
            new_line += 1
        elif tag == " ":
            new_line += 1
        elif tag == "-":
            pass  # deletion does not advance the new-side counter

    return out


def _pick_anchor(
    patch_anchors: dict[tuple[int, int], list[int]] | None,
    warning_hunk: str,
) -> int | None:
    """Return the line number of the last '+' line in the hunk matching the warning, or None."""
    if not patch_anchors:
        return None
    key = _hunk_key(warning_hunk)
    if key is None:
        return None
    plus_lines = patch_anchors.get(key)
    if not plus_lines:
        return None
    # Last '+' line in the hunk: anchors near the end of the change so the
    # comment shows up under the new code, not above it.
    return plus_lines[-1]


def _commit_summary(warning: Warning) -> tuple[str, str]:
    incident = warning.matched_incident
    if not incident:
        return "unknown", "Matched historical incident"
    first_line = incident.commit_message.splitlines()[0] if incident.commit_message else "Matched historical incident"
    return incident.commit_sha, first_line


def _dedup_marker(warning: Warning, line: int | None) -> str:
    """Stable per-warning fingerprint embedded in the comment body for idempotency."""
    sha, _ = _commit_summary(warning)
    short = sha[:12] if sha != "unknown" else "unknown"
    line_str = str(line) if line is not None else "issue"
    return f"<!-- scartissue:{short}:{warning.pr_file}:{line_str} -->"


def _comment_body(repo_slug: str, warning: Warning, line: int | None) -> str:
    full_sha, first_line = _commit_summary(warning)
    short_sha = full_sha[:7] if full_sha != "unknown" else "unknown"
    proposed_fix = ""
    if warning.proposed_fix:
        proposed_fix = f"\n**Suggested fix:**\n{warning.proposed_fix}\n"

    return (
        f"{_dedup_marker(warning, line)}\n"
        "⚠️ **ScarTissue: possible regression of historical bug**\n\n"
        f"This change rhymes with commit [`{short_sha}`](https://github.com/{repo_slug}/commit/{full_sha}):\n"
        f"> {first_line}\n\n"
        f"**Why this matters:** {warning.explanation}\n"
        f"{proposed_fix}\n"
        f"**Severity:** {warning.severity} · **Confidence:** {warning.confidence:.0%}\n\n"
        "---\n"
        '<sub>Posted by [ScarTissue](https://github.com/ShivamSinghNow/scartissue) — '
        "every codebase remembers its bugs.</sub>"
    )


def _body_preview(body: str) -> str:
    collapsed = " ".join(body.split())
    return collapsed[:117] + "..." if len(collapsed) > 120 else collapsed


def _existing_markers(pr) -> set[str]:
    """Collect every scartissue:... marker present in existing PR comments."""
    markers: set[str] = set()
    pattern = re.compile(r"<!-- (scartissue:[^>]+) -->")
    try:
        for comment in pr.get_issue_comments():
            for m in pattern.finditer(comment.body or ""):
                markers.add(m.group(1).strip())
    except GithubException:
        pass
    try:
        for comment in pr.get_review_comments():
            for m in pattern.finditer(comment.body or ""):
                markers.add(m.group(1).strip())
    except GithubException:
        pass
    return markers


def _github_error_detail(exc: GithubException) -> str:
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        message = data.get("message")
        if message:
            return str(message)
    return str(exc)


def _raise_github_http(exc: GithubException) -> None:
    status = getattr(exc, "status", None)
    detail = _github_error_detail(exc)
    if status == 403:
        lower_detail = detail.lower()
        if "rate limit" in lower_detail:
            headers = getattr(exc, "headers", {}) or {}
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            suffix = f"; retry-after: {retry_after}" if retry_after else ""
            raise HTTPException(429, f"GitHub rate limit exceeded{suffix}") from exc
        raise HTTPException(403, "No write access to this repository") from exc
    if status == 404:
        raise HTTPException(404, "PR not found") from exc
    if status == 429:
        headers = getattr(exc, "headers", {}) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        suffix = f"; retry-after: {retry_after}" if retry_after else ""
        raise HTTPException(429, f"GitHub rate limit exceeded{suffix}") from exc
    raise HTTPException(status or 500, detail) from exc


def _build_post_plan(
    repo_slug: str,
    warnings: list[Warning],
    file_anchors: dict[str, dict[tuple[int, int], list[int]]] | None,
    existing_markers: set[str],
) -> tuple[list[dict[str, Any]], list[PostedComment], list[tuple[PostedComment, str]], int]:
    review_comments: list[dict[str, Any]] = []
    posted: list[PostedComment] = []
    fallback_comments: list[tuple[PostedComment, str]] = []
    skipped = 0

    for warning in warnings:
        anchors = file_anchors.get(warning.pr_file) if file_anchors is not None else None
        line = _pick_anchor(anchors, warning.pr_hunk) if file_anchors is not None else _hunk_key(warning.pr_hunk)
        # In dry_run mode (file_anchors is None) we still want a plausible
        # display line; use the new_start of the hunk as a hint.
        if file_anchors is None and isinstance(line, tuple):
            line = line[0]

        body = _comment_body(repo_slug, warning, line if isinstance(line, int) else None)
        marker = _dedup_marker(warning, line if isinstance(line, int) else None)
        marker_id = marker.removeprefix("<!-- ").removesuffix(" -->").strip()

        if marker_id in existing_markers:
            skipped += 1
            continue

        if isinstance(line, int):
            posted_comment = PostedComment(
                pr_file=warning.pr_file,
                line=line,
                body_preview=_body_preview(body),
                anchored=file_anchors is not None,
            )
            posted.append(posted_comment)
            if file_anchors is not None:
                review_comments.append(
                    {"path": warning.pr_file, "line": line, "side": "RIGHT", "body": body}
                )
            else:
                # dry-run: just include in posted with the line hint
                pass
        else:
            # Could not pick an anchor — fall back to a top-level issue comment.
            posted_comment = PostedComment(
                pr_file=warning.pr_file,
                line=0,
                body_preview=_body_preview(body),
                anchored=False,
            )
            posted.append(posted_comment)
            fallback_comments.append((posted_comment, body))

    return review_comments, posted, fallback_comments, skipped


@router.post("/post-to-github", response_model=PostToGithubResponse)
async def post_to_github(request: PostToGithubRequest) -> PostToGithubResponse:
    repo_slug, number = _parse_pr_url(request.pr_url)
    summary = (
        f"ScarTissue analyzed this PR against {repo_slug}'s historical bug fixes and found "
        f"{len(request.warnings)} potential regression pattern(s). See inline comments for details."
    )

    if request.dry_run:
        _, posted, _, _ = _build_post_plan(repo_slug, request.warnings, file_anchors=None, existing_markers=set())
        return PostToGithubResponse(
            pr_url=request.pr_url,
            review_url=None,
            total_comments=len(posted),
            posted=posted,
            summary_comment=summary,
        )

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(400, "GITHUB_TOKEN not configured")

    try:
        gh = Github(token)
        repo = gh.get_repo(repo_slug)
        pr = repo.get_pull(number)
        file_anchors: dict[str, dict[tuple[int, int], list[int]]] = {
            file.filename: _parse_patch_anchors(file.patch) for file in pr.get_files()
        }
        existing_markers = _existing_markers(pr)
        review_comments, posted, fallback_comments, skipped = _build_post_plan(
            repo_slug, request.warnings, file_anchors, existing_markers,
        )

        review_url: str | None = None
        if review_comments:
            head_commit = repo.get_commit(pr.head.sha)
            try:
                review = pr.create_review(commit=head_commit, body=summary, event="COMMENT", comments=review_comments)
                review_url = f"https://github.com/{repo_slug}/pull/{number}#pullrequestreview-{review.id}"
            except GithubException as exc:
                if getattr(exc, "status", None) != 422:
                    raise
                # Last-resort: GitHub rejected the batched anchors. Re-route each
                # comment as an issue comment so the user still sees the warning.
                for comment in review_comments:
                    fallback_comments.append(
                        (
                            PostedComment(
                                pr_file=str(comment["path"]),
                                line=int(comment["line"]),
                                body_preview=_body_preview(str(comment["body"])),
                                anchored=False,
                            ),
                            str(comment["body"]),
                        )
                    )
                posted = [item.model_copy(update={"anchored": False}) for item in posted]

        for _, body in fallback_comments:
            pr.create_issue_comment(body)

        return PostToGithubResponse(
            pr_url=request.pr_url,
            review_url=review_url,
            total_comments=len(posted),
            posted=posted,
            summary_comment=summary,
            skipped_duplicates=skipped,
        )
    except GithubException as exc:
        _raise_github_http(exc)
    except Exception as exc:
        print(f"post-to-github failed: {exc}", file=sys.stderr)
        raise HTTPException(500, str(exc)) from exc

    raise HTTPException(500, "Unknown GitHub posting error")
