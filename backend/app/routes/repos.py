from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.scar_index import ScarIndex

router = APIRouter()


class IndexedRepo(BaseModel):
    repo: str
    incidents: int
    last_indexed: str | None


@router.get("/repos", response_model=List[IndexedRepo])
async def list_indexed_repos() -> List[IndexedRepo]:
    return [IndexedRepo(**entry) for entry in ScarIndex().list_repos()]
