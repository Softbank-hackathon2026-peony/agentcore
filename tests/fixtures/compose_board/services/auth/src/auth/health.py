import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.infra import check_db, check_redis

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    # 외부 의존성은 보지 않는다. DB 장애가 Pod 재시작으로 번지지 않게 하기 위함이다.
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    redis_ok, db_ok = await asyncio.gather(
        check_redis(request.app.state.redis), check_db(request.app.state.engine)
    )
    ready = redis_ok and (db_ok or not request.app.state.settings.ready_requires_db)
    status = 200 if ready else 503
    return JSONResponse({"redis": redis_ok, "db": db_ok}, status_code=status)
