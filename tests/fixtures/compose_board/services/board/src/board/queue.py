from contextlib import suppress
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from uuid6 import uuid7

from board import keys
from board.config import Settings
from board.errors import RETRY_AFTER_SECONDS, ApiError, unavailable
from board.identity import CurrentUser


async def enqueue_post(
    redis: Redis, settings: Settings, user: CurrentUser, title: str, body: str, idempotency_key: str
) -> str:
    """글을 posts:stream 에 넣고 post_id 를 돌려준다. DB 는 건드리지 않는다."""
    idem_key = keys.idempotency(user.id, idempotency_key)
    existing = await redis.get(idem_key)
    if existing:
        return existing

    # 큐 상한 확인을 멱등 키 저장보다 먼저 한다. 503 을 받은 재시도가 "이미 처리됨"으로 처리되면 안 된다.
    if await redis.xlen(keys.POSTS_STREAM) >= settings.queue_max_len:
        raise ApiError(
            503,
            "QUEUE_FULL",
            "요청이 많아 글을 받을 수 없습니다. 잠시 후 다시 시도해 주세요.",
            {"Retry-After": RETRY_AFTER_SECONDS},
        )

    post_id = str(uuid7())
    if not await redis.set(idem_key, post_id, nx=True, ex=settings.idempotency_ttl_seconds):
        # 같은 키로 동시에 들어온 재시도. 먼저 들어온 요청의 post_id 를 돌려준다.
        return await redis.get(idem_key) or post_id

    fields = {
        "id": post_id,
        "author_id": user.id,
        "author_nickname": user.nickname,
        "title": title,
        "body": body,
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.xadd(keys.POSTS_STREAM, fields)
            pipe.set(keys.pending(post_id), "1", ex=settings.status_ttl_seconds)
            await pipe.execute()
    except RedisError as exc:
        with suppress(RedisError):
            await redis.delete(idem_key)
        raise unavailable() from exc
    return post_id
