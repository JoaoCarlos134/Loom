# Loom — roadmap

Where the project is, what comes next, and which datasets feed each step.
Architecture and rationale live in `README.md`; Phase 0's closing state is in
`PHASE0_TODO.md`.

---

## Where we are

**Phase 0 is complete.** Schema, ingest, API, UI, desktop window and 229 tests,
with the `anime` domain populated from two sources.

```
catalog        14,503 items      (34 MB SQLite at %LOCALAPPDATA%\Loom)
  anime:mal      14,478          Kaggle MAL dump, static 2018 snapshot
  anime:jikan        25          live API, capped by the outage below
tests             229            none touch the network or %LOCALAPPDATA%
```

No recommendation models yet, by design.

---

## Phases

| Phase | Work | Plugs into | Status |
|---|---|---|---|
| **0** | Skeleton, schema, one domain end to end | — | **done** |
| **1** | More domains: books, movies/TV, music | one file in `ingest/` + one line in `registry.py` | next |
| **1** | Cross-source id linking | `external_ids` — already populated | ready |
| **2** | Entity resolution (same work, two sources) | new `links` table via a new migration | blocked on 1 |
| **2** | Per-source popularity normalisation | `db.query_items` | **open bug, see below** |
| **2** | Feature matrices | pyarrow → parquet under `DATA_DIR` | — |
| **3** | User ratings and feedback | new `ratings` table via a new migration | **data already on disk** |
| **3** | Content-based / collaborative / hybrid models | new `recsys/models/` package | — |
| **4** | Region-aware availability | `REGION` setting — already threaded through | — |

---

## Next step, and why

**Ingest the MAL ratings tables we already downloaded.** The Kaggle dataset
pulled for Phase 0 contains far more than the anime table we used:

| File | Size | Contents | Used? |
|---|---|---|---|
| `AnimeList.csv` | 10 MB | 14,478 anime | ✅ Phase 0 |
| `UserAnimeList.csv` | 4.65 GB | **~80M** user–anime ratings (raw) | ❌ |
| `animelists_cleaned.csv` | 2.11 GB | ~31M ratings, cleaned dates | ❌ |
| `users_cleaned.csv` | 15 MB | **108,712 users** + demographics | ❌ |

Columns are `username, anime_id, my_score, my_status, my_watched_episodes, …` —
a complete collaborative-filtering matrix, larger than MovieLens-32M, already on
disk with no upstream dependency and no outage risk. Prefer
`animelists_cleaned.csv` (~31M) over the raw file: same shape, sane dates.

This is Phase 3's fuel and it costs one migration plus one loader. It is also
the first real test of whether the ingest abstraction holds for **non-item
data** — `Ingester` currently assumes it yields `UnifiedItem`s, so ratings may
need a sibling contract rather than a subclass. Decide that deliberately.

---

## Datasets for the cross-domain half

Loom promises anime / books / music / movies / TV and has only anime. Sizes are
approximate and unverified — check before committing to one.

| Domain | Dataset | Ratings? | Rough size | Notes |
|---|---|---|---|---|
| Movies | **MovieLens 32M** | ✅ 32M | ~350 MB | Canonical recsys benchmark, free, no auth. `links.csv` maps to IMDb/TMDB ids. |
| Movies/TV | IMDb official TSV dumps | aggregate only | ~1.5 GB | Free, stable. Good metadata, no per-user ratings. |
| Movies/TV | TMDB API | ❌ | — | Rich metadata + posters; needs a free key. |
| Books | **goodbooks-10k** | ✅ ~6M | ~250 MB | Small and clean. Best first book source. |
| Books | OpenLibrary dumps | ❌ | very large | Metadata only; the README's original Phase 1 pick. |
| Books | UCSD Goodreads (McAuley) | ✅ ~876M | very large | Only if goodbooks proves too small. |
| Music | Last.fm 360K | ✅ play counts | ~500 MB | Implicit feedback, not explicit scores. |
| Music | MusicBrainz dumps | ❌ | large | Canonical metadata. |

**Recommended order:** MovieLens 32M → goodbooks-10k → music. Both leaders ship
ratings in the same shape as the MAL data, so one `ratings` table serves all
three, and both carry cross-reference ids that Phase 2's linker can use.

---

## The constraint that shapes Phase 3

**MAL users, MovieLens users and Goodreads users are disjoint sets.** No person
appears in two of them. Collaborative filtering therefore *cannot* bridge
domains here — "you liked this anime, try this book" has no co-occurrence signal
to learn from, because no user has rated both.

Consequences, worth accepting up front:

- **Within a domain:** collaborative filtering works, and works well — ~31M
  anime ratings is plenty.
- **Across domains:** recommendations must come from **content** — shared tags,
  text embeddings, genre space. This is exactly why `UnifiedItem` collapses
  every domain into one table: it is the only thing that makes an anime and a
  novel comparable.

So Phase 3 is really two models, not one. Anything promising cross-domain CF is
promising something the data cannot support.

A cross-domain dataset with overlapping users would change this. None of the
common public ones have it.

---

## Known issues carried forward

- **Cross-source popularity ordering is wrong.** `query_items` sorts by
  `popularity DESC` globally. Jikan's counts are live 2026 figures (avg ~1.1M
  over its 25 rows); the CSV dump is a 2018 snapshot (avg ~23k over 14,478).
  *Fullmetal Alchemist: Brotherhood* reads 3,709,700 in one and 1,199,091 in the
  other — the same work, 3× apart on vintage alone. Every Jikan row therefore
  outranks every CSV row. Fix is per-source normalisation (percentile rank
  within source) at query time — Phase 2, alongside entity resolution.

- **13 duplicate works across sources**, by design. `anime:jikan:5114` and
  `anime:mal:5114` are the same anime; both carry `external_ids.mal_id` as the
  join key. Phase 2 reconciles them.

- **The CSV dump has no synopsis.** `description` falls back to `background`
  (production trivia) at 7.3% coverage, so FTS leans almost entirely on titles
  for the CSV half of the catalog. This matters for content-based similarity in
  Phase 3 — it is the main argument for a crawler or a TMDB-style source that
  carries real text.

- **Jikan is degraded to its warm cache.** Page 1 of `/top/anime` returns 200;
  everything deeper 504s, as do search, seasons, relations, recommendations, and
  `/anime/{id}` for any non-popular id. Not routable around — see
  `PHASE0_TODO.md` for the measured endpoint matrix and the sampling trap that
  makes id lookups look usable. Re-run `bootstrap.py` once it recovers; upserts
  are idempotent.

---

## Storage

Current usage is small: 34 MB catalog, 9.4 GB kagglehub cache. `C:` has ~508 GB
free, so **the external drive is not needed yet.**

When it is, split rather than move everything:

- **Bulk → external drive.** `KAGGLEHUB_CACHE=D:\kagglehub` for raw dumps, and
  Phase 2's parquet feature matrices. This is where the ratings files and any
  new domain dumps will live.
- **SQLite catalog stays on the internal SSD.** It is small and random-access,
  and an external drive is the slower, more fragile place for it — if the drive
  is disconnected the app will not start.

Whichever drive: keep `DATA_DIR` out of any OneDrive/Dropbox-synced folder. Sync
clients lock SQLite WAL sidecars mid-write and produce spurious
`database is locked` errors. That is why the default is `%LOCALAPPDATA%`.
