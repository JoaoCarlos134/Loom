# Loom

Cross-domain hybrid recommender system (anime / books / music / movies / TV) that runs
as a local desktop app. No server, no cloud, no admin rights.

**Status: Phase 0** — project skeleton plus one domain (anime) ingested end to end
from two sources: a live API (Jikan) and a static CSV dump (MyAnimeList via
Kaggle). ~14.5k items. No recommendation models yet, by design.

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

| Ingester | Source | Items | Cost |
|---|---|---|---|
| `anime_mal_csv` | Kaggle MAL dump, static | ~14,478 | ~2 GB download once, then ~2 s |
| `anime_jikan` | Jikan v4, live API | up to 2000 | 3 req/sec limiter |

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
  recsys.db          SQLite catalog (+ -wal / -shm sidecars)
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
              routes.py   /api/health /api/items /api/items/{uid} /api/stats
              ui/         plain HTML + CSS + vanilla JS, served as static files

src/recsys/db.py          SQLite: items + items_fts (FTS5) + ingest_runs
           schemas.py     UnifiedItem — the one row shape all domains collapse into
           config.py      pydantic-settings, the only reader of the environment

src/recsys/ingest/
  base.py                 Ingester ABC + the generic fetch→map→upsert run loop
  registry.py             name → ingester. The single seam for new domains.
  ratelimit.py            sliding-window limiter (3/s AND 60/min) + jittered backoff
  cache.py                on-disk response cache keyed by URL
  anime_jikan.py          Jikan v4 → UnifiedItem
  anime_mal_csv.py        Kaggle MAL CSV dump → UnifiedItem
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

**`mean_rating` is normalised to 0–10 at ingest; `popularity` is not normalised.**
Ratings are comparable across sources once rescaled, so scaling belongs in the
ingester. Popularity is a raw within-source size signal (MAL member count in both
current sources) whose scale is meaningless across sources — pretending otherwise
would silently corrupt any cross-domain ranking.

⚠️ **The second source made this bite.** `query_items` orders by `popularity DESC`
globally, which was harmless with one source and is not any more. Jikan's counts
are live 2026 figures for the top 25 titles (avg ~1.1M); the CSV dump is a 2018
snapshot of the whole catalog (avg ~23k). The same work, *Fullmetal Alchemist:
Brotherhood*, is 3,709,700 in Jikan and 1,199,091 in the dump — a 3× gap that is
pure vintage, not popularity. So all 25 Jikan rows sort above all 14,478 CSV rows
in the default listing. Fixing it properly means per-source normalisation
(percentile rank within source) at query time, which is Phase 2 work; it is
recorded here rather than papered over.

**Two sources means duplicate works, on purpose.** 13 of the 25 Jikan rows
describe an anime the CSV dump also carries, under a different `item_uid`
(`anime:jikan:5114` vs `anime:mal:5114`). Both populate `external_ids.mal_id`,
which is the join key Phase 2's entity resolution will reconcile them on. Search
shows both today.

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
| 3 | User ratings and feedback | new `ratings` table via a new migration |
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
