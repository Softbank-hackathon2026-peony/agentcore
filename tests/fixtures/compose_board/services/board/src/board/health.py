import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from board.infra import check_db, check_redis

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    # Redis 만 살아 있으면 글쓰기(큐)를, DB 만 살아 있으면 읽기를 처리할 수 있다.
    # 둘 다 죽었을 때만 트래픽에서 빠진다.
    redis_ok, db_ok = await asyncio.gather(
        check_redis(request.app.state.redis), check_db(request.app.state.engine)
    )
    status = 200 if (redis_ok or db_ok) else 503
    return JSONResponse({"redis": redis_ok, "db": db_ok}, status_code=status)
