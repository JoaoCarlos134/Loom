# Phase 0 — remaining work

Handoff note. Everything in `README.md` is accurate; this is what is *not* done yet.

## Not yet written

- **`tests/`** — the only substantial gap. `tests/fixtures/jikan_top_anime_p1.json`
  exists (3 real Jikan records: Fullmetal Alchemist Brotherhood, Steins;Gate, Cowboy
  Bebop) and `tests/__init__.py` exists, but no test modules yet. Planned:
  - `test_schemas.py` — UnifiedItem validation, `item_uid` derivation, rejection of a
    mismatched explicit uid, 0–10 rating bound, tag normalisation
  - `test_jikan_mapping.py` — fixture → UnifiedItem, MAL footer stripping, year
    fallback via `aired.prop.from.year`, missing-image and missing-score handling
  - `test_db_upsert.py` — insert/update counts, FTS row stays in sync after update,
    within-batch dedup
  - `test_api.py` — four routes against a seeded temp DB, 404 path, 422 on a bad domain
  - `conftest.py` — a `DATA_DIR` monkeypatch to a tmp_path so tests never touch
    `%LOCALAPPDATA%`

  The logic these would cover was verified manually end to end before this commit
  (schema v1, 3 mapped, upsert `(3,0)` then `(0,3)`, FTS hit on "alchemist", all four
  routes 200, static UI 200). It is untested in CI terms, not unverified.

## Blocked / verified-partially

- **Full 2000-item ingest has not been run.** `api.jikan.moe/v4/top/anime` was
  returning `504 BadResponseException` ("Jikan failed to connect to MyAnimeList")
  for every attempt during the build session — an upstream Jikan outage, not a
  local or proxy problem: `/v4/anime/{id}` returned 200 over the same connection,
  which is how the fixture was captured.

  The retry path treats 504 as retryable and will exhaust `JIKAN_MAX_RETRIES` and
  fail the run cleanly with a recorded `ingest_runs` error row. **First thing to do
  on the other machine:** re-run `uv run python scripts/bootstrap.py --limit 100`
  and confirm `/top/anime` is back before the full run.

- **pywebview window has not been opened.** `app.py` imports fine and the server
  half is verified, but no native window was launched in this session. If the Edge
  WebView2 runtime is missing, `open_window()` logs and falls back to the browser
  rather than crashing — use `--browser` to skip the webview entirely.

## Notes for the second machine

- `DATA_DIR` defaults to `%LOCALAPPDATA%\Loom`, so the catalog does not come with the
  clone. Run `bootstrap.py` once.
- If TLS fails with `CERTIFICATE_VERIFY_FAILED`, that is the corporate proxy;
  `src/recsys/net.py` already handles it via `truststore`. On a home network it is a
  no-op.
- `uv sync` pins everything from `uv.lock`. All wheels — nothing compiles.
