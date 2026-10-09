from typing import Literal

from pydantic import BaseModel


class Health(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
    # The cache is optional: when it's unavailable the API still serves from Postgres.
    cache: Literal["ok", "unavailable", "disabled"]
