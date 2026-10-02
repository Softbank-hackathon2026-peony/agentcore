"""board-worker: posts:stream 과 user:events 를 소비해 DB 에 반영한다."""
import asyncio
import logging
import os
import signal
import socket
import time
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from board import cache, keys, posts_repo
from board.config import Settings
from board.infra import make_engine, make_redis
from board.observability import configure_logging
from board.worker_metrics import refresh_queue_metrics

log = logging.getLogger("board.worker")

Entry = tuple[str, dict[str, str]]
RECLAIM_INTERVAL_SECONDS = 10
METRICS_INTERVAL_SECONDS = 5
INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 5.0
# 탈퇴 표시 유지 시간. 큐에 남은 글이 이 안에 처리된다고 본다 (status_ttl 보다 넉넉하게).
DELETED_USER_TTL_SECONDS = 7200


class TransientDbError(Exception):
    """DB 연결 문제. 메시지를 버리지 않고 나중에 다시 시도한다."""


_TRANSIENT_SQLSTATES = {"57P01", "57P02", "57P03", "25006", "40001", "40P01"}


def _sqlstate(exc: BaseException) -> str | None:
    """exc, exc.orig, __cause__ 체인에서 SQLSTATE 를 찾는다 (asyncpg 예외는 .sqlstate 를 가진다)."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        state = getattr(cur, "sqlstate", None)
        if isinstance(state, str) and state:
            return state
        stack.extend([getattr(cur, "orig", None), cur.__cause__])
    return None


def is_transient(exc: BaseException) -> bool:
    # PoolTimeoutError: 커넥션 풀 고갈. DB 가 바쁜 것이므로 배치를 보존하고 재시도한다.
    if isinstance(exc, (OSError, ConnectionError, PoolTimeoutError)):
        return True
    if isinstance(exc, DBAPIError):
        if exc.connection_invalidated or isinstance(exc, (OperationalError, InterfaceError)):
            return True
    state = _sqlstate(exc)
    return state is not None and (state[:2] in ("08", "53") or state in _TRANSIENT_SQLSTATES)


def decode_post(fields: dict[str, str]) -> dict:
    return {
        "id": uuid.UUID(fields["id"]),
        "author_id": uuid.UUID(fields["author_id"]),
        "author_nickname": fields["author_nickname"],
        "title": fields["title"],
        "body": fields["body"],
        "created_at": datetime.fromisoformat(fields["created_at"]),
    }


class Worker:
    def __init__(self, engine: AsyncEngine, redis: Redis, settings: Settings, name: str):
        self.engine = engine
        self.redis = redis
        self.settings = settings
        self.name = name
        self._inflight: list[Entry] | None = None

    async def ensure_groups(self) -> None:
        for stream, group in ((keys.POSTS_STREAM, keys.POSTS_GROUP), (keys.USER_EVENTS, keys.USER_EVENTS_GROUP)):
            try:
                await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def process_posts_once(self, block_ms: int | None = None) -> int:
        """새 메시지를 최대 batch_size 개 읽어 저장한다. DB 연결 오류 시 배치를 보관하고 TransientDbError."""
        if self._inflight is None:
            resp = await self.redis.xreadgroup(
                keys.POSTS_GROUP,
                self.name,
                {keys.POSTS_STREAM: ">"},
                count=self.settings.worker_batch_size,
                block=block_ms,
            )
            self._inflight = resp[0][1] if resp else []
        entries = self._inflight
        if entries:
            await self._persist_posts(entries)
        self._inflight = None
        return len(entries)

    async def reclaim_posts_once(self) -> int:
        """죽은 워커가 가져가서 오래 ACK 하지 않은 메시지를 넘겨받는다."""
        result = await self.redis.xautoclaim(
            keys.POSTS_STREAM,
            keys.POSTS_GROUP,
            self.name,
            min_idle_time=self.settings.claim_idle_ms,
            start_id="0-0",
            count=self.settings.worker_batch_size,
        )
        claimed: list[Entry] = [(mid, fields) for mid, fields in result[1] if fields]
        # 전달 횟수로 DLQ 에 보내지 않는다. 긴 DB 장애 뒤에는 정상 글도 횟수가 쌓인다.
        # 저장할 수 없는 글은 _persist_posts 의 poison 경로(디코딩/DB 거부)로 DLQ 에 간다.
        if claimed:
            await self._persist_posts(claimed)
        return len(claimed)

    async def _persist_posts(self, entries: list[Entry]) -> None:
        decoded: list[tuple[str, dict[str, str], dict]] = []
        poison: list[tuple[str, dict[str, str], str]] = []
        for mid, fields in entries:
            try:
                decoded.append((mid, fields, decode_post(fields)))
            except (KeyError, ValueError) as exc:
                poison.append((mid, fields, f"decode: {exc!r}"))

        stored: list[tuple[str, dict[str, str], dict]] = []
        if decoded:
            await self._anonymize_deleted_authors([row for _, _, row in decoded])
            try:
                await self._insert([row for _, _, row in decoded])
                stored = decoded
            except TransientDbError:
                raise
            except Exception as exc:
                # 배치 안의 어떤 행이 DB 에서 거부됐다. 한 행씩 다시 넣어 범인만 골라낸다.
                log.warning("batch insert failed, retrying row by row: %r", exc)
                for item in decoded:
                    try:
                        await self._insert([item[2]])
                        stored.append(item)
                    except TransientDbError:
                        raise
                    except Exception as row_exc:
                        poison.append((item[0], item[1], f"insert: {row_exc!r}"))

        if stored:
            await self._ack_posts([(mid, str(row["id"])) for mid, _, row in stored])
        for mid, fields, reason in poison:
            await self.dead_letter(mid, fields, reason)

    async def _anonymize_deleted_authors(self, rows: list[dict]) -> None:
        """탈퇴 이벤트가 먼저 처리된 사용자의 글은 익명으로 저장한다.

        Redis 조회 실패는 그대로 올린다. 배치는 _inflight 에 남아 다음 반복에서 다시 시도된다.
        """
        author_ids = sorted({str(row["author_id"]) for row in rows})
        markers = await self.redis.mget([keys.deleted_user(a) for a in author_ids])
        deleted = {a for a, marker in zip(author_ids, markers, strict=True) if marker}
        for row in rows:
            if str(row["author_id"]) in deleted:
                row["author_id"] = None
                row["author_nickname"] = posts_repo.ANONYMIZED_NICKNAME

    async def _insert(self, rows: list[dict]) -> None:
        try:
            async with self.engine.begin() as conn:
                await posts_repo.insert_many(conn, rows)
        except Exception as exc:
            if is_transient(exc):
                raise TransientDbError(repr(exc)) from exc
            raise

    async def _ack_posts(self, items: list[tuple[str, str]]) -> None:
        message_ids = [mid for mid, _ in items]
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(keys.POSTS_STREAM, keys.POSTS_GROUP, *message_ids)
            pipe.xdel(keys.POSTS_STREAM, *message_ids)
            pipe.delete(*(keys.pending(post_id) for _, post_id in items))
            pipe.delete(*(keys.failed(post_id) for _, post_id in items))
            pipe.delete(keys.FIRST_PAGE)
            await pipe.execute()

    async def dead_letter(self, message_id: str, fields: dict[str, str], reason: str) -> None:
        log.error(
            "moving message to DLQ",
            extra={"extra_fields": {"message_id": message_id, "reason": reason}},
        )
        post_id = fields.get("id")
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(keys.POSTS_DLQ, {**fields, "source_id": message_id, "reason": reason[:500]})
            if post_id:
                pipe.set(keys.failed(post_id), "1", ex=self.settings.status_ttl_seconds)
                pipe.delete(keys.pending(post_id))
            pipe.xack(keys.POSTS_STREAM, keys.POSTS_GROUP, message_id)
            pipe.xdel(keys.POSTS_STREAM, message_id)
            await pipe.execute()

    async def process_user_events_once(self) -> int:
        resp = await self.redis.xreadgroup(
            keys.USER_EVENTS_GROUP, self.name, {keys.USER_EVENTS: ">"}, count=self.settings.worker_batch_size
        )
        entries: list[Entry] = resp[0][1] if resp else []
        for mid, fields in entries:
            await self._handle_user_event(mid, fields)
        return len(entries)

    async def reclaim_user_events_once(self) -> int:
        result = await self.redis.xautoclaim(
            keys.USER_EVENTS,
            keys.USER_EVENTS_GROUP,
            self.name,
            min_idle_time=self.settings.claim_idle_ms,
            start_id="0-0",
            count=self.settings.worker_batch_size,
        )
        claimed: list[Entry] = [(mid, fields) for mid, fields in result[1] if fields]
        for mid, fields in claimed:
            await self._handle_user_event(mid, fields)
        return len(claimed)

    async def _handle_user_event(self, message_id: str, fields: dict[str, str]) -> None:
        if fields.get("type") == "user_deleted":
            try:
                user_id = str(uuid.UUID(fields["user_id"]))
            except (KeyError, ValueError):
                log.error("dropping invalid user event", extra={"extra_fields": {"message_id": message_id}})
            else:
                # 먼저 표시를 남겨서, 큐에 아직 남은 이 사용자의 글도 저장 시 익명으로 바뀌게 한다.
                await self.redis.set(keys.deleted_user(user_id), "1", ex=DELETED_USER_TTL_SECONDS)
                # DB 오류는 그대로 올려서 ACK 하지 않는다. 익명화는 멱등이라 reclaim 으로 다시 처리하면 된다.
                post_ids = await posts_repo.anonymize_author(self.engine, user_id)
                await cache.invalidate(self.redis, *post_ids)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(keys.USER_EVENTS, keys.USER_EVENTS_GROUP, message_id)
            pipe.xdel(keys.USER_EVENTS, message_id)
            await pipe.execute()

    async def run(self, stop: asyncio.Event) -> None:
        heartbeat = Path(self.settings.heartbeat_path)
        groups_ready = False
        backoff = INITIAL_BACKOFF_SECONDS
        last_reclaim = last_metrics = 0.0
        log.info("worker started", extra={"extra_fields": {"consumer": self.name}})
        while not stop.is_set():
            # DB/Redis 장애 중에도 heartbeat 를 갱신한다. 의존성 장애로 Pod 가 재시작되면 안 된다.
            heartbeat.touch()
            # 큐 지표는 DB 장애 중에도 갱신되어야 한다 (장애 중 큐가 쌓이는 것을 봐야 한다).
            now = time.monotonic()
            if now - last_metrics >= METRICS_INTERVAL_SECONDS:
                try:
                    await refresh_queue_metrics(self.redis)
                    last_metrics = now
                except Exception as exc:
                    log.warning("queue metrics refresh failed", extra={"extra_fields": {"error": repr(exc)}})
            try:
                if not groups_ready:
                    await self.ensure_groups()
                    groups_ready = True
                await self.process_posts_once(block_ms=self.settings.worker_block_ms)
                await self.process_user_events_once()
                now = time.monotonic()
                if now - last_reclaim >= RECLAIM_INTERVAL_SECONDS:
                    await self.reclaim_posts_once()
                    await self.reclaim_user_events_once()
                    last_reclaim = now
                backoff = INITIAL_BACKOFF_SECONDS
            except Exception as exc:
                if isinstance(exc, ResponseError) and "NOGROUP" in str(exc):
                    # 영속성 없는 Redis 가 재시작되어 스트림/그룹이 사라졌다. 다음 반복에서 다시 만든다.
                    groups_ready = False
                log.warning(
                    "worker iteration failed, backing off",
                    extra={"extra_fields": {"error": repr(exc), "backoff_seconds": backoff}},
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        log.info("worker stopped")


async def _run(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    redis = make_redis(settings.redis_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    name = os.environ.get("HOSTNAME") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
    try:
        await Worker(engine, redis, settings, name).run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    start_http_server(settings.worker_metrics_port)
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
