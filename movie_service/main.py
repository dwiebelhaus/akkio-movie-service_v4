from fastapi import FastAPI

app = FastAPI(
    title="Movie API",
    version="1.0.0",
)

from app.movies import router as movies_router

app.include_router(movies_router)
