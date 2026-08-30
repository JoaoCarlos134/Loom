# Loom

Cross-domain hybrid recommender system (anime / manga / books / music / movies / TV) that runs
as a local desktop app. No server, no cloud, no admin rights.

**Status: Phase 3 started** — project skeleton plus one domain (anime) ingested end
to end from three sources: AniList (GraphQL), a static CSV dump (MyAnimeList via
Kaggle) and Jikan. **14,490 works** shown from 27,340 stored rows, 80% carrying a
real synopsis, plus **19.2M user ratings** from 106k users. No recommendation
models yet, by design — the ratings and the text are their fuel.

---

## Setup from zero on a Windows machine without admin rights

Every step below is user-space. Nothing installs a service, needs an installer, or
touches `Program Files`.

### 1. Install `uv`

In PowerShell:

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

This drops `uv.exe` in `%USERPROFILE%\.local\bin` and adds it to your user PATH.
Close and reopen the terminal, then check:

```powershell
uv --version
```

### 2. Clone and sync

```powershell
git clone git@github.com:JoaoCarlos134/Loom.git
cd Loom
uv sync
```

`uv sync` reads `uv.lock`, downloads a private CPython 3.12 into `%LOCALAPPDATA%\uv`,
creates `.venv`, and installs exact pinned versions. Every dependency is a prebuilt
wheel — nothing compiles, so no MSVC build tools are required.

### 3. Configure

```powershell
copy .env.example .env
```

Defaults are fine. `REGION=BR` is carried for future region-aware providers.

### 4. Populate the catalog

```powershell
uv run python scripts/bootstrap.py
```

Initialises the SQLite schema, runs the configured ingesters, prints a summary table.
Idempotent — safe to re-run.

Two sources feed the `anime` domain:

| Ingester | Source | Rows | Cost |
|---|---|---|---|
| `anime_mal_csv` | Kaggle MAL dump, static | ~14,478 items | ~2 GB download once, then ~2 s |
| `anime_jikan` | Jikan v4, live API | up to 2000 items | 3 req/sec limiter |
| `anime_anilist` | AniList GraphQL, live | ~12.8k items | ~10 min, 30 req/min, no API key |
| `manga_anilist` | AniList GraphQL, live | ~3.3k items | ~2 min; a second domain |
| `movie_tmdb` | TMDB, live | ~8k items | ~30 min; **needs a free API credential** |
| `tv_tmdb` | TMDB, live | ~4k items | ~25 min; same credential |
| `book_goodbooks` | goodbooks-10k, GitHub CSVs | 10k items | ~4 s, no credential |
| `music_musicbrainz` | MusicBrainz + Deezer | ~5k artists | hours; MusicBrainz asks 1 req/s |
| `book_openlibrary` | Open Library | ~9k enriched | ~30 min; the synopses goodbooks lacks |
| `anime_mal_ratings` | same Kaggle archive | 19.2M ratings | ~75 s, grows the DB to ~1.3 GB |

`anime_mal_ratings` is **not** in the default `INGESTERS` list. It is a one-off
bulk load of Phase 3's training data rather than part of building the catalog,
and it turns a 34 MB database into a 1.3 GB one. Run it deliberately:

```powershell
uv run python scripts/bootstrap.py --only anime_mal_ratings
```

`anime_mal_csv` downloads `azathoth42/myanimelist` through kagglehub on first
run (cached in `~/.cache/kagglehub`, so later runs are offline) and reads only
the anime table — the multi-GB user-ratings files in that dataset are ignored
for now. Point `MAL_CSV_PATH` at a local file to skip the download entirely.

> **Jikan is currently degraded** and serves only its warm cache: page 1 of
> `/top/anime` works, everything deeper 504s, so it contributes ~25 items and
> burns ~20 s exhausting retries each run. Drop it from `INGESTERS` if that is
> not worth it; the run is still recorded honestly as `status=error` and does
> not count as a successful ingest.

Useful flags:

```powershell
uv run python scripts/bootstrap.py --limit 100          # small run while developing
uv run python scripts/bootstrap.py --only anime_mal_ratings  # load the 19.2M ratings
uv run python scripts/bootstrap.py --no-ingest          # schema only
uv run python scripts/bootstrap.py --only anime_mal_csv # skip the live API
```

### 5. Run the app

```powershell
uv run python app.py
```

Opens a native window. Fallbacks:

```powershell
uv run python app.py --browser   # skip the webview, use the default browser
uv run python app.py --no-ui     # serve only, for curl/httpie against the API
```

### 6. Tests

```powershell
uv run pytest
```

No test touches the network.

---

## Where the data lives

**Not in the repo.** `DATA_DIR` defaults to `%LOCALAPPDATA%\Loom`:

```
%LOCALAPPDATA%\Loom\
  recsys.db          SQLite catalog + ratings (+ -wal / -shm sidecars)
  cache\jikan\       HTTP response cache, sharded by URL hash
  logs\recsys.log    rotating log
```

This is deliberate. The repo lives under a OneDrive-synced folder on the primary
machine, and sync clients take file locks on SQLite WAL sidecars mid-write, which
surfaces as random `database is locked` errors. `%LOCALAPPDATA%` is never synced.
Override with `DATA_DIR` in `.env` if you want the data beside the code.

Consequence worth knowing: the catalog does **not** travel between machines. Each
machine runs `bootstrap.py` once. That is the intended reproducibility story — the
repo reproduces the *catalog build*, not the catalog file.

---

## Corporate TLS interception

On networks that terminate TLS at an inspecting proxy (the primary dev machine's is
one), Python rejects the proxy's re-signed certificates because `ssl` uses certifi's
bundle rather than the Windows certificate store, where the corporate CA actually
lives. Symptom:

```
CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain
```

`src/recsys/net.py` calls `truststore.inject_into_ssl()` before the first handshake,
which points `ssl` at the OS trust store. Pure Python, no admin, and verification
stays **on** — we are not disabling certificate checks.

---

## Architecture

```
app.py                    pywebview window  ──┐
                                              │ http://127.0.0.1:<free port>
src/recsys/api/server.py  FastAPI  ───────────┘
              routes.py   /api/health /api/items /api/items/{uid}
                          /api/similar/{uid} /api/stats
              ui/         plain HTML + CSS + vanilla JS, served as static files

src/recsys/db.py          SQLite: items + items_fts (FTS5) + ratings + links
                                  + popularity_norm + works + relations
                                  + ingest_runs
           link.py        entity resolution: which rows are one work
           popularity.py  one popularity scale across sources
           works.py       the read view: one row per work, sort key on the row
           relations.py   adaptation and franchise edges, out of raw_json
           prune.py       drop rows the ingest filters would reject

src/recsys/models/       reads the catalog, writes derived artifacts
              text.py    documents -> embedding matrix (parquet)
              similarity.py  brute-force nearest neighbours
              evaluate.py    recall against known adaptation links
              collaborative.py  item-item CF over 19.2M ratings (parquet)
           schemas.py     UnifiedItem — the one row shape all domains collapse into
           config.py      pydantic-settings, the only reader of the environment

src/recsys/ingest/
  base.py                 Ingester ABC + the generic fetch→map→upsert run loop,
                          and IngestRunner, the Protocol bootstrap runs against
  registry.py             name → ingester. The single seam for new domains.
  ratelimit.py            sliding-window limiter (3/s AND 60/min) + jittered backoff
  cache.py                on-disk response cache keyed by URL
  kaggle.py               locate a file in a Kaggle dataset, download if absent
  anime_jikan.py          Jikan v4 → UnifiedItem
  anime_mal_csv.py        Kaggle MAL CSV dump → UnifiedItem
  anilist.py              shared AniList client + mapper, used by both domains
  tmdb.py                 shared TMDB client + mapper, used by movie and tv
  anime_anilist.py        AniList ANIME → UnifiedItem (the source with text)
  manga_anilist.py        AniList MANGA → UnifiedItem, via adaptation edges
  movie_tmdb.py           TMDB film → UnifiedItem
  tv_tmdb.py              TMDB television → UnifiedItem
  book_goodbooks.py       goodbooks-10k CSVs → UnifiedItem
  music_musicbrainz.py    composers the catalog credits → UnifiedItem
  book_openlibrary.py     Open Library work text → UnifiedItem
  ratings_mal_csv.py      Kaggle MAL user lists → ratings rows (a sibling
                          contract, not an Ingester — see below)
```

Adding `anime_mal_csv` cost exactly what the abstraction promised: one new file
in `ingest/`, one line in `registry.py`, and settings in `config.py`. No edit to
`db.py`, `routes.py`, `bootstrap.py` or the UI — `bootstrap.py` picks it up
through the registry and sets `target_count` generically.

### Why these choices

**One table, not one table per domain.** A hybrid recommender's whole value is
scoring an anime and a novel on the same axes. Per-domain tables would push that join
into the model layer, where it becomes a permanent tax. `UnifiedItem` pays the
normalisation cost once, at ingest.

**`item_uid` is derived, never assigned.** It is always
`"{domain}:{source}:{source_id}"`, computed by a pydantic validator, so it cannot
drift from its parts. Two sources describing the same work stay separate rows —
deduplication is Phase 2's job and needs both rows to survive to do it.

**`mean_rating` is normalised at ingest; `popularity` is calibrated after it.**
Rating scales are known up front, so each ingester rescales its own to 0–10.
Popularity cannot work that way: a raw audience count means something different
in every source, and no ingester can know the others' scales. So it is stored
raw and reconciled afterwards.

The reconciliation is in `src/recsys/popularity.py`, run straight after linking
because it needs to know which rows are the same work. Where two sources both
carry a work, that pair of numbers is an anchor; the anchors define a mapping
from one source's scale onto the reference's, and the result is written to
`popularity_norm`. Reads order by that. Raw `popularity` is never modified — it
is the honest source datum, and this is a second opinion stored beside it.

It was a real problem, not a theoretical one. *Fullmetal Alchemist:
Brotherhood* is one work carried by all three sources:

| | raw | calibrated |
|---|---|---|
| `anilist` | 724,564 | 853,610 |
| `jikan` | 3,709,700 | 1,265,703 |
| `mal` | 1,199,091 | 1,199,091 |

A 5.1× spread on vintage and definition alone, down to 1.48×. Before this,
Jikan's *least* popular row outranked AniList's most popular and Jikan held the
top seven slots of every listing.

Two things this deliberately is **not**:

- **Not percentile-within-source.** The obvious fix, and wrong: it assumes every
  source samples the same population. Jikan's rows are the *top* 25 anime, so
  ranking them among themselves puts its weakest row — still a top-25 title —
  last in the catalog. It inverts the error rather than fixing it.
- **Not a formula.** A fitted power law was measurably the wrong shape: against
  12,821 anchors it overestimated the quiet end by 1.47× and underestimated the
  popular end by 0.74×. The mapping is a binned monotone curve instead, which
  assumes only that the relationship increases.

The reference source is whichever covers most of the domain, chosen from the
data — naming one would mean editing `popularity.py` to add a source. A domain's
first source is its own reference, so books arriving with nothing to calibrate
against simply work. With too few anchors to shape a curve it falls back to a
median scale factor, and with none at all to quantile mapping, which is the only
path that reintroduces the representative-sample assumption and therefore warns.

**Three sources describe the same anime, and the catalog shows it once.** This
used to be an open wart: `anime:jikan:5114` and `anime:mal:5114` are one work,
and both appeared in search. Adding AniList would have made it three.

The fix is *resolution*, not merging. `src/recsys/link.py` groups rows by an
exact shared `external_ids.mal_id` — every anime source populates it, so this is
an integer join with no thresholds and no fuzzy title matching — and writes a
`links` row per group naming which row represents the work. `query_items`,
`count_items` and `domain_stats` show only representatives; `get_item` still
reaches any row by uid, and `link.work_members()` returns a work's full set.

Nothing is merged and nothing is deleted, which matters more than it looks:

- **Phase 3 wants the union, not the winner.** AniList brings the synopsis and
  the ranked tags; the 2018 dump brings a member count from a different era.
  A merge would have to pick one of each and throw the rest away, permanently.
- **A source stays removable.** Drop an ingester, re-run the linker, and the
  catalog is exactly what the remaining sources say. After a merge there is no
  way back.
- **`item_uid` stays derived.** Merging would need a synthetic row belonging to
  no source, and every id in the system is derived from its parts on purpose.

**The representative is chosen by content, never by source name.** `_richness()`
prefers a row that has a synopsis, then more tags, then more ratings, with a
deterministic tiebreak so a rebuild never reshuffles the catalog. This is why
AniList rows win today — they carry text — but the rule never mentions AniList.
A source-preference list would put source names inside `link.py` and `db.py` and
make "add a source" mean "edit them", which is exactly the coupling the
project's one invariant forbids.

Resolution runs from `bootstrap.py` after ingest, never as a registry entry: it
has to see every source's rows, and a registry entry would let `INGESTERS`
ordering put it first. It is a full rebuild each time — tens of thousands of
rows, well under a second, and an incremental linker would have to reason about
rows whose ids changed underneath it. `--no-link` skips it.

**Ratings got a sibling contract, not an `Ingester` subclass.** The `Ingester`
pipeline is fetch → `UnifiedItem` → `upsert_items`, and every part of it is
wrong for a rating: it is not a catalog row, it goes to a different table, and
there are 19.2M of them rather than 14.5k. Unifying them would have meant
parameterising the run loop over row type, upsert function and dedup strategy —
an abstraction over two implementations that share only their bookkeeping. So
`ratings_mal_csv.py` shares `IngestResult` and the `ingest_runs` row, duplicates
the ~20-line batching loop, and satisfies a Protocol (`IngestRunner`) instead of
a base class. `bootstrap.py` and `registry.py` cannot tell the two apart.

**Ratings carry no foreign key to `items`.** The two loaders can then run in
either order, and a rating for an anime missing from the catalog survives rather
than being silently dropped. In practice all 19.2M resolve — the join is exact,
because both sides derive their uid through the same `make_item_uid`.

**The `ratings` table has no secondary index.** Its only reader will be a bulk
export to parquet, which is a full scan; an index on `item_uid` would cost a
random-order build over 19.2M rows at load time and buy nothing yet. Append a
migration when a query actually needs one.

**`raw_json` is stored per item.** A mapping bug is then fixable offline: correct
`map_item`, re-run the mapper over the DB, never re-hit the API.

**FTS5 with an external-content table.** The index stores no duplicate copy of the
text; three triggers keep it in sync with `items`. User input is tokenised and quoted
before it reaches `MATCH`, so `AND` / `*` / `NEAR` typed into the search box are
treated as ordinary words rather than as query syntax.

**Migrations-lite over Alembic.** `PRAGMA user_version` plus an ordered list of SQL
scripts in `db.py`. A single-file local database does not justify a migration
framework. Never edit a shipped migration; append a new one.

**Sync SQLite, async HTTP.** Ingest is network-bound and benefits from `asyncio`;
queries are local-file-bound and finish in microseconds, where a connection per
request is simpler and safer than sharing one across FastAPI's threadpool.

**No frontend build step.** Plain HTML/CSS/JS served by FastAPI, no CDN, no npm.
A build step would mean Node, and Node is off the table. All DOM insertion goes
through `textContent` — catalog synopses are third-party text and never touch
`innerHTML`.

---

## Where each future phase plugs in

| Phase | Work | Touches |
|---|---|---|
| 1 | More domains: books (OpenLibrary), movies/TV (TMDB) | one new file in `ingest/` + one line in `registry.py` |
| 1 | Cross-source id linking | `external_ids` — already populated |
| 2 | Entity resolution (same work, two sources) | new `links` table via a new migration |
| 2 | Feature matrices | pyarrow → parquet under `DATA_DIR`, read from `items` |
| 3 | Content-based / collaborative / hybrid models | new `recsys/models/` package; reads parquet, writes scores |
| 3 | ~~User ratings and feedback~~ **done** | `ratings` table, migration 2, `ratings_mal_csv.py` |
| 4 | Region-aware availability | `REGION` setting — already threaded through config |

The invariant: **adding a domain must not require editing `db.py`, `routes.py`,
`bootstrap.py`, or the UI.** If a future ingester forces a change in any of those,
the abstraction in `ingest/base.py` is wrong and should be fixed rather than
worked around.

---

## API

| Route | Notes |
|---|---|
| `GET /api/health` | liveness + item count; `app.py` polls this before opening the window |
| `GET /api/items?domain=&q=&limit=&offset=` | `q` hits the FTS index, bm25-ranked |
| `GET /api/items/{item_uid}` | `item_uid` is a path param — it contains `:` |
| `GET /api/stats` | per-domain counts, sources, last successful ingest |

Interactive docs at `/docs` while the server runs.

`ratings` is not exposed over the API. Nothing reads it yet — it exists so
Phase 3's models have something to train on, and a route would be a guess at
what they will need.
