"""Redis 키와 스트림 이름. board-api 와 board-worker 가 함께 쓴다."""

POSTS_STREAM = "posts:stream"
POSTS_GROUP = "writers"
POSTS_DLQ = "posts:dlq"

# auth-svc 가 발행한다 (services/auth/src/auth/events.py 와 같은 이름이어야 한다).
USER_EVENTS = "user:events"
USER_EVENTS_GROUP = "user-events"

FIRST_PAGE = "cache:posts:first"
FIRST_PAGE_STALE = "stale:posts:first"
FIRST_PAGE_LOCK = "lock:posts:first"


def idempotency(user_id: str, key: str) -> str:
    return f"idem:{user_id}:{key}"


def pending(post_id: str) -> str:
    return f"pending:{post_id}"


def failed(post_id: str) -> str:
    return f"failed:{post_id}"


def post_cache(post_id: str) -> str:
    return f"cache:post:{post_id}"


def deleted_user(user_id: str) -> str:
    """탈퇴한 사용자 표시. 큐에 남아 있던 그 사용자의 글을 저장할 때 익명으로 바꾸는 데 쓴다."""
    return f"deleted_user:{user_id}"
