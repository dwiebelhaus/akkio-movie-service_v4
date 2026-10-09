"""Movie search and lookup (DESIGN.md §6).

Keyset pagination over `movies.id`. Genre names are resolved to ids in the app first, so the
planner sees literal ids and their real selectivity:
- `any`: one semi-join on `movie_genres` with `genre_id = any(ids)`
- `all`: one semi-join per genre, which Postgres merges cheaply even for common genres
"""

import base64
import binascii

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.errors import ApiError
from app.schemas import MovieFilter

_SELECT = """
select m.id, m.title, m.year, m.rating::float8 as rating,
       array(select g.name
               from movie_genres mg
               join genres g on g.id = mg.genre_id
              where mg.movie_id = m.id
              order by g.name) as genres
  from movies m
"""


class GenreCatalog:
    """Genre name -> id, cached per dataset version (genres only change with imports)."""

    def __init__(self) -> None:
        self._version: int | None = None
        self._by_name: dict[str, int] = {}

    async def resolve(self, pool: AsyncConnectionPool, names: list[str], version: int) -> list[int]:
        if self._version != version:
            async with pool.connection() as conn:
                cur = await conn.execute("select id, name from genres")
                self._by_name = {name.casefold(): gid for gid, name in await cur.fetchall()}
            self._version = version
        unknown = sorted({n for n in names if n.strip().casefold() not in self._by_name})
        if unknown:
            raise ApiError(422, "unknown_genre", f"Unknown genre(s): {', '.join(unknown)}", {"unknown": unknown})
        return sorted({self._by_name[n.strip().casefold()] for n in names})


def encode_cursor(movie_id: int) -> str:
    return base64.urlsafe_b64encode(str(movie_id).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        if value.isdigit():
            return int(value)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        pass
    raise ApiError(422, "invalid_cursor", "Invalid pagination cursor")


async def search_movies(
    pool: AsyncConnectionPool, flt: MovieFilter, genre_ids: list[int], after_id: int
) -> tuple[list[dict], str | None]:
    where = ["m.id > %(after)s"]
    params: dict = {"after": after_id, "limit": flt.limit + 1}
    if flt.year_from is not None:
        where.append("m.year >= %(year_from)s")
        params["year_from"] = flt.year_from
    if flt.year_to is not None:
        where.append("m.year <= %(year_to)s")
        params["year_to"] = flt.year_to
    if genre_ids and (flt.genre_match == "any" or len(genre_ids) == 1):
        where.append("m.id in (select movie_id from movie_genres where genre_id = any(%(genre_ids)s))")
        params["genre_ids"] = genre_ids
    elif genre_ids:
        for i, gid in enumerate(genre_ids):
            where.append(f"m.id in (select movie_id from movie_genres where genre_id = %(g{i})s)")
            params[f"g{i}"] = gid

    query = f"{_SELECT} where {' and '.join(where)} order by m.id limit %(limit)s"
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(query, params)
        rows = await cur.fetchall()

    next_cursor = None
    if len(rows) > flt.limit:
        rows = rows[: flt.limit]
        next_cursor = encode_cursor(rows[-1]["id"])
    return rows, next_cursor


async def get_movie(pool: AsyncConnectionPool, movie_id: int) -> dict | None:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(f"{_SELECT} where m.id = %s", (movie_id,))
        return await cur.fetchone()
