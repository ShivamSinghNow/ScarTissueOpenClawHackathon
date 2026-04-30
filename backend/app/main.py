from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.rate_limits import REVIEW_RATE_LIMIT_RESPONSE, limiter
from app.routes.index import router as index_router
from app.routes.github_post import router as github_post_router
from app.routes.email_review import router as email_review_router
from app.routes.repos import router as repos_router
from app.routes.review import router as review_router

app = FastAPI(
    title="ScarTissue",
    version="0.1.0",
    description="PR review agent that warns about reintroduced bug patterns",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://scartissue.dev",
        "https://www.scartissue.dev",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    if request.url.path == "/review":
        return JSONResponse(REVIEW_RATE_LIMIT_RESPONSE, status_code=429)
    return _rate_limit_exceeded_handler(request, exc)

app.include_router(index_router)
app.include_router(github_post_router)
app.include_router(email_review_router)
app.include_router(repos_router)
app.include_router(review_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
