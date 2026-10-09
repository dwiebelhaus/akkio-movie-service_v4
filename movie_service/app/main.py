from fastapi import FastAPI

from app.routers import movies

app = FastAPI(
    title="Movie API",
    version="1.0.0",
)

app.include_router(movies.router)
