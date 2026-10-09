# AGENTS.md

Guidance for AI coding agents working in this repo.

## Project

FastAPI movie database service 

- Ingest a movie CSV (`movies.csv`) via an upload endpoint.
- Ability Download the whole dataset as a gzipped CSV.
- Query by year range and genre(s), returning a list of movies.
- Progress updates for requests running longer than 2 seconds (real-time updates endpoint).
- Graded on performance, responsiveness, CPU/memory efficiency, and graceful error handling.
- Design for datasets much larger than the sample (`movies.csv` is ~367k rows): stream, don't load everything into memory per request.


## Infrastructure
- pytestdocker for integration testing
- The cloud services are undecided so design as cloud agnostic
- Database is postgresql and runs in its own docker container
- API should be scalable and load in docker container

## Layout

- `app/main.py` — FastAPI app (entrypoint set in `pyproject.toml` `[tool.fastapi]`); includes routers.
- `app/routers/movies.py` — API router (currently placeholder endpoints `/hello`, `/search`).
- `app/schemas/` — Pydantic models, one file per schema, re-exported from `app/schemas/__init__.py` (`MoviesQuery` is a starter example; update it, e.g. `genres: list[str]`, integer years).
- `movies.csv` — sample data. Columns: `movie_name,year,genres,rating`. `genres` is a comma-separated quoted string; `rating` may be empty.

## Commands

Python is pinned to 3.13 via `.python-version` (`pydantic-core` has no 3.14 wheel). Use `uv` for everything.

```bash
uv sync                          # install dependencies
uv run fastapi dev                #  run dev server, docs at localhost:8000/docs
uv add <package>                 # add a dependency (never edit uv.lock by hand)
```

Run commands from this directory (`movie_service/`).

## Conventions

- Keep endpoints `async`; offload blocking/CPU work (CSV parsing, gzip) to a threadpool or stream it. Long running endpoints require real-time status updates.
- Use Pydantic models for request/response validation; return proper HTTP errors (4xx for bad input, never an unhandled 500).
- Handle missing values in the data (empty ratings) and malformed rows without crashing.
- Add dependencies only with `uv add`; commit `pyproject.toml` and `uv.lock` together.
- Each requirement should have an end to end test built when reasonable
- Unhandled exceptions should return 500 http status
- Follow REST best practices for api design
- Data design should be well normalized

