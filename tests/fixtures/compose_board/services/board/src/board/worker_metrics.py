"""큐 관련 게이지. board-worker 프로세스에서만 import 한다 (board-api 의 /metrics 에 섞이지 않도록)."""
import time

from prometheus_client import Gauge

from board import keys

QUEUE_LENGTH = Gauge("queue_length", "처리 대기 중인 글쓰기 메시지 수 (XLEN posts:stream)")
QUEUE_LAG_SECONDS = Gauge("queue_lag_seconds", "가장 오래된 대기 메시지의 나이(초)")
DLQ_SIZE = Gauge("dlq_size", "실패 큐(posts:dlq)에 쌓인 메시지 수")


async def refresh_queue_metrics(redis) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.xlen(keys.POSTS_STREAM)
        pipe.xrange(keys.POSTS_STREAM, count=1)
        pipe.xlen(keys.POSTS_DLQ)
        length, oldest, dlq = await pipe.execute()
    QUEUE_LENGTH.set(length)
    if oldest:
        oldest_ms = int(oldest[0][0].split("-")[0])
        QUEUE_LAG_SECONDS.set(max(0.0, time.time() - oldest_ms / 1000))
    else:
        QUEUE_LAG_SECONDS.set(0.0)
    DLQ_SIZE.set(dlq)
