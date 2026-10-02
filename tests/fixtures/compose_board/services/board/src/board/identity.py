import uuid
from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import Request

from board.errors import ApiError, unavailable


@dataclass(frozen=True)
class CurrentUser:
    id: str
    nickname: str


def optional_user(request: Request) -> CurrentUser | None:
    """nginx 가 auth_request 결과로 채운 헤더를 읽는다. 클라이언트가 보낸 값은 nginx 가 지운다."""
    raw_id = request.headers.get("x-user-id")
    if not raw_id:
        return None
    try:
        user_id = str(uuid.UUID(raw_id))
    except ValueError:
        return None
    nickname = unquote(request.headers.get("x-user-nickname", "")) or "알 수 없음"
    return CurrentUser(id=user_id, nickname=nickname)


def require_user(request: Request) -> CurrentUser:
    if request.headers.get("x-auth-degraded") == "1":
        raise unavailable("로그인 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.")
    user = optional_user(request)
    if user is None:
        raise ApiError(401, "UNAUTHORIZED", "로그인이 필요합니다.")
    return user
