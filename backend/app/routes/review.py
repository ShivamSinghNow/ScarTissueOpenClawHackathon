import time

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException, Request, Response

from app.models.schemas import PRReviewRequest, ReviewResponse
from app.rate_limits import limiter, reserve_daily_capacity
from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRFetcher
from app.services.gbrain_client import GBrainClient
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex
from app.spend_guard import SafeAsyncAnthropic, SpendCapExceeded, SPEND_LIMIT_MESSAGE

router = APIRouter()


@router.post("/review", response_model=ReviewResponse)
@limiter.limit("10/day")
async def review_pr(
    request: Request,
    response: Response,
    payload: PRReviewRequest,
) -> ReviewResponse:
    start = time.time()
    reserve_daily_capacity("review", 300)
    try:
        fetcher = PRFetcher()
        pr = fetcher.fetch(payload.pr_url)

        scar_index = ScarIndex()
        nia = NiaClient()
        anthropic_client = SafeAsyncAnthropic(AsyncAnthropic())
        reviewer = Reviewer(scar_index, nia, anthropic_client, gbrain_client=GBrainClient())

        warnings = await reviewer.review(pr)

        return ReviewResponse(
            pr_url=payload.pr_url,
            pr_repo=pr.repo,
            upstream_repo=pr.upstream_repo,
            pr_title=pr.title,
            pr_author=pr.author,
            warnings=warnings,
            total_warnings=len(warnings),
            duration_seconds=round(time.time() - start, 2),
        )
    except SpendCapExceeded as e:
        raise HTTPException(status_code=503, detail=SPEND_LIMIT_MESSAGE) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
