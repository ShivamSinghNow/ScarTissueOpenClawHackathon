import time

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException

from app.models.schemas import PRReviewRequest, ReviewResponse
from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRFetcher
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex

router = APIRouter()


@router.post("/review", response_model=ReviewResponse)
async def review_pr(request: PRReviewRequest) -> ReviewResponse:
    start = time.time()
    try:
        fetcher = PRFetcher()
        pr = fetcher.fetch(request.pr_url)

        scar_index = ScarIndex()
        nia = NiaClient()
        anthropic_client = AsyncAnthropic()
        reviewer = Reviewer(scar_index, nia, anthropic_client)

        warnings = await reviewer.review(pr)

        return ReviewResponse(
            pr_url=request.pr_url,
            pr_repo=pr.repo,
            upstream_repo=pr.upstream_repo,
            pr_title=pr.title,
            pr_author=pr.author,
            warnings=warnings,
            total_warnings=len(warnings),
            duration_seconds=round(time.time() - start, 2),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
