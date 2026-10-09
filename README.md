# Movie Service

A FastAPI service for a movie database. You can:

- **Import** movie CSVs (`movie_name,year,genres,rating`). Each import is merged into the existing data and deduplicated, both within the file and across imports.
- **Search** movies by year range and one or more genres.
- **Export** the whole database as a gzipped CSV.
- **Follow progress** of long-running work (imports and exports) in real time with server-sent events (SSE).

It is built for datasets much larger than the 367k-row sample (`movies.csv`). Uploads and exports are streamed, so memory stays flat as data grows. Bulk loads use Postgres `COPY`. Long-running work happens in a separate worker, so the API stays responsive.

The full design, including the measurements behind each choice, is in [movie_service/DESIGN.md](movie_service/DESIGN.md).

## API

All endpoints are under `/api/v1`, except `/health`. Interactive docs are at `/docs` (Swagger UI) and `/redoc`. The OpenAPI spec at `/openapi.json` lists the current genre names as allowed values.

| Method | Path | Purpose | Responses |
|---|---|---|---|
| `POST` | `/api/v1/imports` | Upload a CSV (multipart field `file`, or a raw `text/csv` body) and queue an import job. Needs `X-API-Key`. | `202` + job, `Location: /api/v1/jobs/{id}` |
| `GET` | `/api/v1/movies` | Search by year range and genres, with cursor pagination. | `200` / `304` |
| `GET` | `/api/v1/movies/{movie_id}` | Get one movie. | `200` / `304` / `404` |
| `GET` | `/api/v1/genres` | List all genre names, which are the valid `genre` filter values. | `200` / `304` |
| `POST` | `/api/v1/exports` | Start a gzipped CSV export, or reuse a finished or in-progress export of the current data. Needs `X-API-Key`. | `202` new / `200` reused, + job |
| `GET` | `/api/v1/exports/{job_id}/file` | Download a finished export (`application/gzip`). | `200` / `409` not ready / `410` replaced |
| `GET` | `/api/v1/jobs/{job_id}` | Get a snapshot of a job's status. | `200` / `404` |
| `GET` | `/api/v1/jobs/{job_id}/events` | Stream live job progress over SSE. The stream closes when the job succeeds or fails. | `text/event-stream` / `503` too many streams |
| `GET` | `/health` | Check that the service and database are up, and report the cache state. | `200` / `503` |

**Search parameters** for `GET /api/v1/movies`:

| Parameter | Meaning |
|---|---|
| `year_from`, `year_to` | Inclusive year bounds. When either bound is set, movies with an unknown year are excluded. |
| `genre` | Repeatable, case-insensitive (`?genre=Action&genre=Drama`). |
| `genre_match` | `any` (default) or `all`. |
| `limit` | Page size: 50 by default, 1000 at most. |
| `cursor` | The `next_cursor` value from the previous page. |

Unknown genres and unknown query parameters return `422`.

**Response shapes:**

```json
// Movie
{"id": 42, "title": "Glass Onion", "year": 2022, "genres": ["Comedy", "Crime", "Drama"], "rating": 7.2}

// Page
{"items": [ /* movies */ ], "next_cursor": "..." }

// Job
{"id": 7, "type": "import", "status": "running", "progress": 0.43,
 "processed_rows": 158000, "total_rows": null, "result": null, "error": null}

// Error (the same shape for every error)
{"error": {"code": "invalid_csv", "message": "Missing required column: year", "details": {}}}
```

**Authentication:** the two write endpoints (`POST /imports` and `POST /exports`) need an `X-API-Key` header that matches the `API_KEY` setting. Read endpoints are open. See [API keys](#api-keys) for how keys are set up.

**Status codes:** `401` for a missing or wrong API key, `404` for a missing resource, `409` for an export that isn't ready, `413` for an oversized upload, `422` for invalid input (including ids outside the 64-bit range), `503` (with `Retry-After`) when an API process already has `MAX_EVENT_STREAMS` progress streams open or a query exceeds `STATEMENT_TIMEOUT_MS`, and a generic `500` for unexpected errors. Error details go to the logs, never to the client.

## Tech stack

| Area | Choice |
|---|---|
| Language | Python 3.13, managed with [uv](https://docs.astral.sh/uv/) |
| Web framework | FastAPI (async), Pydantic v2, pydantic-settings |
| Database | PostgreSQL 16, accessed with psycopg 3 (async) and psycopg_pool. Migrations are plain SQL files applied at startup. |
| Job queue and progress | A Postgres `jobs` table, claimed with `FOR UPDATE SKIP LOCKED`. Progress is broadcast with `LISTEN/NOTIFY` and streamed to clients over SSE (`sse-starlette`). |
| Cache | Redis 7, optional and fail-open, plus HTTP `ETag` / `304` responses |
| Storage | A shared volume behind a small `Storage` interface, so it can be swapped for S3 or GCS |
| Containers | Docker Compose with four services: `api`, `worker`, `postgres` and `redis` |
| Tests | pytest. End-to-end tests send real HTTP requests with `httpx` to an isolated stack started by `pytest-docker`. |
| CI | GitHub Actions runs the unit and end-to-end suites on every PR and on every push to `main`. |

## Key decisions

The full list, with the reasoning and measurements, is under "Decisions" in [movie_service/DESIGN.md](movie_service/DESIGN.md#decisions).

- **Normalized schema.** The schema has `movies`, `genres` and a `movie_genres` link table, with foreign keys enforced. The year and the rating can be null, because about 15% of rows have no year and about 37% have no rating.
- **Deduplication key `(title, year, genres)`.** The title is normalized before matching. Rows with the same title and year but different genres stay separate films. On a match, the latest non-null rating wins, so re-importing the same file writes nothing.
- **Bad rows are skipped, not fatal.** Rows with an unparseable year or rating, a missing title or the wrong field count are counted and sampled in the job result. Only file-level problems fail a job, such as a file that isn't a CSV or is missing a required column.
- **Imports and exports always run as background jobs.** The API returns `202` with a job ID right away, and a separate worker container does the work. You can follow progress over SSE or by polling. Concurrent imports are serialized with a Postgres advisory lock.
- **Postgres as the job queue.** Postgres is the queue instead of Redis or a message broker, so jobs survive restarts and there is one fewer system to run. Workers send heartbeats. A sweep marks crashed jobs as `failed`, and a graceful shutdown puts the current job back in the queue.
- **Streaming everywhere.** Uploads are streamed to disk with a size limit enforced along the way (`MAX_UPLOAD_BYTES`, 64 MB by default, which is enough for about 750k movies). The file is then loaded with `COPY` into a temporary table and merged with set-based SQL. Exports stream from `COPY TO` through gzip in 1 MB chunks, from one consistent snapshot of the data.
- **Keyset pagination.** Pages use an opaque cursor over `id` rather than an offset, so deep pages stay fast. Every measured search pattern ran in under 4 ms on the sample.
- **Caching keyed on a dataset version.** Imports that change data bump a version number, and every cache key includes it. That covers `ETag` responses, the Redis cache and export reuse, so no cache ever needs explicit invalidation. If Redis is down, results are served from Postgres.
- **Stateless and cloud-agnostic.** The API and the worker scale independently. All configuration comes from environment variables, and file storage sits behind an interface that can be pointed at cloud storage.

## Setup

**Prerequisites:**

- [Docker](https://docs.docker.com/get-docker/) with the Compose v2 plugin (`docker compose`)
- [uv](https://docs.astral.sh/uv/getting-started/installation/). It installs the pinned Python 3.13 for you.
- Git

**Clone the repo and install dependencies.** Run every command after this from the `movie_service/` directory.

```bash
git clone https://github.com/dwiebelhaus/akkio-movie-service_v4.git
```

```bash
cd akkio-movie-service_v4/movie_service
```

```bash
uv sync
```

**Configuration (optional).** The defaults work without changes, except `API_KEY` and `REDIS_PASSWORD`, which have no defaults; `run.py up` generates both for you (see [API keys](#api-keys)). To override settings, copy `.env.example` to `.env` and edit it. The most useful settings are:

| Variable | Default | Purpose |
|---|---|---|
| `API_KEY` | None (required, 16+ characters; `run.py up` generates one) | The value expected in the `X-API-Key` header for imports and exports |
| `REDIS_PASSWORD` | None (required; `run.py up` generates one) | Redis refuses to start without it, and the API connects with it. URL-safe characters only |
| `API_PORT` | 8000, or the next free port | The API's port on your machine |
| `POSTGRES_PORT` | Random, localhost only | Postgres's port on your machine, for debugging |
| `MAX_UPLOAD_BYTES` | 64 MB | The upload size limit |
| `WORKER_REPLICAS` | 1 | The number of worker containers |
| `CACHE_MAX_AGE_SECONDS` | 3600 | The `Cache-Control` max-age on movie reads |
| `COMPOSE_PROJECT_NAME` | Directory name | Set a unique name to run several stacks side by side |

## Running locally

**Start the stack and load the sample data.** This command builds the images, starts `postgres`, `redis`, `api` and `worker`, waits until they are healthy, and imports `movies.csv` with a progress display. The first import takes about 40 seconds.

```bash
uv run python run.py up --seed
```

The command prints the API's URL, which is `http://localhost:8000` unless that port is taken. Open `http://localhost:8000/docs` to explore the API.

**Example requests:**

```bash
curl "http://localhost:8000/api/v1/movies?year_from=2000&year_to=2010&genre=Action&genre=Comedy&genre_match=all&limit=5"
```

```bash
curl -H "X-API-Key: $(grep '^API_KEY=' .env | cut -d= -f2-)" -F "file=@movies.csv" http://localhost:8000/api/v1/imports
```

```bash
curl -N http://localhost:8000/api/v1/jobs/1/events
```

**Download the whole database** as a gzipped CSV. This starts an export, or reuses the current one, shows progress, and saves the file as `movies-export.csv.gz`. Use `-o` to choose another file name.

```bash
uv run python run.py export
```

**Other commands:**

```bash
uv run python run.py logs
```

```bash
uv run python run.py down
```

```bash
uv run python run.py down --volumes
```

`logs` follows the service logs. `down` stops the stack, and `--volumes` also deletes all data. Running `up` without `--seed` starts an empty database.

**Running the API outside Docker** for faster iteration, with auto-reload. Start only Postgres in Docker and point the app at it. In `.env`, set `POSTGRES_PORT=5432` (by default Postgres gets a random localhost port) and an `API_KEY` (see [API keys](#api-keys); `run.py up` adds one if you've run it once). Then run:

```bash
docker compose up -d postgres
```

```bash
DATA_DIR=./data REDIS_URL= uv run fastapi dev --port 8001
```

```bash
DATA_DIR=./data uv run python -m app.worker
```

The API (without the cache; docs at `localhost:8001/docs`) and the worker, in a second terminal, read `API_KEY` and the other settings from `.env`. Give them the same `DATA_DIR` so the worker can find the files the API uploads.

## API keys

Write endpoints (`POST /api/v1/imports` and `POST /api/v1/exports`) need an `X-API-Key` header that matches the `API_KEY` setting. Reads are open: search, genres, job status and events, and export downloads.

- **No default.** The API and the worker refuse to start if `API_KEY` is missing or shorter than 16 characters. A deployment that forgets to set it fails at startup instead of accepting writes with a well-known key. The rejected value is never written to the logs.
- **Locally, `run.py up` creates one for you.** If `API_KEY` isn't set in the environment or in `.env`, `run.py up` generates a random key and writes it to `.env`, which git ignores. `run.py up --seed` and `run.py export` read the key from there automatically.
- **Use it** from curl as shown above, or in Swagger UI (`/docs`) by clicking **Authorize** and pasting the key.
- **Choose your own** key by setting `API_KEY` in `.env` or the environment. Generate one with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
- **Rotate** a key by changing the value and running `uv run python run.py up` again, which recreates the containers with the new value.
- **In deployed environments**, supply `API_KEY` from your platform's secret store as an environment variable. Never commit it, and use a different key for each environment.
- **Upgrading from an older checkout:** an existing `.env` with `API_KEY=dev-api-key` is now too short. Delete that line and `run.py up` generates a new key. `run.py up` also adds a `REDIS_PASSWORD` if `.env` doesn't have one.

## Testing

Run the whole suite. The end-to-end tests start their own isolated Docker Compose stack, with a unique project name and random ports, and tear it down afterwards. They never touch your local data.

```bash
uv run python run.py test
```

Run only the fast unit tests (no Docker needed):

```bash
uv run python run.py test tests/unit -q
```

Run only the end-to-end tests:

```bash
uv run python run.py test tests/e2e
```

To keep the end-to-end stack running afterwards for debugging:

```bash
E2E_KEEP_STACK=1 uv run python run.py test tests/e2e
```

To start your local stack and then run the suite in one step:

```bash
uv run python run.py up --test
```

Extra arguments after `test` are passed to pytest, for example `-k export -x`.

- **Unit tests** (`tests/unit/`) cover CSV parsing and normalization, filter validation, error responses and the OpenAPI spec.
- **End-to-end tests** (`tests/e2e/`) send real HTTP requests to the running containers. They cover import then search, deduplication on re-import, rating merges, export round-trips, SSE progress, bad rows, and error cases (`401`, `413`, `422`, unknown jobs).

## Project layout

```
movie_service/
  app/
    main.py        # FastAPI app; startup opens the DB pool and runs migrations
    config.py      # settings from environment variables
    errors.py      # ApiError and the ErrorResponse handlers
    db/            # connection pool, migration runner, migrations/NNN_*.sql
    routers/       # health, movies, genres, imports, exports, jobs, docs
    services/      # search, caching, CSV parsing, uploads, import merge, export, jobs, SSE fan-out, storage
    worker/        # job worker (python -m app.worker)
    domain/        # plain dataclasses and enums (Movie, Job)
    schemas/       # Pydantic API models
  tests/unit/      # fast tests, no Docker
  tests/e2e/       # HTTP tests against a pytest-docker stack
  run.py           # one-command runner
  docker-compose.yml, Dockerfile, .env.example
  DESIGN.md        # full design and decision log
  AGENTS.md        # guidance for AI coding agents (worktrees, conventions)
```
