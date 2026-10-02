"""db/migrations/*.sql 을 이름순으로 한 번씩 적용한다.

여러 Pod/Job이 동시에 실행해도 advisory lock 으로 직렬화된다.
"""
import asyncio
import logging
import os
from pathlib import Path

import asyncpg

log = logging.getLogger("board.migrate")
ADVISORY_LOCK_ID = 7272001


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def migrate(database_url: str, migrations_dir: str | Path) -> list[str]:
    conn = await asyncpg.connect(_dsn(database_url))
    try:
        applied_now: list[str] = []
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_ID)
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
                " version TEXT PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM public.schema_migrations")}
            for path in sorted(Path(migrations_dir).glob("*.sql")):
                if path.name in done:
                    continue
                # 인자 없는 execute 는 simple query protocol 이라 여러 문장을 한 번에 실행할 수 있다.
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO public.schema_migrations (version) VALUES ($1)", path.name)
                applied_now.append(path.name)
        return applied_now
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    applied = asyncio.run(
        migrate(os.environ["DATABASE_URL"], os.environ.get("MIGRATIONS_DIR", "/app/migrations"))
    )
    log.info("migrations applied: %s", applied or "none")


if __name__ == "__main__":
    main()
