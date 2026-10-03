import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, EmailStr, Field, StringConstraints

from auth import users
from auth.config import Settings
from auth.errors import ApiError
from auth.events import publish_user_deleted
from auth.login_limiter import LoginLimiter
from auth.passwords import DUMMY_HASH, hash_password, verify_password
from auth.sessions import SessionStore

log = logging.getLogger(__name__)
router = APIRouter()
COOKIE_NAME = "sid"


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # 앞뒤 공백은 잘라낸 뒤 길이를 검사한다 (공백뿐인 닉네임은 422).
    nickname: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=20)]


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    email: str
    nickname: str


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


def _store(request: Request) -> SessionStore:
    return SessionStore(request.app.state.redis, request.app.state.settings.session_ttl_seconds)


def _set_cookie(response: Response, sid: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax")


async def _current_session(request: Request) -> dict:
    sid = request.cookies.get(COOKIE_NAME)
    session = await _store(request).get(sid) if sid else None
    if session is None:
        raise ApiError(401, "UNAUTHORIZED", "로그인이 필요합니다.")
    return {**session, "sid": sid}


@router.post("/api/auth/signup", status_code=201)
async def signup(body: SignupIn, request: Request) -> UserOut:
    password_hash = await hash_password(body.password)
    user = await users.create_user(
        request.app.state.engine,
        email=body.email.lower(),
        nickname=body.nickname,
        password_hash=password_hash,
    )
    return UserOut(**user)


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> UserOut:
    settings: Settings = request.app.state.settings
    email = body.email.lower()
    limiter = LoginLimiter(request.app.state.redis, settings.login_fail_limit, settings.login_fail_window_seconds)
    retry_after = await limiter.blocked_for(email)
    if retry_after:
        raise ApiError(
            429,
            "TOO_MANY_ATTEMPTS",
            "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            {"Retry-After": str(retry_after)},
        )
    user = await users.get_by_email(request.app.state.engine, email)
    ok = await verify_password(user["password_hash"] if user else DUMMY_HASH, body.password)
    if user is None or not ok:
        await limiter.record_failure(email)
        raise ApiError(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.")
    await limiter.reset(email)
    sid = await _store(request).create(user_id=user["id"], email=user["email"], nickname=user["nickname"])
    _set_cookie(response, sid, settings)
    return UserOut(id=user["id"], email=user["email"], nickname=user["nickname"])


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    response = Response(status_code=204)
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        store = _store(request)
        session = await store.get(sid)
        if session:
            await store.delete(sid, session["user_id"])
    _clear_cookie(response, request.app.state.settings)
    return response


@router.get("/api/auth/me")
async def me(request: Request) -> UserOut:
    session = await _current_session(request)
    return UserOut(id=session["user_id"], email=session["email"], nickname=session["nickname"])


@router.delete("/api/auth/me", status_code=204)
async def delete_account(body: DeleteAccountIn, request: Request) -> Response:
    """탈퇴. 각 단계가 멱등이라 중간에 실패해도 같은 요청을 다시 보내면 끝까지 진행된다."""
    session = await _current_session(request)
    engine = request.app.state.engine
    user = await users.get_by_id(engine, session["user_id"])
    if user is None or not await verify_password(user["password_hash"], body.password):
        raise ApiError(403, "FORBIDDEN", "비밀번호가 올바르지 않습니다.")
    await users.soft_delete(engine, user["id"])
    await publish_user_deleted(request.app.state.redis, user["id"])
    await _store(request).delete_all(user["id"])
    response = Response(status_code=204)
    _clear_cookie(response, request.app.state.settings)
    return response


@router.get("/internal/verify", include_in_schema=False)
async def verify(request: Request) -> Response:
    """nginx auth_request 전용. 항상 200 을 반환한다.

    nginx 는 auth_request 가 5xx 를 받으면 원래 요청 전체를 500 으로 끝내므로,
    Redis 장애 시에도 200 + X-Auth-Degraded 로 응답하고 판단은 board-api 에 맡긴다.
    """
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return Response(status_code=200)
    try:
        session = await _store(request).get(sid)
        if session is None:
            return Response(status_code=200)
        headers = {"X-User-Id": session["user_id"], "X-User-Nickname": quote(session["nickname"], safe="")}
    except Exception:
        # Redis 장애뿐 아니라 깨진 세션 값도 5xx 로 올리면 nginx 가 요청 전체를 500 으로 끝낸다.
        log.warning("session lookup failed during verify", exc_info=True)
        return Response(status_code=200, headers={"X-Auth-Degraded": "1"})
    return Response(status_code=200, headers=headers)
