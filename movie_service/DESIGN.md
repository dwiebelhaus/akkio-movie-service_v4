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
                              ┌─────────┐
                              │  redis  │  search-result cache
                              └────▲────┘
            ┌──────────────┐       │        ┌───────────────┐
 client ──▶ │  api (N)     │───────┴─SQL───▶│               │
   ▲        │  FastAPI     │◀────NOTIFY─────│   postgres    │
   │ SSE    └──────┬───────┘                │               │
   └───────────────┘   ▲                    │  movies       │
   ETag / 304          │ files              │  genres       │
            ┌──────────┴───┐                │  jobs  …      │
            │  worker (M)  │──────SQL──────▶│               │
            └──────────────┘                └───────────────┘
                 shared volume: /data (uploads, exports)
```

Four containers, run with Docker Compose:

- **api**: FastAPI app. Stateless, so it can be scaled to N replicas. Handles requests, accepts uploads, enqueues jobs and streams progress. Never does long-running work itself.
- **worker**: runs imports and exports. Scales independently of the API.
- **postgres**: data, plus the job queue and progress notifications. The source of truth.
- **redis**: a shared cache of search results (§13). Optional at runtime: if it is down, the API serves from Postgres.

**Job queue:** a `jobs` table in Postgres. Workers claim jobs with `SELECT … FOR UPDATE SKIP LOCKED`. Progress updates are written to the row and broadcast with `NOTIFY`. This keeps jobs durable across restarts; Redis is used only as a cache, never for jobs.

**File storage:** uploads and exports live on a shared volume behind a small `Storage` interface (`LocalStorage` now, with S3/GCS possible later), so the design stays cloud-agnostic.

Code layout:

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
  title_key     text collate "C" not null,  -- name key of the title (§5)
  year          smallint,               -- null = unknown
  genre_key     text collate "C" not null,  -- sorted genre keys joined with '|' (dedup only)
  rating        numeric(3,1),           -- null = unrated
  first_import_id bigint,               -- audit: import that created the row (no FK, D14)
  last_import_id  bigint,               -- audit: last import that changed the rating
  updated_at    timestamptz not null default now(),
  unique nulls not distinct (title_key, year, genre_key)
)

genres (
  id    smallint generated always as identity primary key,
  name  text not null,                        -- display spelling: the first one imported
  key   text collate "C" not null unique      -- name key (§5)
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
  params        jsonb,                  -- job input, e.g. upload path; dataset version for exports
  result        jsonb,                  -- e.g. {inserted, updated, duplicates, rejected} or export file path
  error         text,
  created_at, started_at, finished_at, heartbeat_at timestamptz
)

dataset_state (            -- single row
  version  bigint not null  -- bumped by every import that changes data (§13)
)
```

Indexes: `movies (year)`, `movie_genres (genre_id, movie_id)`, `jobs (status, created_at)`.

`genre_key` is a derived column that exists only to back the uniqueness constraint. The source of truth for genres is `movie_genres`, whose foreign keys to `movies` and `genres` are enforced by Postgres.

The identity keys use the `"C"` collation (byte-wise comparison): they are never used for display ordering, and it makes index maintenance and joins much faster than locale-aware collation.

## 5. Merge and deduplication (R2)

**Identity key:** `(title_key, year, genre_key)`.

**Name normalization:** titles and genre names are cleaned on import: Unicode NFC, control and invisible format characters (zero-width spaces, BOMs) removed, whitespace trimmed and collapsed. Their *name key* is the cleaned name case-folded. Titles keep their cleaned spelling for display. Genres are identified by key, so `sci-fi` and `SCI-FI` link to the existing `Sci-Fi`; a new genre keeps the first spelling seen. Genre filters match by key too.

- Exact duplicates collapse to one row, as do rows that differ only in title case, whitespace, Unicode form or genre case.
- Same title and year with different genres stay separate films (e.g. the two *The Stranger* 2022 entries).
- Missing years count as equal for matching (`NULLS NOT DISTINCT`, Postgres 15+).

**When an import matches an existing movie:** the latest non-null rating wins and other fields are unchanged. A row is only written when its rating actually changes (then `last_import_id` and `updated_at` are set), so re-importing the same file is nearly write-free and doesn't bump the dataset version.

**Within a single file:** duplicate keys are collapsed in the staging table first, keeping the last non-null rating.

Import results report `inserted`, `updated`, `unchanged`, `duplicates_in_file` and `rejected` counts.

## 6. API

Versioned under `/api/v1` (except `GET /health`, which stays at the root for load balancers and orchestrators). Plural nouns, standard status codes, JSON errors.

**Authentication:** write endpoints (`POST /imports`, `POST /exports`) need an `X-API-Key` header that matches the `API_KEY` env var (constant-time comparison). `API_KEY` has no default: the API and worker refuse to start without a key of at least 16 characters (`run.py up` generates one into `.env`; see the root README.md). Read endpoints (search, job status and events, export download) are open. A missing or wrong key returns `401`. The check is a FastAPI dependency, so it can later be replaced by OAuth or an API gateway.

| Method | Path | Purpose | Response |
|---|---|---|---|
| `POST` | `/imports` | Upload a CSV (multipart). Streamed to storage; an import job is queued. Needs API key. | `202` + `JobRead`, `Location: /jobs/{id}` |
| `GET` | `/movies` | Search (R3). Cached (§13). | `200` / `304` + `Page[MovieRead]` |
| `GET` | `/movies/{movie_id}` | Get one movie. Cached (§13). | `200` / `304` / `404` |
| `POST` | `/exports` | Start a gzipped CSV export of the whole database (R4), or reuse a finished or running one for the current dataset version (§13). Needs API key. | `202` new / `200` reused, + `JobRead`, `Location` |
| `GET` | `/exports/{job_id}/file` | Download the finished export (`application/gzip`). | `200` / `409` not ready or failed / `410` replaced |
| `GET` | `/jobs/{job_id}` | Job status snapshot. | `200` + `JobRead` |
| `GET` | `/jobs/{job_id}/events` | Live progress via server-sent events (R5). | `text/event-stream` |
| `GET` | `/health` | Liveness and database check. | `200` / `503` |

**Search parameters:** `year_from`, `year_to` (inclusive integers), `genre` (repeatable: `?genre=Action&genre=Drama`), `genre_match=any|all` (default `any`), `limit` (default 50, max 1000), `cursor`.

- Pagination is keyset-based (an opaque cursor over `id`), so deep pages stay fast on large tables. Results are ordered by `id`, which follows first-import file order.
- Movies with an unknown year are excluded when either year bound is set.
- Genre names are case-insensitive. Unknown genre names return `422` (`unknown_genre`), and so do unknown query parameters (a typo like `genres=` would otherwise silently return unfiltered results).
- Query strategy: genre names are resolved to ids in the app (cached per dataset version), so the planner sees literal ids and their real selectivity. `any` is one semi-join on `movie_genres`; `all` is one semi-join per genre, which stays fast even for common genres (a `GROUP BY … HAVING count(*) = n` took up to 260 ms on the sample). Every filter shape measured under 4 ms on the 242k-movie sample.

**Schemas** (one per file in `app/schemas/`): `MovieRead`, `MovieFilter`, `Page[T]` (`{"items": [...], "next_cursor": "..." | null}`), `JobRead`, `ErrorResponse`, `Health`.

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
3. `GET /jobs/{id}/events` sends the current state immediately, then pushes an event on each notification, and closes after a final `succeeded` or `failed` event. Clients that can't use SSE poll `GET /jobs/{id}`. Each API process keeps one shared snapshot per watched job: a notification triggers one job read for all of that job's streams, not one per client. Open streams are capped per process (`MAX_EVENT_STREAMS`, default 1000); beyond that, `503` with `Retry-After`.

Import progress is measured by bytes read divided by file size during staging, which works without counting rows first, then by chunks of genre links during the merge. The shares (25% staging, 10% movie insert, 65% links) roughly match measured time on a fresh database, so the bar moves steadily. Export progress is rows written divided by the movie count.

Measured on the 367k-row sample (242k movies, 480k links): a first import takes about 37s, of which about 23s is per-row foreign-key checks on `movie_genres` (kept by choice, D14). Re-importing the same file takes about 10s and writes nothing.

Search is synchronous. It is kept under 2 seconds with indexes, keyset pagination and the `limit` cap, and a database statement timeout returns a clean error instead of hanging.

## 8. Import pipeline (R1)

1. **Upload:** the API streams the request body to storage in chunks (constant memory) and checks the header row.
   - **Size limit:** `MAX_UPLOAD_BYTES`, default **64 MB**. This is sized for about 750,000 movies: the sample averages 45 bytes per row (about 34 MB for 750k rows) and a 99th-percentile row is 81 bytes (about 61 MB). The limit is enforced while streaming, so an oversized upload is cut off early with `413` and the partial file is deleted. For multipart uploads the cap covers the whole body (the file plus 64 KB of overhead), and part headers are capped too (8 KB each, 16 per part; `422` beyond that), so extra fields can't be used to stream unbounded data or grow memory.
   - **Header:** the required columns (`movie_name`, `year`, `genres`, `rating`) must be present, in any order; otherwise `422`. Extra columns are ignored and listed in `result.warnings`.
   - Then it creates a job and returns `202`.
2. **Parse:** the worker reads the file with `csv.reader` in batches of about 10k rows and normalizes each row (`Movie.from_csv_row`). Bad rows (unparseable year or rating, missing title, wrong field count, or a title over 500 characters, a genre name over 64, more than 20 genres, or title and genres together over 2,000 bytes, which keeps the identity key under Postgres's btree entry limit) are skipped: they are counted in `result.rejected`, and the first 100 are recorded with line numbers and reasons in `result.rejected_samples`. Reasons quote at most 100 characters of the bad value. Bad rows never fail the job. Only file-level problems do (not a CSV, unreadable encoding, missing required columns, or more than 1,000 new genres in one import, which protects the `smallint` genre ids from being used up).
3. **Stage:** each batch is encoded to `COPY` text format in a thread (one batch ahead, so parsing overlaps the database write) and streamed into a per-import temp table (`ON COMMIT DROP`: not WAL-logged, no cleanup, nothing left behind by a crash).
4. **Merge:** set-based SQL in the same transaction, under the import advisory lock:
   - collapse duplicates within the file into a second temp table and `ANALYZE` it
   - insert genres that don't exist yet (resolved once per distinct `genre_key`, so identity values aren't burned)
   - `UPDATE` matched movies whose rating changed, then `INSERT … WHERE NOT EXISTS` new movies in file order. Under the lock this is safe and about 3× cheaper than `INSERT … ON CONFLICT`; the unique constraint stays as a backstop.
   - insert `movie_genres` for the new movies in chunks of 20k movie ids, reporting progress after each chunk
   - bump the dataset version if anything changed (§13)
5. **Finish:** write the counts to `result` and mark the job `succeeded`. If any step fails, the transaction rolls back and the job is marked `failed` with an error.

Concurrent imports are serialized with a Postgres advisory lock, so merges never race.

## 9. Export pipeline (R4)

1. The worker opens a `REPEATABLE READ, READ ONLY` transaction, so the row count, the dataset version and the rows all come from one consistent snapshot while imports keep running.
2. `COPY (…) TO STDOUT WITH (FORMAT csv, HEADER)` streams `movie_name,year,genres,rating` in id order. Genres come from `movie_genres` (the source of truth) through one grouped join; a per-row lateral subquery took 8s instead of 0.65s on the sample.
3. Rows are gzipped (level 6) in a thread in 1 MB chunks into `exports/{id}.csv.gz.part`, then moved into place atomically, so a crash never leaves a half-written file that looks finished. Progress is rows written over the snapshot's movie count.
4. The result records `rows`, `bytes`, `dataset_version` and `download_url`. Only the newest export file is kept; older downloads return `410 Gone`.

The CSV matches the input format (empty fields for missing year, genres or rating), so an export re-imports with zero changes. Against CSV injection, a title or genre name that starts with `=`, `+`, `-` or `@` (after any `'`s) is written with a leading `'`, so spreadsheets show it as text; the importer removes one such `'`, so the round trip stays exact. On the sample it takes about 1.4s and produces 3.3 MB.

`GET /exports/{id}/file` streams the file (`application/gzip`, `Content-Disposition: attachment`, `Content-Length`). It returns `409` while the export is queued or running, or if it failed, and `410` once a newer export has replaced it.

## 10. Error handling

- Validation errors return `422`. A missing or invalid API key returns `401`. Missing resources return `404`. A job that isn't finished yet returns `409`. Oversized uploads return `413`.
- A query that exceeds `STATEMENT_TIMEOUT_MS` returns `503 query_timeout`, and no free pooled connection returns `503 database_busy`, both with `Retry-After`: the database is overloaded, the request isn't wrong.
- Path ids and pagination cursors must fit a Postgres `bigint` (else `422`): a larger number would be compared as `numeric`, which can't use the primary key index.
- Unhandled exceptions return `500` with a generic `ErrorResponse`. Details go to the logs, never to the client.
- Every error uses the `ErrorResponse` shape.
- Worker crashes leave a job in `running`. Workers refresh `heartbeat_at` while running; a periodic sweep (any worker) marks jobs whose heartbeat is older than `JOB_STALE_SECONDS` as `failed` and notifies listeners, so SSE clients never wait forever. Failing (not re-queuing) avoids crash loops; imports are transactional, so a failed import leaves no partial data. On graceful shutdown (SIGTERM) a worker re-queues its current job.

## 11. Running and testing (R6)

`run.py`, run with `uv run python run.py <command>`:

| Command | Action |
|---|---|
| `up` | `docker compose up -d --build --wait`, print the API URL. `--seed` imports the sample `movies.csv` and shows progress. |
| `test` | Run `pytest`. The pytest-docker fixtures start an isolated Compose project (unique project name and ports) and tear it down afterwards. |
| `up --test` | Start the services, then run the test suite (end-to-end tests still use their own isolated stack, so they never touch your data). |
| `export [-o FILE]` | Request an export (or reuse the current one), show progress, and download it. |
| `down` | Stop the services; `--volumes` also wipes the data. |
| `logs` | Follow the service logs. |

Configuration comes from environment variables (`.env.example` provided), including `API_KEY` and `MAX_UPLOAD_BYTES`. Host ports and `COMPOSE_PROJECT_NAME` are configurable so parallel worktrees don't collide (see `AGENTS.md`).

**Tests:**
- Unit tests for parsing and normalization and for filter validation.
- End-to-end tests per requirement: real HTTP requests with `httpx` against the real stack:
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
| Cache | Redis 7 (`redis` asyncio client) | Shared across API replicas; key-versioned so it never needs explicit invalidation. |

## 13. Caching

Everything keys on the **dataset version** (`dataset_state.version`). An import bumps it in its merge transaction, only when it inserted or updated rows, and issues `NOTIFY dataset_changed`. Each API replica keeps the current version in memory (loaded at startup, updated from the notification), so checking it costs no query. While the listener connection is down the in-memory version is dropped and requests read it from the database, and the listener re-reads it every `DATASET_VERSION_REFRESH_SECONDS` (default 5), so a lost notification can't leave caches stale for long.

**HTTP (all clients, CDNs, proxies):**
- `GET /movies` and `GET /movies/{id}` return `ETag: "v{version}-{hash of normalized query}"` and `Cache-Control: public, max-age=3600` (`CACHE_MAX_AGE_SECONDS`).
- A request with a matching `If-None-Match` gets `304 Not Modified` without touching Postgres or Redis.
- Responses carry `X-Cache: HIT|MISS` to show whether the shared cache served them.
- Trade-off: with a one-hour `max-age`, clients and CDNs may serve results up to an hour old after an import without asking. Revalidation after that is cheap (`304`). Lower `CACHE_MAX_AGE_SECONDS` if fresher results matter more.

**Redis (shared across API replicas):**
- Search pages and single movies are cached as serialized JSON under `movies:v{version}:{query hash}` with a TTL (`CACHE_TTL_SECONDS`, default 1 hour).
- A version bump makes old keys unreachable, so there is no explicit invalidation; the TTL reclaims memory.
- Fail-open: Redis errors and timeouts (250 ms socket timeout) are logged and the request is served from Postgres; Redis is then skipped for `CACHE_RETRY_SECONDS` so an outage costs requests nothing. Redis is never required for correctness, and `GET /health` reports it (`cache: ok|unavailable|disabled`) without failing. Configured with `maxmemory`, `allkeys-lru` and no persistence, and password-protected (`REDIS_PASSWORD`, required; it is passed in a private config file, not on the command line).

**Exports:**
- Each export records the dataset version it was built from. `POST /exports` returns the existing job (`200`, not `202`) if a succeeded export for the current version still has its file, or an export requested at the current version is queued or running; otherwise it queues a new one (`202`). The decision runs under an advisory lock, so concurrent requests share one job.

**Database:** Postgres's own buffer cache handles hot index pages; no extra tuning beyond sensible `shared_buffers`.

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
| D9 | Cache invalidation | A dataset version bumped by imports that change data; every cache key includes it. |
| D10 | HTTP caching | `ETag` + `If-None-Match` → `304` on movie reads, with `Cache-Control: public, max-age=3600` (configurable). |
| D11 | Shared cache | Redis caches search results and single movies across replicas, versioned keys plus TTL, fail-open. Used only as a cache (jobs stay in Postgres, D4). |
| D12 | Export reuse | `POST /exports` reuses a finished or in-progress export for the current dataset version. |
| D13 | Crashed workers | Heartbeat plus sweep marks stale `running` jobs `failed`; graceful shutdown re-queues. |
| D14 | Foreign keys | `movie_genres` keeps both FKs (integrity over ~23s of per-row checks on a first bulk import). `movies.first/last_import_id` are plain audit columns without FKs, which also lets old jobs be pruned. |

## Open questions

None currently.
