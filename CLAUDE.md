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

Cross-domain hybrid recommender (anime / manga / books / music / movies / TV), local
desktop app, no admin rights. See `README.md` for architecture and `ROADMAP.md`
for what comes next.

## Commands

```powershell
uv sync                                       # install from uv.lock
uv run pytest                                 # 960 tests, none touch the network
uv run python scripts/bootstrap.py            # init schema + run ingesters
uv run python scripts/bootstrap.py --only anime_mal_csv --limit 100
uv run python scripts/bootstrap.py --only anime_mal_ratings   # 19.2M ratings, ~75 s
uv run python scripts/bootstrap.py --only book_goodbooks_ratings  # 6M ratings, GitHub, ~70 s
uv run python scripts/bootstrap.py --only manga_anilist        # 2nd domain, ~2 min
uv run python scripts/bootstrap.py --only movie_tmdb --only tv_tmdb  # needs TMDB creds
uv run python scripts/bootstrap.py --only book_goodbooks       # 10k books, ~4 s
uv run python scripts/bootstrap.py --only music_musicbrainz    # composers, 1 req/s
uv run python scripts/bootstrap.py --only music_commons --only music_deezer  # artist pictures
uv run python scripts/bootstrap.py --no-ingest                 # rebuild derived tables only
uv run python scripts/bootstrap.py --no-ingest --prune-dry-run # what filters reject
uv run python scripts/remap.py --only book_openlibrary --field image_url  # mapper fix, offline
uv run python scripts/embed.py                # build the embedding matrix, ~2.5 h
uv run python scripts/embed.py --evaluate     # score it against adaptation links
uv run python scripts/cf.py                   # item-item CF from 19.2M ratings, ~2 min
uv run python scripts/cf.py --evaluate        # held-out recall vs a popularity baseline
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

**Non-item data gets a sibling contract, not an `Ingester` subclass.** A ratings
loader is not fetch → `UnifiedItem` → `upsert_items` in any of its three parts.
`ratings_mal_csv.py` shares `IngestResult` and the `ingest_runs` row, duplicates
the ~20-line batching loop, and satisfies the `IngestRunner` Protocol in
`ingest/base.py` — which is what keeps `bootstrap.py` and `registry.py`
indifferent to which kind they hold. Same three-step seam either way.

## Rules that are easy to violate

- **Duplicates are collapsed on read, never merged on write.** Several sources
  describe one work and each keeps its own row; `recsys/link.py` writes a
  `links` row naming which represents the work, and `query_items` /
  `count_items` show only that one. `get_item` still reaches any row by uid,
  and `link.work_members()` returns a work's full set — Phase 3 wants the
  union (AniList's text plus the dump's 2018 counts), not just the winner.

- **The linker ranks rows by content, never by source name.** A source-preference
  list in `link.py` or `db.py` would mean editing them to add a source, which the
  invariant above forbids. `_richness()` prefers whichever row actually has a
  synopsis and more tags.

- **A model claim needs a measurement, and the measurement needs a hard half.**
  Cross-domain similarity cannot be judged by eye. `models/evaluate.py` scores
  against adaptation edges -- ground truth the sources assert -- and reports the
  pairs whose titles *differ* separately, because 87% share a title and the
  embedded document leads with the title. The headline number is largely a
  string match congratulating itself; the hard column is the evidence.

- **The embedder is a parameter, never an import.** `build_embeddings` takes a
  callable, so the whole pipeline is tested with a deterministic fake and no
  test ever downloads a model.

- **No single source is complete, and reads must show the union.** Open Library
  has a book's synopsis and none of its ratings; goodbooks has the ratings and
  no prose. Two mechanisms carry that: `popularity.py` gives every row of a work
  the best figure any source reported (ordering happens in SQL, so a gap there
  cannot be patched afterwards), and `db.fill_from_siblings` completes the
  representative's null scalars from its siblings. Neither ever overwrites a
  populated field, and lists are deliberately not merged -- Phase 3 reads the
  real union through `link.work_members()`.

- **Filter at the source, not after the fact.** Every row an ingester rejects
  before requesting it is a request not made and a row not stored. Thresholds
  belong in `config.py` and are set from *measured* distributions, not guessed:
  `tmdb_min_votes=50` cut 22% of films, `tmdb_tv_min_votes=25` cut 53% of shows
  because television's vote counts run an order of magnitude below film's.

- **Never filter a catalog by age, language or country.** A vote threshold says
  "nobody engaged with this" directly; a year cutoff only correlates with it and
  throws out *Metropolis*, and a language filter mostly encodes a bias toward
  English. `tests/test_prune.py` pins the old-but-well-rated case.

- **`prune.py` is the only code that deletes *catalog* rows.** Everything else
  upserts; `link.py` hides duplicates without touching them and `popularity.py`
  writes a second opinion beside the source datum. Pruning never runs as part of
  a normal bootstrap, defaults to a dry run, and reports before it acts. After
  it runs, every derived table -- `links`, `popularity_norm`, `works`,
  `relations` -- must be rebuilt; they point at deleted rows.

  `db.delete_rating` is the one deletion outside it and is not the same act. The
  rule above exists because derived tables point at catalog rows; nothing on
  disk is derived from your ratings, and un-rating something has to actually
  remove it, since there is no "hidden but present" state for a rating that
  would mean anything.

- **A paginated sweep is incomplete until it is partitioned.** AniList refuses
  to page past 5,000 entries into any listing and TMDB stops at 500 pages, in
  both cases silently as far as coverage goes. `partition_by_year` walks one
  start-year at a time so no single listing is deep enough to hit the ceiling.

- **Credentials live in `.env` and nowhere else.** TMDB is the only source
  needing one. Prefer `TMDB_READ_TOKEN` (v4): it goes in an `Authorization`
  header, so it never reaches a URL. A v3 `api_key` is a query parameter, and
  `DiskCache` writes the string it is keyed on into every envelope -- so
  `TmdbIngester._cache_key` strips it. Never put a real credential in
  `.env.example`, a docstring, a log line, or a test.

- **Reads go through `works`, which is a cache and must degrade.** Listings
  order by the calibrated popularity, and that value lives in `popularity_norm`
  -- a *joined* table, so no index on `items` could ever satisfy the ORDER BY
  and every page scanned the catalog and sorted it in a temp b-tree (267 ms).
  `works.py` stores one row per work with the sort key on it; reads are 5.6 ms.
  It is rebuilt strictly after `link.py` and `popularity.py`, in that order,
  because it stores what both decided. When it is empty `db.py` falls back to
  the live joins -- a catalog that has never been linked must still list, and
  an empty shelf on a populated database is the worst way for a cache to fail.

- **`works` also holds what a listing filters and sorts on, and after a schema
  change it must be rebuilt.** Year and score ride on the row *filled from the
  work's siblings* (8,399 of 10,000 book works have no year on their
  representative), tags sit in `work_tags`, and the picker's vocabulary in
  `tag_counts` -- a `json_each` walk for one tag was 265 ms. Migration 7 empties
  `works` on purpose so reads fall back to the live joins rather than filter on
  NULLs; run `bootstrap.py --no-ingest` after pulling it. Several tags are ANDed:
  a second tag narrows.

- **"Highest rated" sorts on `rating_key`, never on `mean_rating`.** By raw mean
  the catalog's best work was a road-safety short with one vote of 10.
  `works._weigh_ratings` shrinks each score toward its domain's mean by
  `m` votes, where `m` is that domain's *median* vote count, measured at each
  rebuild -- rejected: any constant, because half the rated TV shows have under
  77 votes and half the rated books over 21,157. Filters still use
  `mean_rating`, so "8 and up" means the number on the card.

- **Same-franchise suppression is two mechanisms, each with a measured guard.**
  `relations.franchise_representatives` walks sequel *and adaptation* edges
  transitively but never *through* a work with two parents: unlimited, the walk
  took *Dragon Ball Z* to *ONE PIECE* via a festival short. `CHARACTER` and
  `OTHER` edges are excluded (the former fuses 4,599 works into one group).
  `franchise.py` then matches titles, because TMDB and book rows share no id
  with the anime they belong to -- using only the titles of the works actually
  rated (franchise-wide names hid the *MONSTER* manga for *Golden Kamuy*
  raters) and never a one-word stem (`Mission:`, `Fate/`). A title match is
  allowed here and forbidden in `link.py` because the stakes differ: a false
  link hides a work everywhere, a false match costs one slot and
  `same_franchise=true` shows it. The numbers came from one-off probes over the
  1,500 most popular anime, not from a kept script; re-measure before loosening
  either guard.

- **A URL in `image_url` is not a cover until something answers at it.** Three
  gaps hid behind non-null fields: every 2018 MAL dump URL named a host that no
  longer resolves, a third of goodbooks' pointed at one "no photo" file, and
  Open Library rows ignored the cover ids in their own payload. Fix covers in
  the mapper and apply them with `scripts/remap.py`, which re-runs a mapper over
  stored `raw_json` and writes *only the named columns* -- goodbooks takes its
  tags from a second file, so a whole-row remap would erase them. Open Library
  covers go by cover id, not ISBN (ISBN lookups are limited to 100 per 5 min).

- **Music portraits come from Wikimedia Commons first and Deezer second.**
  `music_commons` joins on the MusicBrainz id through Wikidata and covers 1,811
  of 6,556 artists with freely licensed pictures. `music_deezer` covers the
  rest it can (4,730 of 6,556 together) from ids already in the catalog, with no
  request -- label promotional photos, hot-linked, left out of the first pass
  for that reason and added at the owner's request on 2026-11-10. Commons wins
  where both exist because `fill_from_siblings` reads donors in uid order and
  `commons` sorts before `deezer`; nothing names a source. A picture row must
  never win the primary election (the embedding matrix holds representatives
  only); the last tiebreak is the uid, where both sort below `musicbrainz`, and
  `tests/test_covers.py` pins all of it. Everything without a cover gets a
  generated tile in the UI, coloured from its title.

- **Search relevance is name first, bm25 last.** bm25 alone opened "shingeki no
  kyojin" on a recap special: it measures how much of a document the words are,
  and a row with no synopsis is nearly all title. `query_items` now orders by
  tier -- the title or an alt title *is* the query; every word is in a name;
  synopsis only -- with calibrated popularity inside the first two and bm25
  only inside the third. A score filter compares `ROUND(mean_rating, 1)`,
  because the card prints one decimal and "8 and up" means what is printed.
  Cost: a one-word stopword query ("the", 67k matches) takes ~0.6 s.

- **`domain` is repeatable on every listing route.** `?domain=anime&domain=movie`
  is "these and not the rest"; none, `all`, or every domain named is no filter.
  One domain stays an equality in SQL so `idx_works_domain_sort` still serves
  the ordering. Responses name a `domain` only when exactly one was asked for.

- **`relations` is not `links`.** `links` means "these rows are the same work".
  An adaptation or a sequel is the opposite claim -- two different works, one
  story -- so the AniList relation edges get their own table. Never collapse
  them onto `links`, and never assume an edge's far side is in the catalog.

- **Your ratings are resolved onto the work, not the row you clicked.**
  `set_rating` runs the uid through `link.representative_uid` before storing it,
  because `get_item` reaches every row of a work and the `(user_uid, item_uid)`
  primary key cannot tell that `anime:anilist:5114` and `anime:mal:5114` are one
  show. `rated_uids` resolves again on the way out -- not redundantly: relinking
  moves representatives, and the embedding matrix holds only winners, so a
  rating stranded on a loser would silently stop counting toward your profile.

- **A taste profile counts what you rated at or above your own mean.** Not a
  fixed bar on the 1-10 scale -- nobody shares one, and MAL's corpus mean of
  7.59 says so. That single rule needs no special case: one rating is its own
  mean and still builds a profile, and rating everything 8 counts all of it.
  `models/profile.py` deliberately has no Rocchio negative term, because its
  weight cannot be set from any measurement this project has.

- **Ratings and listings use different uids; translate, never assume.** Every
  rating names `anime:mal:{id}`, but only 451 of the 6,598 rated rows *represent*
  their work -- the other 6,147 lost the primary election to an AniList row with
  a synopsis, so they are exactly the uids `query_items` never returns and the
  embedding matrix does not hold. `models/collaborative.py` stores its artifact
  in rating space and translates at read time through `links`; that translation
  lives in `CollaborativeIndex.similar_to` and nowhere else. Getting it wrong
  returns an empty list and raises nothing.

- **Migrations are append-only.** Add a new entry to `MIGRATIONS` in `db.py`;
  never edit a shipped one. `PRAGMA user_version` tracks what has been applied.

- **Everything is UTF-8; Windows is not.** Python's `open()` defaults to cp1252
  here and dies on the first Japanese title. Always pass `encoding="utf-8"`
  explicitly, and set `PYTHONIOENCODING=utf-8` before printing non-ASCII.

- **Tests never touch the network or `%LOCALAPPDATA%`.** `tests/conftest.py`
  redirects `DATA_DIR` to a `tmp_path` and clears the `get_settings` cache.
  Keep it that way — use fixtures under `tests/fixtures/`.

- **Never order by raw `popularity`.** It is a raw audience count whose scale
  is meaningless between sources (MAL members 2018, MAL members 2026, AniList
  list-adds). `recsys/popularity.py` calibrates it onto one scale by fitting a
  monotone curve on works that two sources both carry, and stores the result in
  `popularity_norm`; reads order by that. `mean_rating` needs none of this --
  each ingester rescales it to 0–10 at ingest.

- **Do not "fix" cross-source ranking with percentile-within-source.** It is the
  obvious move and it is wrong: it assumes every source samples the same
  population, so a top-25-only source has its weakest row ranked last overall.
  `tests/test_popularity.py` pins that case.

- **User input never reaches FTS5 as syntax.** Route it through
  `db.build_fts_query()`, which quotes every token, so `AND` / `*` / `NEAR`
  typed in the search box are treated as ordinary words.

- **Rating uids go through the same helpers as item uids.** `make_item_uid` and
  `make_user_uid` in `schemas.py` are the only places those formats are
  written down. A rating points at a catalog row by uid with no foreign key, so
  a drift between the two spellings would not error — it would just make every
  Phase 3 join return nothing.

- **`item_uid` is derived, never assigned.** It is always
  `"{domain}:{source}:{source_id}"`, computed by a pydantic validator. Two
  sources describing the same work stay separate rows; reconciling them is
  Phase 2's job and needs both rows to survive.

- **No frontend build step.** Plain HTML/CSS/JS, no npm, no CDN. All DOM
  insertion goes through `textContent` — catalog text is third-party and never
  touches `innerHTML`.

- **The catalog lives outside the repo**, in `%LOCALAPPDATA%\Loom`, because
  OneDrive locks SQLite WAL sidecars mid-write. Never write data into the repo.
