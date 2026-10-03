import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from board import keys
from board.config import Settings
from board.metrics import record_cache

log = logging.getLogger(__name__)

DB_ERRORS = (DBAPIError, PoolTimeoutError, OSError)
LOCK_TTL_SECONDS = 2
LOCK_WAIT_SECONDS = 0.05
LOCK_WAIT_ATTEMPTS = 5


async def _try(coro: Awaitable):
    """Redis 호출이 실패하면 None 을 돌려준다. 캐시는 없어도 서비스가 동작해야 한다."""
    try:
        return await coro
    except RedisError:
        return None


async def first_page(redis: Redis, loader: Callable[[], Awaitable[dict]], settings: Settings) -> dict:
    try:
        cached = await redis.get(keys.FIRST_PAGE)
    except RedisError:
        log.warning("redis unavailable, reading first page from db")
        return await loader()
    if cached is not None:
        record_cache(hit=True)
        return json.loads(cached)
    record_cache(hit=False)

    # 캐시 스탬피드 방지: 락을 잡은 요청 하나만 DB 를 조회한다.
    got_lock = await _try(redis.set(keys.FIRST_PAGE_LOCK, "1", nx=True, ex=LOCK_TTL_SECONDS))
    if not got_lock:
        for _ in range(LOCK_WAIT_ATTEMPTS):
            await asyncio.sleep(LOCK_WAIT_SECONDS)
            cached = await _try(redis.get(keys.FIRST_PAGE))
            if cached is not None:
                return json.loads(cached)

    try:
        page = await loader()
    except DB_ERRORS:
        stale = await _try(redis.get(keys.FIRST_PAGE_STALE))
        if stale is not None:
            log.warning("db unavailable, serving stale first page")
            return json.loads(stale)
        raise

    payload = json.dumps(page, ensure_ascii=False)
    with suppress(RedisError):
        async with redis.pipeline(transaction=False) as pipe:
            pipe.set(keys.FIRST_PAGE, payload, ex=settings.list_cache_ttl_seconds)
            pipe.set(keys.FIRST_PAGE_STALE, payload, ex=settings.stale_cache_ttl_seconds)
            if got_lock:
                pipe.delete(keys.FIRST_PAGE_LOCK)
            await pipe.execute()
    return page


async def get_post(redis: Redis, post_id: str) -> dict | None:
    raw = await _try(redis.get(keys.post_cache(post_id)))
    record_cache(hit=raw is not None)
    return json.loads(raw) if raw is not None else None


async def set_post(redis: Redis, post_id: str, post: dict, ttl: int) -> None:
    with suppress(RedisError):
        await redis.set(keys.post_cache(post_id), json.dumps(post, ensure_ascii=False), ex=ttl)


async def post_status(redis: Redis, post_id: str) -> str | None:
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.exists(keys.failed(post_id))
            pipe.exists(keys.pending(post_id))
            failed, pending = await pipe.execute()
    except RedisError:
        return None
    if failed:
        return "failed"
    if pending:
        return "pending"
    return None


async def invalidate(redis: Redis, *post_ids: str) -> None:
    with suppress(RedisError):
        await redis.delete(keys.FIRST_PAGE, keys.FIRST_PAGE_STALE, *(keys.post_cache(p) for p in post_ids))
