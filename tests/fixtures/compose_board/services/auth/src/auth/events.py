from redis.asyncio import Redis

USER_EVENTS = "user:events"
USER_EVENTS_MAXLEN = 10000


async def publish_user_deleted(redis: Redis, user_id: str) -> None:
    await redis.xadd(
        USER_EVENTS,
        {"type": "user_deleted", "user_id": user_id},
        maxlen=USER_EVENTS_MAXLEN,
        approximate=True,
    )
