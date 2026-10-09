# Movie service

A FastAPI service for a movie database: upload a movies CSV, search by year range and genres,
download everything as a gzipped CSV, and follow long-running jobs in real time over
server-sent events. Postgres stores the data, a worker container runs imports and exports, and
Redis is an optional response cache.

See [DESIGN.md](DESIGN.md) for the architecture and API, and [AGENTS.md](AGENTS.md) for
contributor conventions.

## Local setup

Prerequisites:

- Docker with the Compose plugin (`docker compose version`)
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). uv
  installs the pinned Python 3.13 itself.

Run everything from this directory (`movie_service/`):

```bash
uv sync                               # install dependencies into .venv
cp .env.example .env                  # optional: edit ports, passwords, limits
uv run python run.py up --seed        # build and start the stack, then import movies.csv
```

`run.py up` prints the API URL (`http://localhost:8000` unless that port is taken). Interactive
docs are at `/docs`. Other commands:

```bash
uv run python run.py export           # download the database as movies-export.csv.gz
uv run python run.py logs             # follow service logs
uv run python run.py test             # unit tests + e2e tests (e2e starts its own isolated stack)
uv run python run.py down             # stop the stack (--volumes also deletes the data)
```

### Running the API outside Docker

To use `fastapi dev` with auto-reload, start only Postgres in Docker and point the
app at it. In `.env`, set `POSTGRES_PORT=5432` (by default Postgres gets a random localhost
port) and an `API_KEY` (see [API keys](#api-keys); `run.py up` adds one if you've run it once),
then:

```bash
docker compose up -d postgres
DATA_DIR=./data REDIS_URL= uv run fastapi dev --port 8001    # API without the cache; docs at :8001/docs
DATA_DIR=./data uv run python -m app.worker                  # worker, in a second terminal
```

The API and worker read `API_KEY` and the other settings from `.env`. Give them the same
`DATA_DIR` so the worker can find the files the API uploads.

## API keys

Write endpoints (`POST /api/v1/imports` and `POST /api/v1/exports`) need an `X-API-Key` header
matching the `API_KEY` setting. Reads are open (search, genres, job status and events, export
downloads).

- **No default.** The API and the worker refuse to start if `API_KEY` is missing or shorter
  than 16 characters. A deployment that forgets to set it fails at startup instead of accepting
  writes with a well-known key. The rejected value is never written to the logs.
- **Locally, `run.py up` creates one for you.** If `API_KEY` isn't set in the environment or
  in `.env`, `run.py up` generates a random key and writes it to `.env`, which git ignores.
  `run.py up --seed` and `run.py export` read the key from there automatically.
- **Use it** from curl or any HTTP client:

  ```bash
  curl -H "X-API-Key: $(grep '^API_KEY=' .env | cut -d= -f2-)" \
       -F file=@movies.csv http://localhost:8000/api/v1/imports
  ```

  In Swagger UI (`/docs`), click **Authorize** and paste the key.
- **Choose your own** key by setting `API_KEY` in `.env` or the environment. Generate one with
  `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
- **Rotate** a key by changing the value and running `uv run python run.py up` again, which
  recreates the containers with the new value.
- **In deployed environments**, supply `API_KEY` from your platform's secret store as an
  environment variable. Never commit it, and use a different key for each environment.
