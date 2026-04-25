import os
from typing import List

import chromadb
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

KNOWN_COLLECTION_REPOS = {
    "langchain_ai_langchain": "langchain-ai/langchain",
}


class IndexedRepo(BaseModel):
    repo: str
    incidents: int
    last_indexed: str | None


@router.get("/repos", response_model=List[IndexedRepo])
async def list_indexed_repos() -> List[IndexedRepo]:
    persist_dir = os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
    client = chromadb.PersistentClient(path=persist_dir)
    collections = client.list_collections()
    result = []
    for coll in collections:
        name = coll.name
        if name in KNOWN_COLLECTION_REPOS:
            repo = KNOWN_COLLECTION_REPOS[name]
        elif "_" in name:
            first_underscore = name.index("_")
            repo = name[:first_underscore] + "/" + name[first_underscore + 1:]
        else:
            repo = name
        count = coll.count()
        metadata = coll.metadata or {}
        last_indexed = metadata.get("last_indexed")
        result.append(IndexedRepo(repo=repo, incidents=count, last_indexed=last_indexed))
    return result
