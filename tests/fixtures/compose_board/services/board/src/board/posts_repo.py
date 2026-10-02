import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

ANONYMIZED_NICKNAME = "탈퇴한 사용자"
_COLUMNS = "id, author_id, author_nickname, title, body, created_at"


def serialize(row) -> dict:
    return {
        "id": str(row["id"]),
        "author_id": str(row["author_id"]) if row["author_id"] else None,
        "author_nickname": row["author_nickname"],
        "title": row["title"],
        "body": row["body"],
        "created_at": row["created_at"].isoformat(),
    }


async def list_posts(engine: AsyncEngine, cursor: str | None, limit: int) -> dict:
    sql = f"SELECT {_COLUMNS} FROM board.posts WHERE deleted_at IS NULL"
    params: dict = {"n": limit + 1}
    if cursor:
        sql += " AND id < :cursor"
        params["cursor"] = uuid.UUID(cursor)
    sql += " ORDER BY id DESC LIMIT :n"
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    items = [serialize(r) for r in rows[:limit]]
    next_cursor = items[-1]["id"] if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


async def get_post(engine: AsyncEngine, post_id: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(f"SELECT {_COLUMNS} FROM board.posts WHERE id = :id AND deleted_at IS NULL"),
                {"id": uuid.UUID(post_id)},
            )
        ).mappings().first()
    return serialize(row) if row else None


async def insert_many(conn: AsyncConnection, rows: list[dict]) -> None:
    await conn.execute(
        text(
            "INSERT INTO board.posts (id, author_id, author_nickname, title, body, created_at) "
            "VALUES (:id, :author_id, :author_nickname, :title, :body, :created_at) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        rows,
    )


async def soft_delete(engine: AsyncEngine, post_id: str, author_id: str) -> str:
    async with engine.begin() as conn:
        deleted = (
            await conn.execute(
                text(
                    "UPDATE board.posts SET deleted_at = now() "
                    "WHERE id = :id AND author_id = :author AND deleted_at IS NULL RETURNING id"
                ),
                {"id": uuid.UUID(post_id), "author": uuid.UUID(author_id)},
            )
        ).first()
        if deleted:
            return "deleted"
        exists = (
            await conn.execute(
                text("SELECT 1 FROM board.posts WHERE id = :id AND deleted_at IS NULL"),
                {"id": uuid.UUID(post_id)},
            )
        ).first()
    return "forbidden" if exists else "not_found"


async def anonymize_author(engine: AsyncEngine, user_id: str) -> list[str]:
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "UPDATE board.posts SET author_id = NULL, author_nickname = :nickname "
                    "WHERE author_id = :author RETURNING id"
                ),
                {"nickname": ANONYMIZED_NICKNAME, "author": uuid.UUID(user_id)},
            )
        ).all()
    return [str(r[0]) for r in rows]
