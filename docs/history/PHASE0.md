# Phase 0 — status

> **Historical snapshot (2026-08).** Kept for the measured Jikan endpoint matrix
> below. Its numbers are long superseded; `README.md` and `ROADMAP.md` are current.

Everything in `README.md` is accurate. Phase 0 is **complete**: schema,
ingest, API, UI, desktop window and tests all verified end to end, with the
`anime` domain populated from two sources.

Catalog as built: **14,503 items** — 14,478 from the MAL CSV dump, 25 from Jikan.

## Done

- **Two ingesters feeding `anime`.**
  - `anime_mal_csv` — Kaggle `azathoth42/myanimelist`, `AnimeList.csv`,
    14,478 rows, all mapping cleanly (0 skipped) in ~2 s. Field coverage:
    year 98.5%, rating 97.6%, popularity 100%, tags 100%, image 99.3%,
    creators 81.5%, description 7.3%.
  - `anime_jikan` — live API, currently capped at 25 items by the outage below.

  Adding the second source cost one file in `ingest/` plus one line in
  `registry.py`, with settings in `config.py`. `db.py`, `routes.py`,
  `bootstrap.py` and the UI were untouched — the invariant the README sets out
  held under its first real test.

- **229 tests**, none touching the network or `%LOCALAPPDATA%`. `uv run pytest`
  is green. Covers schemas, both mappers, DB/FTS sync, the API, and the generic
  run loop.

- **pywebview verified** — WebView2 `152.0.4191.53`, `edgechromium` renderer,
  real native window titled "Loom", no browser fallback.

## Known issues, recorded not hidden

- **Cross-source popularity ordering is wrong.** `query_items` sorts by
  `popularity DESC` across all sources. Jikan's counts are live 2026 figures
  (avg ~1.1M over its 25 rows); the CSV is a 2018 snapshot (avg ~23k over
  14,478). FMA:B is 3,709,700 vs 1,199,091 for the same work. Result: every
  Jikan row outranks every CSV row in the default listing. The honest fix is
  per-source normalisation at query time — Phase 2, alongside entity
  resolution. See README, "Why these choices".

- **13 duplicate works across sources**, by design. `anime:jikan:5114` and
  `anime:mal:5114` are the same anime; both carry `external_ids.mal_id` as the
  join key. Phase 2's job.

- **The CSV dump has no synopsis.** `description` falls back to `background`
  (production trivia), present on only 7.3% of rows, so the FTS index leans
  almost entirely on titles for the CSV half of the catalog. Jikan rows do have
  real synopses.

- **The dump is a static 2018 snapshot.** Scores and member counts are frozen;
  anything newer is absent. Re-running `anime_jikan` once it recovers is what
  refreshes the live half.

## Upstream: Jikan is serving only its warm cache

Not a pagination bug. Anything needing a live MyAnimeList fetch returns:

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
and reads as "id lookups work". They do not — those ids are simply cached. A
sequential id walk 504s on nearly every id and is *not* a viable workaround;
neither is graph traversal, since `recommendations` and `relations` are down
too. This is what motivated the CSV ingester.

Re-run `bootstrap.py` once MAL is healthy; upserts are idempotent and the
remaining pages fill in as updates.

## Two bugs found while writing the tests

- **`ingest/base.py` discarded a partial run.** When `fetch()` raised
  mid-stream the exception skipped the post-loop `if batch:` flush, so with
  `BATCH_SIZE=200` a run that fetched 25 items then hit a 504 persisted
  **zero**. Now flushed in the exception path, guarded separately since the
  original failure may itself have come from `upsert_items`. Verified against
  the live outage: before, `fetched 25 / inserted 0`; after, `25 / 25`. The run
  is still recorded `status="error"`.

- **`ingest/anime_jikan.py` title fallback short-circuited on whitespace.**
  `raw.get("title") or raw.get("title_english")` — a whitespace-only title is
  truthy and hid a good `title_english` behind it.

## Notes for any further machine

- `DATA_DIR` defaults to `%LOCALAPPDATA%\Loom`; the catalog does not come with
  the clone. Run `bootstrap.py` once.
- The kagglehub cache (`~/.cache/kagglehub`) holds ~9.4 GB after extraction,
  mostly the user-ratings files this ingester ignores. Set `MAL_CSV_PATH` to a
  local CSV to skip the download.
- If TLS fails with `CERTIFICATE_VERIFY_FAILED`, that is the corporate proxy;
  `net.py` handles it via `truststore`. Confirmed a no-op on a home network.
- Fixtures and the dump are UTF-8 and hold Japanese titles. Read them with an
  explicit `encoding="utf-8"` — Python on Windows defaults `open()` to cp1252
  and dies on the first one. Set `PYTHONIOENCODING=utf-8` before printing them.
