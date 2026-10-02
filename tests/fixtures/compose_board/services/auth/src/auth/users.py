import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from auth.errors import ApiError


def _user(row) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "nickname": row["nickname"],
        "password_hash": row["password_hash"],
        "deleted_at": row["deleted_at"],
    }


async def create_user(engine: AsyncEngine, *, email: str, nickname: str, password_hash: str) -> dict:
    user_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO auth.users (id, email, nickname, password_hash) "
                    "VALUES (:id, :email, :nickname, :password_hash)"
                ),
                {"id": user_id, "email": email, "nickname": nickname, "password_hash": password_hash},
            )
    except IntegrityError as exc:
        detail = str(exc.orig)
        if "users_email_key" in detail:
            raise ApiError(409, "EMAIL_TAKEN", "이미 가입된 이메일입니다.") from exc
        if "users_nickname_key" in detail:
            raise ApiError(409, "NICKNAME_TAKEN", "이미 사용 중인 닉네임입니다.") from exc
        raise
    return {"id": str(user_id), "email": email, "nickname": nickname}


async def get_by_email(engine: AsyncEngine, email: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, email, nickname, password_hash, deleted_at FROM auth.users "
                    "WHERE email = :email AND deleted_at IS NULL"
                ),
                {"email": email},
            )
        ).mappings().first()
    return _user(row) if row else None


async def get_by_id(engine: AsyncEngine, user_id: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, email, nickname, password_hash, deleted_at FROM auth.users WHERE id = :id"),
                {"id": uuid.UUID(user_id)},
            )
        ).mappings().first()
    return _user(row) if row else None


async def soft_delete(engine: AsyncEngine, user_id: str) -> bool:
    """탈퇴 처리. 이메일/닉네임을 익명화해 같은 이메일로 재가입할 수 있게 한다. 이미 탈퇴했으면 False."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE auth.users SET deleted_at = now(), "
                "email = 'deleted-' || id::text || '@deleted.invalid', "
                "nickname = 'deleted-' || id::text "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": uuid.UUID(user_id)},
        )
    return result.rowcount == 1
