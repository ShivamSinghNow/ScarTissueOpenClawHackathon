from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _sanitize_pr_url(v: str) -> str:
    # GitHub PR URLs are pure ASCII. Drop any non-ASCII bytes (zero-width
    # spaces, BOM, bidi marks, NBSP, etc.) that browsers paste in.
    if not isinstance(v, str):
        return v
    return v.encode("ascii", errors="ignore").decode("ascii").strip()


class Incident(BaseModel):
    commit_sha: str
    commit_message: str
    commit_date: datetime
    author: str
    files_changed: list[str]
    functions_changed: list[str]
    fix_diff: str
    buggy_parent_sha: str
    issue_refs: list[int]
    symptom_summary: Optional[str] = None


class Warning(BaseModel):
    pr_file: str
    pr_hunk: str
    matched_incident: Optional[Incident] = None
    severity: Literal["low", "medium", "high"]
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    proposed_fix: Optional[str] = None


class IndexRequest(BaseModel):
    repo: str = Field(..., examples=["owner/repo"])
    max_commits: int = 3000


class IndexResponse(BaseModel):
    repo: str
    incidents_found: int
    duration_seconds: float
    status: str


class PRReviewRequest(BaseModel):
    pr_url: str = Field(..., examples=["https://github.com/owner/repo/pull/123"])

    @field_validator("pr_url", mode="before")
    @classmethod
    def strip_invisible(cls, v: str) -> str:
        return _sanitize_pr_url(v)


class ReviewResponse(BaseModel):
    pr_url: str
    pr_repo: str
    upstream_repo: Optional[str] = None  # set when pr_repo is a fork
    pr_title: str
    pr_author: str
    warnings: list[Warning]
    total_warnings: int
    duration_seconds: float
