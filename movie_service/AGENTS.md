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
- `app/routers/movies.py` — API router for movie endpoints.
- `app/domain/` — Domain classes (plain dataclasses, e.g. `Movie`), independent of the API layer.
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

## Parallel work with git worktrees

Multiple agents may work on this repo at the same time. Each agent works in its own git worktree on its own branch; never edit the main checkout directly or share a worktree with another agent.

Create a worktree per task, from the repo root, as a sibling directory:

```bash
git fetch origin
git worktree add ../akkio-movie-service_v4.worktrees/<branch> -b <branch> origin/main
cd ../akkio-movie-service_v4.worktrees/<branch>/movie_service
uv sync                          # each worktree gets its own .venv; the uv cache is shared, so this is fast
```

Branch names: short and kebab-case, describing the task (e.g. `add-search-endpoint`, `csv-upload`).

Keep worktrees isolated so parallel agents don't collide:

- **Ports:** don't assume 8000. Pick a free port per worktree: `uv run fastapi dev --port <port>`.
- **Docker:** set a unique `COMPOSE_PROJECT_NAME` per worktree (e.g. the branch name) so containers, networks, and volumes don't clash. Never hard-code host ports in compose files; read them from env vars (e.g. `POSTGRES_PORT`, `API_PORT`) so each worktree can use its own.
- **Database:** each worktree uses its own Postgres container/volume. Never point tests at another worktree's database or a shared one. Integration tests (pytest-docker) must start and tear down their own containers.
- **Local files:** keep `.env` and generated data inside the worktree; never write outside it.
- **Scope:** stay within your task. Don't refactor files outside it, so merges with other agents' branches stay clean.

Finishing a task:

1. Rebase onto the latest `origin/main` and resolve conflicts (`git fetch origin && git rebase origin/main`).
2. Run the full test suite in the worktree.
3. Push the branch and open a PR against `main`; one PR per worktree/task.
4. After the PR merges, remove the worktree and stop its containers:

```bash
docker compose -p <branch> down -v
git worktree remove ../akkio-movie-service_v4.worktrees/<branch>
git branch -d <branch>
```

Use `git worktree list` to see active worktrees and `git worktree prune` to clean up stale entries.

## Conventions

- Keep endpoints `async`; offload blocking/CPU work (CSV parsing, gzip) to a threadpool or stream it. Long running endpoints require real-time status updates.
- Use Pydantic models for request/response validation; return proper HTTP errors (4xx for bad input, never an unhandled 500).
- Handle missing values in the data (empty ratings) and malformed rows without crashing.
- Add dependencies only with `uv add`; commit `pyproject.toml` and `uv.lock` together.
- Each requirement should have an end to end test built when reasonable
- Unhandled exceptions should return 500 http status
- Follow REST best practices for api design
- Data design should be well normalized

