# Movie Service — Design

Status: **Ready for review**. Decided items are listed in [Decisions](#decisions).

## 1. Requirements

| # | Requirement |
|---|---|
| R1 | Import a movie database CSV (`movies.csv` format). |
| R2 | Merge and deduplicate across multiple imports (and within one import). |
| R3 | Query movies by year range and one or more genres, returning a list of movies. |
| R4 | Download the whole movie database as a gzipped CSV. |
| R5 | Real-time progress updates for requests that run longer than 2 seconds. |
| R6 | Easy to run: one Python command starts the Docker services and, optionally, runs the test suite. |

Non-functional: performance and responsiveness, efficient CPU and memory, graceful error handling. Design for datasets much larger than the sample. Cloud-agnostic, containerized, horizontally scalable API. Postgres is the database.

## 2. What the data looks like

Analysis of the sample `movies.csv` (`movie_name,year,genres,rating`):

| Finding | Count | Design impact |
|---|---|---|
| Rows | 367,314 | Large enough that row-by-row inserts are too slow; bulk-load with `COPY`. |
| Exact duplicate rows | 122,771 (33%) | Deduplication is needed within a single file, not just across imports. |
| Missing year | 53,248 | `year` is nullable. |
| IMDb disambiguator (`I`, `II`, …) in the year column | 12,812 | Treated as unknown year (`NULL`). The real year was lost upstream. |
| Missing rating | 137,360 | `rating` is nullable. |
| Same title + year, different genres/rating | 2,095 | Mix of genuinely different films (*The Stranger* 2022: Horror vs. Crime/Drama) and the same film with an updated rating (*Minions: The Rise of Gru* 6.5 vs. 6.6). Drives the dedup key choice (§5). |
| Distinct genres | 27 | Small, fixed vocabulary; a lookup table works well. |
| Year range / rating range | 1894–2029 / 1.0–10.0 | Validate on import. |

There is no natural unique ID in the data; titles alone are not unique (81,538 titles appear more than once).

"Date range" in the requirements maps to **year range**, because year is the only date in the data.

## 3. Architecture

```
            ┌──────────────┐        ┌───────────────┐
 client ──▶ │  api (N)     │──SQL──▶│               │
   ▲        │  FastAPI     │◀─NOTIFY│   postgres    │
   │ SSE    └──────┬───────┘        │               │
   └───────────────┘   ▲            │  movies       │
                       │ files      │  genres       │
            ┌──────────┴───┐        │  jobs  …      │
            │  worker (M)  │──SQL──▶│               │
            └──────────────┘        └───────────────┘
                 shared volume: /data (uploads, exports)
```

Three containers, run with Docker Compose:

- **api**: FastAPI app. Stateless, so it can be scaled to N replicas. Handles requests, accepts uploads, enqueues jobs and streams progress. Never does long-running work itself.
- **worker**: runs imports and exports. Scales independently of the API.
- **postgres**: data, plus the job queue and progress notifications.

**Job queue:** a `jobs` table in Postgres. Workers claim jobs with `SELECT … FOR UPDATE SKIP LOCKED`. Progress updates are written to the row and broadcast with `NOTIFY`. This keeps jobs durable across restarts without adding Redis.

**File storage:** uploads and exports live on a shared volume behind a small `Storage` interface (`LocalStorage` now, with S3/GCS possible later), so the design stays cloud-agnostic.

Code layout, extending the current skeleton:

```
app/
  main.py
  config.py           # settings from env vars (pydantic-settings)
  routers/            # movies.py, imports.py, exports.py, jobs.py
  schemas/            # one file per API schema
  domain/             # Movie, Job dataclasses
  db/                 # connection pool, SQL, migrations
  services/           # import, export, query logic
  worker/             # job runner entrypoint
tests/
run.py                # R6: one-command runner
docker-compose.yml
Dockerfile
```

## 4. Data model

Normalized (3NF), snake_case, plural table names.

```sql
movies (
  id            bigint generated always as identity primary key,
  title         text        not null,
  title_key     text        not null,   -- normalized title: lowercased, whitespace collapsed
  year          smallint,               -- null = unknown
  genre_key     text        not null,   -- sorted genre names joined with '|' (dedup only)
  rating        numeric(3,1),           -- null = unrated
  first_import_id bigint references jobs(id),
  last_import_id  bigint references jobs(id),
  updated_at    timestamptz not null default now(),
  unique nulls not distinct (title_key, year, genre_key)
)

genres (
  id    smallint generated always as identity primary key,
  name  text not null unique
)

movie_genres (
  movie_id  bigint   references movies(id) on delete cascade,
  genre_id  smallint references genres(id),
  primary key (movie_id, genre_id)
)

jobs (
  id            bigint generated always as identity primary key,
  type          text not null,          -- 'import' | 'export'
  status        text not null,          -- 'queued' | 'running' | 'succeeded' | 'failed'
  progress      real not null default 0, -- 0.0–1.0
  processed_rows bigint, total_rows bigint,
  result        jsonb,                  -- e.g. {inserted, updated, duplicates, rejected} or export file path
  error         text,
  created_at, started_at, finished_at timestamptz
)
```

Indexes: `movies (year)`, `movie_genres (genre_id, movie_id)`, `jobs (status, created_at)`.

`genre_key` is a derived column that exists only to back the uniqueness constraint. The source of truth for genres is `movie_genres`.

## 5. Merge and deduplication (R2)

**Identity key:** `(title_key, year, genre_key)`.

- Exact duplicates collapse to one row.
- Same title and year with different genres stay separate films (e.g. the two *The Stranger* 2022 entries).
- Missing years count as equal for matching (`NULLS NOT DISTINCT`, Postgres 15+).

**When an import matches an existing movie:** the latest non-null rating wins, `last_import_id` is updated, and other fields are unchanged.

**Within a single file:** duplicate keys are collapsed in the staging table first, keeping the last row's rating.

Import results report `inserted`, `updated`, `unchanged`, `duplicates_in_file` and `rejected` counts.

## 6. API

Versioned under `/api/v1`. Plural nouns, standard status codes, JSON errors.

**Authentication:** write endpoints (`POST /imports`, `POST /exports`) need an `X-API-Key` header that matches the `API_KEY` env var (constant-time comparison). Read endpoints (search, job status and events, export download) are open. A missing or wrong key returns `401`. The check is a FastAPI dependency, so it can later be replaced by OAuth or an API gateway.

| Method | Path | Purpose | Response |
|---|---|---|---|
| `POST` | `/imports` | Upload a CSV (multipart). Streamed to storage; an import job is queued. Needs API key. | `202` + `JobRead`, `Location: /jobs/{id}` |
| `GET` | `/movies` | Search (R3). | `200` + `Page[MovieRead]` |
| `GET` | `/movies/{movie_id}` | Get one movie. | `200` / `404` |
| `POST` | `/exports` | Start a gzipped CSV export of the whole database (R4). Needs API key. | `202` + `JobRead` |
| `GET` | `/exports/{job_id}/file` | Download the finished export (`application/gzip`). | `200` / `409` if not ready |
| `GET` | `/jobs/{job_id}` | Job status snapshot. | `200` + `JobRead` |
| `GET` | `/jobs/{job_id}/events` | Live progress via server-sent events (R5). | `text/event-stream` |
| `GET` | `/health` | Liveness and database check. | `200` / `503` |

**Search parameters:** `year_from`, `year_to` (inclusive integers), `genre` (repeatable: `?genre=Action&genre=Drama`), `genre_match=any|all` (default `any`), `limit` (default 50, max 1000), `cursor`.

- Pagination is keyset-based (an opaque cursor over `id`), so deep pages stay fast on large tables.
- Movies with an unknown year are excluded when either year bound is set.
- Unknown genre names return `422`.

**Schemas** (one per file in `app/schemas/`): `MovieRead`, `MovieFilter`, `Page[T]`, `JobRead`, `ErrorResponse`.

```json
// MovieRead
{"id": 42, "title": "Glass Onion", "year": 2022, "genres": ["Comedy", "Crime", "Drama"], "rating": 7.2}

// JobRead
{"id": 7, "type": "import", "status": "running", "progress": 0.43,
 "processed_rows": 158000, "total_rows": null, "result": null, "error": null}

// ErrorResponse
{"error": {"code": "invalid_csv", "message": "Missing required column: year", "details": {...}}}
```

## 7. Long-running work and progress (R5)

Imports and exports always run as jobs, because their duration grows with the data. The flow:

1. The client calls `POST /imports` or `POST /exports` and gets `202` back immediately with a job ID.
2. A worker claims the job and updates `progress` and `processed_rows` at most every ~250 ms or every N rows, issuing `NOTIFY job_progress, '<id>'` with each update.
3. `GET /jobs/{id}/events` sends the current state immediately, then pushes an event on each notification, and closes after a final `succeeded` or `failed` event. Clients that can't use SSE poll `GET /jobs/{id}`.

Import progress is measured by bytes read divided by file size, which works without counting rows first. Export progress is rows written divided by the movie count.

Search is synchronous. It is kept under 2 seconds with indexes, keyset pagination and the `limit` cap, and a database statement timeout returns a clean error instead of hanging.

## 8. Import pipeline (R1)

1. **Upload:** the API streams the request body to storage in chunks (constant memory) and checks the header row.
   - **Size limit:** `MAX_UPLOAD_BYTES`, default **64 MB**. This is sized for about 750,000 movies: the sample averages 45 bytes per row (about 34 MB for 750k rows) and a 99th-percentile row is 81 bytes (about 61 MB). The limit is enforced while streaming, so an oversized upload is cut off early with `413` and the partial file is deleted.
   - **Header:** the required columns (`movie_name`, `year`, `genres`, `rating`) must be present, in any order; otherwise `422`. Extra columns are ignored and listed in `result.warnings`.
   - Then it creates a job and returns `202`.
2. **Parse:** the worker reads the file with `csv.reader` in batches of about 10k rows and normalizes each row (`Movie.from_csv_row`). Bad rows (unparseable year or rating, missing title, wrong field count) are skipped: they are counted in `result.rejected`, and the first 100 are recorded with line numbers and reasons in `result.rejected_samples`. Bad rows never fail the job. Only file-level problems do (not a CSV, unreadable encoding, missing required columns).
3. **Stage:** `COPY` each batch into an unlogged `import_staging` table, tagged with the job ID.
4. **Merge:** set-based SQL in one transaction: upsert genres, collapse duplicates within the file, `INSERT … ON CONFLICT (title_key, year, genre_key) DO UPDATE`, insert `movie_genres`, then clear the staging rows.
5. **Finish:** write the counts to `result` and mark the job `succeeded`. If any step fails, the transaction rolls back and the job is marked `failed` with an error.

Concurrent imports are serialized with a Postgres advisory lock, so merges never race.

## 9. Export pipeline (R4)

The worker runs `COPY (SELECT title, year, genres, rating …) TO STDOUT`, streaming through gzip into storage with constant memory. The CSV columns match the input format, so an export can be re-imported. `GET /exports/{id}/file` streams the finished file.

## 10. Error handling

- Validation errors return `422`. A missing or invalid API key returns `401`. Missing resources return `404`. A job that isn't finished yet returns `409`. Oversized uploads return `413`.
- Unhandled exceptions return `500` with a generic `ErrorResponse`. Details go to the logs, never to the client.
- Every error uses the `ErrorResponse` shape.
- Worker crashes leave a job in `running`. A heartbeat column plus a sweep re-queues or fails jobs whose worker stopped responding.

## 11. Running and testing (R6)

`run.py`, run with `uv run python run.py <command>`:

| Command | Action |
|---|---|
| `up` | `docker compose up -d --build`, wait for health checks, print the API URL. |
| `test` | Run `pytest`. The pytest-docker fixtures start an isolated Compose project (unique project name and ports) and tear it down afterwards. |
| `up --test` | Start the services, then run the tests against them. |
| `down` | Stop the services; `--volumes` also wipes the data. |
| `logs` | Follow the service logs. |

Configuration comes from environment variables (`.env.example` provided), including `API_KEY` and `MAX_UPLOAD_BYTES`. Host ports and `COMPOSE_PROJECT_NAME` are configurable so parallel worktrees don't collide (see `AGENTS.md`).

**Tests:**
- Unit tests for parsing and normalization and for filter validation.
- End-to-end tests per requirement against the real stack:
  - import, then search
  - re-import produces no duplicates
  - merge updates ratings
  - export round-trip (export, then re-import, gives the same count)
  - SSE progress events arrive
  - errors: bad CSV, bad filters, unknown job, missing or wrong API key (`401`), oversized upload (`413`)
  - bad rows are skipped and reported, not fatal

## 12. Technology choices

| Concern | Choice | Why |
|---|---|---|
| DB driver | psycopg 3 (async) + psycopg_pool | Native `COPY` support, async, and `LISTEN/NOTIFY`. |
| Migrations | Plain SQL files applied at startup | Small schema; avoids ORM overhead on bulk paths. |
| Settings | pydantic-settings | Environment-driven config. |
| SSE | `sse-starlette` | Handles keep-alive and disconnects. |
| Tests | pytest, pytest-docker, httpx | End-to-end against real containers. |
| Postgres | 16 | Needed for `NULLS NOT DISTINCT` (Postgres 15+). |

## Decisions

| # | Topic | Decision |
|---|---|---|
| D1 | Dedup key | `(title_key, year, genre_key)`. Same title and year with different genres stay separate films. |
| D2 | Merge rule | The latest non-null rating wins. Genres are part of identity, so they are never merged. |
| D3 | Export | Async job with SSE progress, then download via `GET /exports/{id}/file`. |
| D4 | Workers | A separate worker container with a Postgres-backed job queue (`SKIP LOCKED`) and progress over `LISTEN/NOTIFY`. No Redis. |
| D5 | Upload size | `MAX_UPLOAD_BYTES`, default 64 MB: sized for about 750k movies with headroom for long rows. Oversized uploads get `413`. |
| D6 | CSV columns | Required columns must be present in any order. Extra columns are ignored and reported as warnings. |
| D7 | Auth | An API key (`X-API-Key`) is required for writes only (`POST /imports`, `POST /exports`). Reads are open. |
| D8 | Bad rows | Skipped and reported (count plus samples). Never fail the job; only file-level errors do. |

## Open questions

None currently.
