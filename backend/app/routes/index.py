import time

from fastapi import APIRouter, HTTPException

from app.models.schemas import IndexRequest, IndexResponse
from app.services.git_miner import GitMiner
from app.services.scar_index import ScarIndex

router = APIRouter()


@router.post("/index", response_model=IndexResponse)
async def index_repo(request: IndexRequest) -> IndexResponse:
    start = time.time()
    try:
        miner = GitMiner()
        incidents = miner.mine(request.repo, request.max_commits)

        scar_index = ScarIndex()
        count = scar_index.index_incidents(request.repo, incidents)

        return IndexResponse(
            repo=request.repo,
            incidents_found=count,
            duration_seconds=round(time.time() - start, 2),
            status="indexed",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
