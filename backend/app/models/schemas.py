from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


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


class ReviewResponse(BaseModel):
    pr_url: str
    pr_title: str
    pr_author: str
    warnings: list[Warning]
    total_warnings: int
    duration_seconds: float
