import time

from fastapi import APIRouter, HTTPException, Request, Response

from app.models.schemas import IndexRequest, IndexResponse
from app.rate_limits import limiter, reserve_daily_capacity
from app.services.git_miner import GitMiner
from app.services.scar_index import ScarIndex

router = APIRouter()


@router.post("/index", response_model=IndexResponse)
@limiter.limit("1/day")
async def index_repo(
    request: Request,
    response: Response,
    payload: IndexRequest,
) -> IndexResponse:
    start = time.time()
    reserve_daily_capacity("index", 5)
    try:
        miner = GitMiner()
        incidents = miner.mine(payload.repo, payload.max_commits)

        scar_index = ScarIndex()
        count = scar_index.index_incidents(payload.repo, incidents)

        return IndexResponse(
            repo=payload.repo,
            incidents_found=count,
            duration_seconds=round(time.time() - start, 2),
            status="indexed",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
