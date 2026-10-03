import asyncio
from collections.abc import Callable
from typing import TypeVar

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.concurrency import run_in_threadpool

from auth.errors import unavailable

T = TypeVar("T")

# parallelism=1: 해시 하나가 스레드 1개만 쓰게 해서 파드 CPU limit 안에서 스레드 경합을 줄인다.
# argon2 해시 문자열에 파라미터가 들어 있어서 기존(p=4) 해시도 그대로 검증된다.
_hasher = PasswordHasher(parallelism=1)

# argon2 는 해시 하나당 메모리를 많이(기본 약 64MiB) 쓴다. 스레드풀이 허용하는 만큼
# 동시에 돌리면 파드가 OOMKill 될 수 있어서, 프로세스 전체에서 동시 실행 수를 제한한다.
_DEFAULT_HASH_CONCURRENCY = 4
# 슬롯을 기다리는 최대 시간. 로그인 폭주 때 요청이 무한정 줄 서서 LB 타임아웃까지 붙잡히지 않도록
# 이 시간이 지나면 503(Retry-After)으로 빨리 거절한다.
_DEFAULT_QUEUE_TIMEOUT_SECONDS = 5.0
_semaphore = asyncio.Semaphore(_DEFAULT_HASH_CONCURRENCY)
_queue_timeout_seconds = _DEFAULT_QUEUE_TIMEOUT_SECONDS


def configure(concurrency: int, queue_timeout_seconds: float = _DEFAULT_QUEUE_TIMEOUT_SECONDS) -> None:
    """앱 시작 시 호출해서 동시 argon2 실행 수 제한과 대기 시간 상한을 (다시) 설정한다."""
    global _semaphore, _queue_timeout_seconds
    _semaphore = asyncio.Semaphore(concurrency)
    _queue_timeout_seconds = queue_timeout_seconds

# 존재하지 않는 이메일로 로그인할 때도 같은 시간만큼 해시 검증을 해서,
# 응답 시간으로 가입 여부를 추측하지 못하게 한다.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


async def _run_limited(fn: Callable[..., T], *args) -> T:
    # argon2 는 CPU 를 많이 쓰므로 이벤트 루프를 막지 않게 스레드풀에서 실행한다.
    semaphore = _semaphore
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=_queue_timeout_seconds)
    except TimeoutError:
        raise unavailable() from None
    try:
        return await run_in_threadpool(fn, *args)
    finally:
        semaphore.release()


async def hash_password(password: str) -> str:
    return await _run_limited(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    return await _run_limited(_verify)
