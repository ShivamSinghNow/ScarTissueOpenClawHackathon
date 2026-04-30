from __future__ import annotations

import os
import re
import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from github import Github, GithubException
from pydantic import BaseModel, field_validator

from app.models.schemas import Warning, _sanitize_pr_url
from app.rate_limits import limiter

router = APIRouter()


class PostToGithubRequest(BaseModel):
    pr_url: str
    warnings: list[Warning]
    dry_run: bool = False

    @field_validator("pr_url", mode="before")
    @classmethod
    def strip_invisible(cls, v: str) -> str:
        return _sanitize_pr_url(v)


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


_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<len>\d+))? @@")


def _parse_pr_url(pr_url: str) -> tuple[str, int]:
    match = _PR_URL_RE.search(pr_url)
    if not match:
        raise HTTPException(400, "Invalid GitHub PR URL")
    repo_slug = f"{match.group('owner')}/{match.group('repo')}"
    return repo_slug, int(match.group("number"))


def _anchor_line(hunk_header: str) -> int:
    match = _HUNK_RE.search(hunk_header)
    if not match:
        raise ValueError(f"Invalid hunk header: {hunk_header}")
    start = int(match.group("start"))
    length = int(match.group("len") or "1")
    return start + (length // 2)


def _commit_summary(warning: Warning) -> tuple[str, str]:
    incident = warning.matched_incident
    if not incident:
        return "unknown", "Matched historical incident"
    first_line = incident.commit_message.splitlines()[0] if incident.commit_message else "Matched historical incident"
    return incident.commit_sha, first_line


def _comment_body(repo_slug: str, warning: Warning) -> str:
    full_sha, first_line = _commit_summary(warning)
    short_sha = full_sha[:7] if full_sha != "unknown" else "unknown"
    proposed_fix = ""
    if warning.proposed_fix:
        proposed_fix = f"\n**Suggested fix:**\n{warning.proposed_fix}\n"

    return (
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


def _patch_new_lines(patch: str | None) -> set[int]:
    if not patch:
        return set()

    lines: set[int] = set()
    new_line: int | None = None
    for patch_line in patch.splitlines():
        match = _HUNK_RE.search(patch_line)
        if match:
            new_line = int(match.group("start"))
            continue
        if new_line is None or patch_line.startswith("\\"):
            continue
        if patch_line.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif patch_line.startswith(" "):
            new_line += 1
        elif patch_line.startswith("-"):
            continue
    return lines


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


def _build_post_plan(repo_slug: str, warnings: list[Warning], changed_lines: dict[str, set[int]] | None) -> tuple[list[dict[str, Any]], list[PostedComment], list[tuple[PostedComment, str]]]:
    review_comments: list[dict[str, Any]] = []
    posted: list[PostedComment] = []
    fallback_comments: list[tuple[PostedComment, str]] = []

    for warning in warnings:
        line = _anchor_line(warning.pr_hunk)
        body = _comment_body(repo_slug, warning)
        can_anchor = changed_lines is None or line in changed_lines.get(warning.pr_file, set())
        posted_comment = PostedComment(
            pr_file=warning.pr_file,
            line=line,
            body_preview=_body_preview(body),
            anchored=can_anchor,
        )
        posted.append(posted_comment)
        if can_anchor:
            review_comments.append({"path": warning.pr_file, "line": line, "side": "RIGHT", "body": body})
        else:
            fallback_comments.append((posted_comment, body))

    return review_comments, posted, fallback_comments


@router.post("/post-to-github", response_model=PostToGithubResponse)
@limiter.limit("5/day")
async def post_to_github(
    request: Request,
    response: Response,
    payload: PostToGithubRequest,
) -> PostToGithubResponse:
    repo_slug, number = _parse_pr_url(payload.pr_url)
    summary = (
        f"ScarTissue analyzed this PR against {repo_slug}'s historical bug fixes and found "
        f"{len(payload.warnings)} potential regression pattern(s). See inline comments for details."
    )

    if payload.dry_run:
        _, posted, _ = _build_post_plan(repo_slug, payload.warnings, changed_lines=None)
        return PostToGithubResponse(
            pr_url=payload.pr_url,
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
        changed_lines = {file.filename: _patch_new_lines(file.patch) for file in pr.get_files()}
        review_comments, posted, fallback_comments = _build_post_plan(repo_slug, payload.warnings, changed_lines)

        review_url: str | None = None
        if review_comments:
            head_commit = repo.get_commit(pr.head.sha)
            try:
                review = pr.create_review(commit=head_commit, body=summary, event="COMMENT", comments=review_comments)
                review_url = f"https://github.com/{repo_slug}/pull/{number}#pullrequestreview-{review.id}"
            except GithubException as exc:
                if getattr(exc, "status", None) != 422:
                    raise
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
            pr_url=payload.pr_url,
            review_url=review_url,
            total_comments=len(posted),
            posted=posted,
            summary_comment=summary,
        )
    except GithubException as exc:
        _raise_github_http(exc)
    except Exception as exc:
        print(f"post-to-github failed: {exc}", file=sys.stderr)
        raise HTTPException(500, str(exc)) from exc

    raise HTTPException(500, "Unknown GitHub posting error")
