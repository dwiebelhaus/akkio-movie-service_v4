"""Apply plain-SQL migrations from `app/db/migrations/` in filename order.

Safe to run from every API replica and worker at startup: an advisory lock makes
concurrent runs wait for each other, and applied files are recorded in `schema_migrations`.
"""

import logging
from pathlib import Path

from psycopg import AsyncConnection

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_ID = 0x6D6F7669  # arbitrary, unique to migrations


async def migrate(conn: AsyncConnection) -> None:
    async with conn.transaction():
        await conn.execute("select pg_advisory_xact_lock(%s)", (_LOCK_ID,))
        await conn.execute(
            "create table if not exists schema_migrations ("
            " name text primary key, applied_at timestamptz not null default now())"
        )
        cur = await conn.execute("select name from schema_migrations")
        applied = {row[0] for row in await cur.fetchall()}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            logger.info("applying migration %s", path.name)
            await conn.execute(path.read_text())
            await conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
