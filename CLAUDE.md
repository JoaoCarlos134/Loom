# CLAUDE.md -- Loom

Cross-domain hybrid recommender (anime / manga / books / music / movies / TV), local
desktop app, no admin rights. `README.md` holds the architecture and its "Why these
choices"; `ROADMAP.md` holds where the project is and what comes next.

**Status (2026-11-05):** 1.0 release candidate (`1.0.0rc1`) on
`claude/sweet-ride-u2k95y`. The seven bugs measured on the full build are fixed
with regression tests. The starter pack was only dry-run on goodbooks-10k (no
vectors, no Open Library text); `scripts/build.py` ran end to end only on the
sources the build machine reached plus a fixture; the PyInstaller package was
built and smoke-tested on Linux, never on Windows. Nothing is published. The
full build's figures in the docs were counted by the owner before
`scripts/measure.py` existed; new ones come from it. Waiting on the owner: a
measurement of the full build with this code, and approval to publish.

## Rules

- **Identity:** commit only as the personal GitHub noreply identity,
  `JoaoCarlos134 <93523542+JoaoCarlos134@users.noreply.github.com>`, set in
  `.git/config`. Never an employer address. Never rewrite history, never
  force-push `main`, never push the pre-rewrite branches (the old history is
  kept in the private `Loom-old`).
- **Data never in the repo.** The catalog, caches, models and packs live in
  `DATA_DIR` (`%LOCALAPPDATA%\Loom`); test fixtures of a few records are the
  only data committed.
- **English** in code, comments, docs and commit messages.
- **One bug or feature per commit, test first**, and each test must fail
  without the change it covers. Commit messages end with the Co-Authored-By line.
- **Ask before anything that publishes** (releases, packages, tags) or needs
  the owner's accounts.
- **Measure, don't edit numbers.** A new number in README/ROADMAP is one
  `scripts/measure.py` (or a named script or commit) printed, and older ones say
  where they came from; the owner's full build is opened read-only for that, and
  anything that writes runs against a copy or a fresh `DATA_DIR`.

## Commands

```powershell
uv sync                                       # install from uv.lock (Python 3.12-3.14)
uv run pytest                                 # every test; none touch the network
uv run python scripts/build.py                # the full build, resumable (--status)
uv run python scripts/bootstrap.py --starter  # install the starter pack instead
uv run python scripts/bootstrap.py            # init schema + run INGESTERS
uv run python scripts/bootstrap.py --only book_goodbooks       # 10k books, ~4 s
uv run python scripts/bootstrap.py --only idmap_wikidata       # anime <-> TMDB ids
uv run python scripts/bootstrap.py --no-ingest                 # relink + rebuild derived
uv run python scripts/bootstrap.py --no-ingest --prune-dry-run # what filters reject
uv run python scripts/embed.py --check        # is the matrix current? embeds nothing
uv run python scripts/embed.py --adopt        # vouch for a pre-fingerprint matrix, once
uv run python scripts/embed.py                # embed only what changed (--full: all)
uv run python scripts/embed.py --evaluate     # score it against adaptation links
uv run python scripts/cf.py                   # item-item CF, ~2 min; --evaluate
uv run python scripts/measure.py              # the docs' numbers, read-only (--out FILE)
uv run python scripts/starter.py --version v1 # build a starter pack (does not publish)
uv run python app.py                          # native window (--browser, --no-ui)
uv sync --group package; uv run pyinstaller packaging/loom.spec --noconfirm
```

### When Windows Application Control blocks uv's venv

On the home machine, Application Control refuses the Python inside a
uv-created venv. Use a stdlib venv and install into it with uv:

```powershell
py -3.12 -m venv C:\loomvenv
uv export --frozen --all-groups --no-hashes -o req.txt   # not "> req.txt": UTF-16
uv pip install -r req.txt --python C:\loomvenv\Scripts\python.exe
C:\loomvenv\Scripts\python.exe -m pytest
C:\loomvenv\Scripts\python.exe -m ruff check .
C:\loomvenv\Scripts\python.exe -m mypy
```

If an import then fails with "Uma política de Controle de Aplicativo bloqueou",
that binary wheel is blocked too: pin an older version of that package in the
stdlib venv only.

## Decisions (newest first)

- **2026-11-05 Starter pack = shareable sources only** (goodbooks-10k, Open
  Library); MusicBrainz left out with TMDB, since that domain is TMDB's
  composers. See README "Get the data" and ROADMAP Priority 1.
- **2026-11-05 Search fuses title match and per-domain popularity by
  reciprocal rank (k=60)**, exact titles first. README "Why these choices".
- **2026-11-05 Browse opens covers-first, domains interleaved by their own
  rank** -- no cross-domain popularity scale exists. README "Why these choices".
- **2026-11-05 One entry per story:** groups are components of adaptation,
  franchise and series edges, capped at `MAX_GROUP_SIZE` (provisional); book
  series fold behind one card (`FOLD_BOOK_SERIES`). ROADMAP "Done: 1.0".
- **2026-11-05 Anime <-> TMDB twins linked through Wikidata's id map**, a
  sibling contract; mapped rows never represent a work. README "Why these choices".
- **2026-11-05 TMDB retention runs without a dry run**: `prune.expire`, called
  by every full build after the TMDB steps, deletes TMDB rows and cached
  responses past `TMDB_MAX_AGE_DAYS` (180), because TMDB's terms require it.
  README "TMDB", ROADMAP Priority 1.
- **2026-11-05 Embedding vectors carry document fingerprints**; rebuilds are
  incremental. ROADMAP "Known issues".
- **2026-11-05 Python 3.12-3.14** (`<3.15`: pythonnet declares `<3.15` and
  onnxruntime has no 3.15 wheel); CI tests all three on Windows. README "Clone
  and sync", ROADMAP "Done: 1.0".
- **2026-11-04 MIT licence**, and the repo recreated with clean history.
  ROADMAP Priority 1.
- **2026-09 Calibrate popularity on overlap, not percentile-within-source.**
  README "Why these choices".
- **2026-08 Resolve duplicates with `links`, never merge.** README "Why these choices".

## Where to continue

1. Measure the full build with this code, on a copy (`DATA_DIR` pointed at it),
   in this order -- ROADMAP "Where we are" gives the reasons:
   `measure.py --out before.json`; `embed.py --adopt` (before anything
   re-ingests or relinks: a work's document folds in its siblings' text);
   `bootstrap.py --only book_goodbooks --only book_openlibrary --only
   music_musicbrainz --only idmap_wikidata` with `HTTP_CACHE_TTL_DAYS=0` (it
   relinks after); `embed.py`; `measure.py --out after.json`. Put the
   before/after into README and ROADMAP.
2. Set `MAX_GROUP_SIZE` from the largest groups that measurement prints.
3. From the same copy, `scripts/starter.py --version v1` (only vectors whose
   fingerprint matches the pack's own documents ship). With the owner's
   approval, publish it and pin its checksum as the `starter_sha256` default in
   `config.py` -- a stranger's checkout and the packaged app have no `.env`.
4. Once `package.yml` is on `main`, dispatch it by hand (or push a `v*` tag,
   with the owner's approval) and check the Windows build; it has never run.
5. ROADMAP "Next step": blend the two models; import a MAL/AniList library.

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
indifferent to which kind they hold. Same three-step seam either way;
`idmap_wikidata.py` is the second kind (identity claims into `id_map`).

## Rules that are easy to violate

- **Duplicates are collapsed on read, never merged on write.** Several sources
  describe one work and each keeps its own row; `recsys/link.py` writes a
  `links` row naming which represents the work, and `query_items` /
  `count_items` show only that one. `get_item` still reaches any row by uid,
  and `link.work_members()` returns a work's full set — Phase 3 wants the
  union (AniList's text plus the dump's 2018 counts), not just the winner.
  A row with no id of its own may join a work through an `id_map` claim
  (Wikidata's MAL <-> TMDB pairs): exact ids only, never a work from claims
  alone, a claim naming several works refused, and a mapped row never
  represents the work.

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

- **`prune.py` is the only code that deletes *catalog* rows.** (The starter pack
  builder drops ratings from the *pack's own copy*, and `--starter` replaces a
  database file only when it holds none of your ratings; neither touches rows of
  a catalog in use.) Its one rule that runs without a dry run is `prune.expire`,
  which the full build calls every time to delete TMDB data kept past TMDB's
  six-month limit -- the terms require it. Everything else upserts; `link.py`
  hides duplicates without touching them and `popularity.py` writes a second
  opinion beside the source datum. Pruning never runs as part of a normal
  bootstrap, defaults to a dry run, and reports before it acts. After it runs,
  every derived table -- `links`, `popularity_norm`, `works`, `relations` --
  must be rebuilt; they point at deleted rows.

  `db.delete_rating` is the one deletion outside it and is not the same act. The
  rule above exists because derived tables point at catalog rows; nothing on
  disk is derived from your ratings, and un-rating something has to actually
  remove it, since there is no "hidden but present" state for a rating that
  would mean anything.

- **A paginated sweep is incomplete until it is partitioned.** AniList refuses
  to page past 5,000 entries into any listing and TMDB stops at 500 pages, in
  both cases silently as far as coverage goes. `partition_by_year` walks one
  start-year at a time so no single listing is deep enough to hit the ceiling.

- **Credentials live in `.env` and nowhere else.** TMDB needs one; Kaggle
  sometimes does (`KAGGLE_API_TOKEN`, handed to kagglehub through the variable
  it reads), and `scripts/build.py` asks for either only when a step needs it.
  Prefer `TMDB_READ_TOKEN` (v4): it goes in an `Authorization` header, so it
  never reaches a URL. A v3 `api_key` is a query parameter, and
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

- **`relations` is not `links`.** `links` means "these rows are the same work".
  An adaptation, a sequel or a series volume is the opposite claim -- two
  different works, one story -- so those edges get their own table (AniList's
  from `raw_json`, `SERIES` read off Goodreads titles). Never collapse them
  onto `links`, and never assume an edge's far side is in the catalog. Results
  use them through `works.group_uid` -- connected components, capped at
  `MAX_GROUP_SIZE` -- to suppress the query's own story and keep one entry
  per story; listings fold a book series behind one card.

- **The embedding matrix is a snapshot; reads must not trust its uids.** It
  holds the representatives of the day it was built. `/api/similar` and
  `/api/recommend` exclude rows that have since lost their primary election
  *before* the top-k and translate every neighbour through
  `link.representative_uid`. Each vector carries a fingerprint of its
  document and model, so `embed.py` re-embeds only what changed; a matrix from
  before fingerprints is kept and reported as unverified, never as fresh.

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
  MAL rating names `anime:mal:{id}` (goodbooks' name `book:goodbooks:{id}`), but
  only 451 of the 6,598 rated anime rows *represent* their work -- the other
  6,147 lost the primary election to an AniList row with a synopsis, so they are
  exactly the uids `query_items` never returns and the embedding matrix does not
  hold. `models/collaborative.py` stores its artifact in rating space and
  translates at read time through `links`; that translation lives in
  `CollaborativeIndex.similar_to` and nowhere else. Getting it wrong returns an
  empty list and raises nothing.

- **Migrations are append-only.** Add a new entry to `MIGRATIONS` in `db.py`;
  never edit a shipped one. `PRAGMA user_version` tracks what has been applied.

- **Licences decide what ships.** `starter.SHIPPABLE` is the one list of
  (domain, source) pairs a starter pack may contain, each with the terms that
  allow it. TMDB, AniList, the MAL Kaggle dump and Jikan never go in it.
  Nothing is published (a release, a package, a pushed tag) without the
  owner's explicit go-ahead.

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
  each ingester rescales it to 0–10 at ingest. **Calibration is per domain.**
  Across domains there is no shared scale (Deezer fans against MAL members), so
  anything that compares domains -- the all-domains Browse order, search's
  popularity rank, sharing a figure within a cross-domain work -- ranks within
  each domain instead.

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
