-- Case-insensitive genre names (DESIGN.md §4, §5).

-- Genres get an identity key (the case-folded name). Genre names imported so far are ASCII,
-- where lower() matches the importer's Python casefold().
alter table genres add column key text collate "C";
update genres set key = lower(name);

-- Merge case variants ("drama" into "Drama"), keeping the oldest genre.
create temp table genre_merge on commit drop as
select id, min(id) over (partition by key) as keep_id from genres;
delete from genre_merge where id = keep_id;

insert into movie_genres (movie_id, genre_id)
select mg.movie_id, gm.keep_id
  from movie_genres mg
  join genre_merge gm on gm.id = mg.genre_id
on conflict do nothing;
delete from movie_genres mg using genre_merge gm where mg.genre_id = gm.id;
delete from genres g using genre_merge gm where g.id = gm.id;

alter table genres
    alter column key set not null,
    add constraint genres_key_key unique (key);

-- movies.genre_key now holds genre keys (case-folded, sorted, unique) instead of names.
create temp table movie_rekey on commit drop as
select m.id, m.title_key, m.year,
       coalesce((select string_agg(distinct k, '|' order by k)
                   from unnest(string_to_array(lower(m.genre_key), '|')) as u(k)), '') collate "C"
           as genre_key
  from movies m;

-- Movies that only differed by genre case become duplicates: keep the oldest one.
delete from movies m
 using (select id, row_number() over (partition by title_key, year, genre_key order by id) as n
          from movie_rekey) d
 where d.id = m.id
   and d.n > 1;

update movies m
   set genre_key = r.genre_key
  from movie_rekey r
 where r.id = m.id
   and m.genre_key <> r.genre_key;

update dataset_state set version = version + 1;
