from redis.asyncio import Redis


class LoginLimiter:
    """계정(이메일) 기준 로그인 실패 카운터. IP 기준 리밋은 nginx 가 담당한다."""

    def __init__(self, redis: Redis, limit: int, window_seconds: int):
        self.redis = redis
        self.limit = limit
        self.window = window_seconds

    @staticmethod
    def _key(email: str) -> str:
        return f"login_fail:{email}"

    async def blocked_for(self, email: str) -> int:
        count = await self.redis.get(self._key(email))
        if count is None or int(count) < self.limit:
            return 0
        ttl = await self.redis.ttl(self._key(email))
        return max(ttl, 1)

    async def record_failure(self, email: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(self._key(email))
            pipe.expire(self._key(email), self.window, nx=True)
            await pipe.execute()

    async def reset(self, email: str) -> None:
        await self.redis.delete(self._key(email))
