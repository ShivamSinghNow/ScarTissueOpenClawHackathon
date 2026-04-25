import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.models.schemas import IndexRequest, IndexResponse
from app.services.bugfix_classifier import BugFixClassifier, llm_classifier_enabled
from app.services.git_miner import GitMiner
from app.services.scar_index import ScarIndex

router = APIRouter()


def _classifier_for(repo: str) -> BugFixClassifier | None:
    if not llm_classifier_enabled():
        return None
    cache_dir = Path(os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")).parent
    cache_path = cache_dir / "classifier_cache" / f"{repo.replace('/', '_')}.json"
    return BugFixClassifier(cache_path=cache_path)


@router.post("/index", response_model=IndexResponse)
async def index_repo(request: IndexRequest) -> IndexResponse:
    start = time.time()
    try:
        miner = GitMiner()
        classifier = _classifier_for(request.repo)
        incidents = miner.mine(request.repo, request.max_commits, classifier=classifier)

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
