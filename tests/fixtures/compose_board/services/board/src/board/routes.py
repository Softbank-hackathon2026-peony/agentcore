import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from board import cache, posts_repo
from board.errors import ApiError
from board.identity import CurrentUser, require_user
from board.queue import enqueue_post

router = APIRouter()


class PostIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=5000)


def _idempotency_key(value: str | None) -> str:
    try:
        return str(uuid.UUID(value or ""))
    except ValueError:
        raise ApiError(
            400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key 헤더(UUID)가 필요합니다."
        ) from None


@router.get("/api/board/posts")
async def list_posts(
    request: Request,
    cursor: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict:
    engine = request.app.state.engine
    settings = request.app.state.settings

    async def load() -> dict:
        return await posts_repo.list_posts(engine, str(cursor) if cursor else None, limit)

    # 트래픽이 가장 몰리는 첫 페이지(기본 크기)만 캐시한다.
    if cursor is None and limit == settings.page_size:
        return await cache.first_page(request.app.state.redis, load, settings)
    return await load()


@router.get("/api/board/posts/{post_id}")
async def get_post(post_id: uuid.UUID, request: Request) -> dict:
    pid = str(post_id)
    redis = request.app.state.redis
    cached = await cache.get_post(redis, pid)
    if cached is not None:
        return {"status": "published", "post": cached}
    try:
        post = await posts_repo.get_post(request.app.state.engine, pid)
    except cache.DB_ERRORS:
        # DB 장애 중이라도 아직 큐에 있거나 실패한 글이면 그 상태를 알려 준다.
        status = await cache.post_status(redis, pid)
        if status is not None:
            return {"status": status}
        raise
    if post is not None:
        await cache.set_post(redis, pid, post, request.app.state.settings.detail_cache_ttl_seconds)
        return {"status": "published", "post": post}
    status = await cache.post_status(redis, pid)
    if status is not None:
        return {"status": status}
    raise ApiError(404, "NOT_FOUND", "글을 찾을 수 없습니다.")


@router.post("/api/board/posts", status_code=202)
async def create_post(
    body: PostIn,
    request: Request,
    user: CurrentUser = Depends(require_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    key = _idempotency_key(idempotency_key)
    post_id = await enqueue_post(
        request.app.state.redis, request.app.state.settings, user, body.title, body.body, key
    )
    return {"id": post_id, "status": "pending"}


@router.delete("/api/board/posts/{post_id}", status_code=204)
async def delete_post(post_id: uuid.UUID, request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    pid = str(post_id)
    result = await posts_repo.soft_delete(request.app.state.engine, pid, user.id)
    if result == "forbidden":
        raise ApiError(403, "FORBIDDEN", "본인이 작성한 글만 삭제할 수 있습니다.")
    if result == "not_found":
        raise ApiError(404, "NOT_FOUND", "글을 찾을 수 없습니다.")
    await cache.invalidate(request.app.state.redis, pid)
    return Response(status_code=204)
