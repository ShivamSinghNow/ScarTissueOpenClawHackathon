from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes.index import router as index_router
from app.routes.github_post import router as github_post_router
from app.routes.repos import router as repos_router
from app.routes.review import router as review_router

app = FastAPI(
    title="ScarTissue",
    version="0.1.0",
    description="PR review agent that warns about reintroduced bug patterns",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(index_router)
app.include_router(github_post_router)
app.include_router(repos_router)
app.include_router(review_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
