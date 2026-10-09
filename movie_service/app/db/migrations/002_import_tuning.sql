-- Import performance and cache invalidation (DESIGN.md §4, §8, §13).

-- Dataset version, bumped in the import transaction whenever an import inserts or updates
-- movies; caches key on it.
create table dataset_state (
    id       boolean primary key default true check (id),  -- single row
    version  bigint  not null default 0
);
insert into dataset_state default values;

-- Identity keys are compared byte-wise: much faster index maintenance and joins than
-- locale-aware collation, and they are never used for display ordering.
alter table movies
    alter column title_key type text collate "C",
    alter column genre_key type text collate "C";

-- Per-row FK checks to jobs cost seconds on bulk imports; these are audit columns, and
-- dropping the FKs also lets old jobs be pruned. movie_genres keeps its FKs.
alter table movies
    drop constraint movies_first_import_id_fkey,
    drop constraint movies_last_import_id_fkey;

-- Imports now stage into a per-transaction temp table instead.
drop table import_staging;
