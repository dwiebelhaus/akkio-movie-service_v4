-- Initial schema (DESIGN.md §4).

create table jobs (
    id              bigint generated always as identity primary key,
    type            text        not null check (type in ('import', 'export')),
    status          text        not null default 'queued'
                                check (status in ('queued', 'running', 'succeeded', 'failed')),
    progress        real        not null default 0 check (progress between 0 and 1),
    processed_rows  bigint,
    total_rows      bigint,
    params          jsonb       not null default '{}',  -- job input, e.g. {"upload_path": ...}
    result          jsonb,                              -- job output, e.g. counts or export path
    error           text,
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    heartbeat_at    timestamptz                         -- refreshed by the worker while running
);

create index jobs_status_created_at_idx on jobs (status, created_at);

create table genres (
    id    smallint generated always as identity primary key,
    name  text not null unique
);

create table movies (
    id               bigint generated always as identity primary key,
    title            text         not null,
    title_key        text         not null,  -- lowercased, whitespace-collapsed title
    year             smallint,               -- null = unknown
    genre_key        text         not null,  -- sorted genre names joined with '|' (dedup only)
    rating           numeric(3,1) check (rating between 0 and 10),  -- null = unrated
    first_import_id  bigint references jobs(id) on delete set null,
    last_import_id   bigint references jobs(id) on delete set null,
    updated_at       timestamptz  not null default now(),
    constraint movies_identity_key unique nulls not distinct (title_key, year, genre_key)
);

create index movies_year_idx on movies (year);

create table movie_genres (
    movie_id  bigint   not null references movies(id) on delete cascade,
    genre_id  smallint not null references genres(id),
    primary key (movie_id, genre_id)
);

create index movie_genres_genre_id_movie_id_idx on movie_genres (genre_id, movie_id);

-- Bulk-load target for imports; rows are tagged with their job and cleared after the merge.
create unlogged table import_staging (
    job_id     bigint   not null,
    line_no    bigint   not null,
    title      text     not null,
    title_key  text     not null,
    year       smallint,
    genre_key  text     not null,
    genres     text[]   not null,
    rating     numeric(3,1)
);

create index import_staging_job_id_idx on import_staging (job_id);
