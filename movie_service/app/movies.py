from typing import Any
from fastapi import APIRouter
from app.model import MoviesQuery

router = APIRouter()


@router.get("/hello")
async def hello():
    return "hello"


@router.post("/search")
async def search(payload: MoviesQuery) -> Any:
    return "Placeholder"
