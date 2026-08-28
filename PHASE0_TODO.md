# Phase 0 — remaining work

Handoff note. Everything in `README.md` is accurate; this is what is *not* done yet.

## Still open

- **Full 2000-item ingest is not achievable while the upstream outage lasts.**
  Not a pagination bug — **Jikan is serving only its warm cache.** Anything that
  needs a live MyAnimeList fetch returns:

  ```
  {"status":504,"type":"BadResponseException",
   "message":"Jikan failed to connect to MyAnimeList..."}
  ```

  Measured, 2026-08-28:

  | Request | Result |
  |---|---|
  | `/top/anime?page=1` (any `limit` ≤ 25) | **200** — 25 items |
  | `/top/anime?page=2` | **504**, 0/8 attempts — deterministic |
  | `limit` > 25 | 504 |
  | `/anime/{id}`, *popular* id (5114, 1535, 21, 20) | **200**, 4/4 |
  | `/anime/{id}`, *ordinary* id (501, 1501, 3001, 7777) | **504**, 0/12, 3 tries each |
  | sequential id sample (50 ids across 1–3010) | 5 ok / 44 5xx / 1 404 |
  | search, seasons, filters, recommendations, relations, schedules | 504 |
  | `/genres/anime` | 200 (static) |

  **Beware the sampling trap:** probing `/anime/{id}` with famous ids gives 9/10
  and reads as "id lookups work". They do not — those ids are simply cached.
  A sequential id walk 504s on nearly every id and is *not* a viable workaround;
  neither is graph traversal, since `recommendations` and `relations` are down
  too. The reachable set is the cached popular head, which is what page 1
  already gives us.

  So the catalog tops out at **25 items** today. Nothing local widens it.
  Re-run `bootstrap.py` once MAL is healthy; upserts are idempotent and the
  remaining pages fill in as updates (verified: a second run reports
  `0 inserted, 25 updated`).

## Done since the last note

- **`tests/` is written — 181 tests, none touching the network or
  `%LOCALAPPDATA%`.** `uv run pytest` is green.
  - `conftest.py` — redirects `DATA_DIR` to `tmp_path` via env var (env wins over
    `.env` in pydantic-settings) and clears the `get_settings` LRU on both sides.
    Also no-ops `setup_logging`, which otherwise pins an open file handle inside a
    tmp dir that pytest then fails to delete on Windows.
  - `test_schemas.py` (40) — uid derivation and mismatch rejection, 0–10 rating
    bound, year/​`n_ratings` bounds, tag + alt-title normalisation, tz coercion,
    row round-trip.
  - `test_jikan_mapping.py` (54) — fixture → UnifiedItem, MAL footer stripping,
    year fallback via `aired.prop.from.year`, image/​score/​title fallback chains,
    skip conditions, malformed-entry tolerance.
  - `test_db_upsert.py` (52) — insert/update accounting, within-batch dedup,
    >500-id chunking, **FTS sync across insert/update/delete**, bm25 weighting,
    and FTS operators (`AND`, `*`, `"`, `NEAR`) typed by a user staying inert.
  - `test_api.py` (30) — four routes against a seeded temp DB, 404, 422 on a bad
    domain, paging bounds, static UI, `/api/*` winning over the `/` mount.
  - `test_ingest_run.py` (25) — the generic run loop, driven by fake ingesters.

- **pywebview window verified.** Edge WebView2 runtime is present
  (`152.0.4191.53`), the winforms backend loads with the `edgechromium` renderer,
  and `uv run python app.py` opens a real native window titled "Loom" — no
  browser fallback. This was untested before.

## Two bugs found and fixed while testing

- **`ingest/base.py` discarded a partial run.** When `fetch()` raised mid-stream
  the exception skipped the post-loop `if batch:` flush, so with `BATCH_SIZE=200`
  a run that fetched 25 items and then hit a 504 persisted **zero** of them — the
  exact shape of the Jikan outage above. The buffered batch is now flushed in the
  exception path (guarded separately, since the original failure may itself have
  come from `upsert_items`). This is the difference between a 0-item and a
  25-item catalog today. The run is still recorded as `status="error"`, so it
  does not count as a successful ingest in `last_ingest_at` / `domain_stats`.
  Covered by `test_partial_failure_*`.

- **`ingest/anime_jikan.py` title fallback short-circuited on whitespace.**
  `raw.get("title") or raw.get("title_english") or ...` — a whitespace-only
  `title` is truthy, so it hid a perfectly good `title_english` behind it. Real
  data is rescued by the `titles[Default]` fallback, but a record with a blank
  title, a good English title and no `Default` entry was dropped entirely. Now
  picks the first non-blank candidate.

## Notes for any further machine

- `DATA_DIR` defaults to `%LOCALAPPDATA%\Loom`, so the catalog does not come with
  the clone. Run `bootstrap.py` once.
- If TLS fails with `CERTIFICATE_VERIFY_FAILED`, that is the corporate proxy;
  `src/recsys/net.py` already handles it via `truststore`. On a home network it is
  a no-op — confirmed no-op here.
- `uv sync` pins everything from `uv.lock`. All wheels — nothing compiles.
- Fixtures are UTF-8 and hold Japanese titles; read them with an explicit
  `encoding="utf-8"`, since Python on Windows defaults `open()` to cp1252.
