import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.gbrain_client import GBrainClient

logger = logging.getLogger(__name__)
router = APIRouter()


class GitHubWebhookPayload(BaseModel):
    ref: str
    commits: list[dict]
    repository: dict


def _validate_github_signature(raw_body: bytes, signature: str | None, secret: str) -> None:
    if not signature:
        raise HTTPException(status_code=403, detail="Missing GitHub webhook signature")

    expected = "sha256=" + hmac.new(
        secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid GitHub webhook signature")


@router.post("/post-merge-check")
async def post_merge_check(request: Request) -> dict:
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    raw_body = await request.body()

    if secret:
        _validate_github_signature(
            raw_body,
            request.headers.get("X-Hub-Signature-256"),
            secret,
        )
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET is empty; skipping GitHub webhook HMAC validation")

    try:
        payload = GitHubWebhookPayload.model_validate(json.loads(raw_body))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not payload.ref.startswith("refs/heads/"):
        return {"status": "ok", "patterns_learned": 0}

    repo = payload.repository.get("full_name", "")
    try:
        patterns_learned = await GBrainClient().ingest_fix_commits(repo, payload.commits)
    except Exception:
        logger.exception("Post-merge GBrain ingestion failed for repo=%s", repo)
        patterns_learned = 0

    return {"status": "ok", "patterns_learned": patterns_learned}
