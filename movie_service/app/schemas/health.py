from typing import Literal

from pydantic import BaseModel


class Health(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
