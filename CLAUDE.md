# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

# Project-specific: Loom

Cross-domain hybrid recommender (anime / books / music / movies / TV), local
desktop app, no admin rights. See `README.md` for architecture and `ROADMAP.md`
for what comes next.

## Commands

```powershell
uv sync                                       # install from uv.lock
uv run pytest                                 # 229 tests, none touch the network
uv run python scripts/bootstrap.py            # init schema + run ingesters
uv run python scripts/bootstrap.py --only anime_mal_csv --limit 100
uv run python app.py                          # native window (--browser, --no-ui)
```

## The one architectural invariant

**Adding a catalog source must not require editing `db.py`, `routes.py`,
`bootstrap.py`, or the UI.** A new source is:

1. one new file in `src/recsys/ingest/`,
2. one line in `ingest/registry.py`,
3. its settings in `config.py`.

If a source forces a change anywhere else, the abstraction in `ingest/base.py`
is wrong and should be fixed rather than worked around. `anime_mal_csv` was
added this way and is the worked example.

## Rules that are easy to violate

- **Migrations are append-only.** Add a new entry to `MIGRATIONS` in `db.py`;
  never edit a shipped one. `PRAGMA user_version` tracks what has been applied.

- **Everything is UTF-8; Windows is not.** Python's `open()` defaults to cp1252
  here and dies on the first Japanese title. Always pass `encoding="utf-8"`
  explicitly, and set `PYTHONIOENCODING=utf-8` before printing non-ASCII.

- **Tests never touch the network or `%LOCALAPPDATA%`.** `tests/conftest.py`
  redirects `DATA_DIR` to a `tmp_path` and clears the `get_settings` cache.
  Keep it that way — use fixtures under `tests/fixtures/`.

- **`popularity` is a within-source signal, not a cross-source one.** It is a
  raw audience count whose scale is meaningless between sources. `mean_rating`
  *is* comparable, because each ingester rescales it to 0–10 at ingest. There is
  a known open issue here: `query_items` currently orders by `popularity DESC`
  globally across sources. See `ROADMAP.md`.

- **User input never reaches FTS5 as syntax.** Route it through
  `db.build_fts_query()`, which quotes every token, so `AND` / `*` / `NEAR`
  typed in the search box are treated as ordinary words.

- **`item_uid` is derived, never assigned.** It is always
  `"{domain}:{source}:{source_id}"`, computed by a pydantic validator. Two
  sources describing the same work stay separate rows; reconciling them is
  Phase 2's job and needs both rows to survive.

- **No frontend build step.** Plain HTML/CSS/JS, no npm, no CDN. All DOM
  insertion goes through `textContent` — catalog text is third-party and never
  touches `innerHTML`.

- **The catalog lives outside the repo**, in `%LOCALAPPDATA%\Loom`, because
  OneDrive locks SQLite WAL sidecars mid-write. Never write data into the repo.
