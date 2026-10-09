from typing import Annotated

from fastapi import Depends, Request
from psycopg_pool import AsyncConnectionPool


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


Pool = Annotated[AsyncConnectionPool, Depends(get_pool)]
