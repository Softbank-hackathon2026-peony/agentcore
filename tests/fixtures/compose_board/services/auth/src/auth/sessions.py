import json
import secrets

from redis.asyncio import Redis


def _session_key(sid: str) -> str:
    return f"session:{sid}"


def _user_sessions_key(user_id: str) -> str:
    return f"user_sessions:{user_id}"


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.redis = redis
        self.ttl = ttl_seconds

    async def create(self, *, user_id: str, email: str, nickname: str) -> str:
        sid = secrets.token_urlsafe(32)
        value = json.dumps({"user_id": user_id, "email": email, "nickname": nickname}, ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(_session_key(sid), value, ex=self.ttl)
            pipe.sadd(_user_sessions_key(user_id), sid)
            pipe.expire(_user_sessions_key(user_id), self.ttl)
            await pipe.execute()
        return sid

    async def get(self, sid: str) -> dict | None:
        raw = await self.redis.get(_session_key(sid))
        return json.loads(raw) if raw else None

    async def delete(self, sid: str, user_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(_session_key(sid))
            pipe.srem(_user_sessions_key(user_id), sid)
            await pipe.execute()

    async def delete_all(self, user_id: str) -> int:
        sids = await self.redis.smembers(_user_sessions_key(user_id))
        async with self.redis.pipeline(transaction=True) as pipe:
            for sid in sids:
                pipe.delete(_session_key(sid))
            pipe.delete(_user_sessions_key(user_id))
            await pipe.execute()
        return len(sids)
