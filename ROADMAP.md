# Loom — roadmap

Where the project is, what comes next, and which datasets feed each step.
Architecture and rationale live in `README.md`; Phase 0's closing state is in
`PHASE0_TODO.md`.

---

## Where we are

**Phase 0 is complete**, and Phase 3's data is loaded. Schema, ingest, API, UI,
desktop window and 266 tests, with the `anime` domain populated from two
sources and a ratings matrix behind it.

```
catalog        83,981 works      six domains, from 96,112 stored rows
  anime:anilist  12,837 rows     live GraphQL: synopses + ranked tags
  anime:mal      14,478 rows     Kaggle MAL dump, static 2018 snapshot
  anime:jikan        25 rows     live API, capped by the outage below
manga:anilist   3,300 rows     the works those anime were adapted from
synopsis coverage  80.0%         anime; 91% for manga
ratings    19,172,125 ratings    106,402 users x 6,598 anime, scores 1-10
tests             531            none touch the network or %LOCALAPPDATA%
```

No recommendation models yet, by design.

---

## Phases

| Phase | Work | Plugs into | Status |
|---|---|---|---|
| **0** | Skeleton, schema, one domain end to end | — | **done** |
| **1** | More domains: books, movies/TV, music | `book_goodbooks`, `movie_tmdb`, `tv_tmdb`, `music_musicbrainz` | **done** |
| **1** | Catalog quality filters | thresholds in `config.py`, `prune.py` | **done** |
| **1** | Complete sweeps (partitioned) | `partition_by_year` in `anilist.py` | **done** |
| **1** | Cross-source id linking | `external_ids` — already populated | ready |
| **1** | Synopses + rich tags + adaptation edges | `anime_anilist` ingester, joins on `idMal` | **done** |
| **1** | Manga domain | `manga_anilist.py` + one registry line | **done** |
| **2** | Entity resolution (same work, N sources) | `links` table, migration 3, `link.py` | **done** |
| **2** | Cross-source popularity calibration | `popularity_norm` table, migration 4, `popularity.py` | **done** |
| **2** | Adaptation links (anime ↔ novel, film ↔ book) | `links` table — strongest bridge, no model needed | high value |
| **2** | Feature matrices | pyarrow → parquet under `DATA_DIR` | — |
| **3** | User ratings and feedback | `ratings` table, migration 2, `ratings_mal_csv.py` | **done** |
| **3** | Content-based / collaborative / hybrid models | new `recsys/models/` package | **next** |
| **3** | Your own library — rate things | `ratings` table, `local:` user namespace — no migration | **the product goal** |
| **3** | Import an existing library (MAL/AniList export) | new file in `ingest/`, writes `local:` rows | solves cold start |
| **3** | Canonical tag vocabulary | authored file in repo + feature-build layer | bridge, step 1 |
| **3** | Cross-domain profile scoring | `recsys/models/` — attributes, not titles | **the bridge** |
| **4** | Implicit signals (views, dismissals) | new `events` table via a new migration | decide explicitly |
| **4** | Similarity graph view | `/api/similar/…` + canvas renderer, reads the model's k-NN | — |
| **4** | Region-aware availability | `REGION` setting — already threaded through | — |

---

## Done: all six domains, filtered

Phase 1's headline item -- "more domains: books, movies/TV, music" -- is
complete, and the catalog is four times the size it was while carrying less
junk.

```
domain    works   synopsis   avg tags   creators
manga    29,034       92%        6.9       100%
anime    18,764       72%        7.5        91%
book     10,000        0%       10.4       100%
tv        9,968       98%        8.9        99%
movie     9,659       99%       14.5       100%
music     6,556        0%        2.2       100%
         ------
         83,981 works from 96,112 rows        2.02 GB
```

### Filters, set from measurement rather than taste

Every filter is applied at the source, so a rejected row is a request never
made. Thresholds came from the distributions of a first unfiltered pass:

| Domain | Filter | What it removed |
|---|---|---|
| movie | `vote_count >= 50` | 2,128 (22%) -- p25 was 271 votes |
| tv | `vote_count >= 25` | 4,369 (53%) -- median show had **28 votes** |
| anime | `popularity > 200`, not adult, not `MUSIC` | 750 opening-theme clips filed as anime |
| manga | `popularity > 200`, not adult | -- |
| book | none | goodbooks-10k is pre-curated: every book has 10,443+ ratings |
| music | scoped to credited composers | -- |

**Television needed its own floor.** Its vote counts run an order of magnitude
below film's, so the film threshold would have halved the domain for the wrong
reason.

**Nothing is filtered by age, language or country, deliberately.** A vote
threshold states "nobody engaged with this" directly. A year cutoff only
correlates with that and discards *Metropolis*; a language filter mostly encodes
a bias toward English-language output. `tests/test_prune.py` pins the
old-but-well-rated case so the rule cannot quietly erode.

Filtering also made the catalog *bigger* where it counts: television rose from
8,183 to 9,968 because `/discover` now spends its 500-page budget on titles that
clear the bar instead of on the long tail.

### Partitioning, which is what "complete" actually required

AniList refuses to page past 5,000 entries into any listing and TMDB stops at
500 pages -- in both cases silently, as far as coverage is concerned. A sweep
that looks like it finished is simply truncated.

Sweeping one start-year at a time keeps every listing shallow enough to finish,
and the effect is not marginal:

```
manga   3,297  ->   7,627  ->  29,034
        (adaptations)  (flat sweep)  (partitioned)

anime dated 2019 or later:   120  ->  4,025
```

The anime domain had been frozen at 2018 for a structural reason, not a bug:
`anime_anilist` was driven by MAL ids *from the 2018 Kaggle dump*, so it could
only ever ask AniList about anime that dump already knew. No amount of re-running
would have fixed it.

### Pruning

Filters stop rows being fetched; they do not reach rows already stored. So
`prune.py` applies the same bars to what is on disk -- and it is **the only code
in Loom that deletes anything**. It never runs as part of a normal bootstrap,
defaults to a dry run, and reports before acting. It removed 7,247 rows on its
first real pass; the catalog now reports zero flagged against its own filters.

### What each source cost, and what it needed

| Source | Credential | Rate | Notes |
|---|---|---|---|
| AniList | none | 30/min | Page depth capped at 5,000 |
| TMDB | **free API key** | 20/s | One detail request per title, for keywords + credits + imdb_id |
| goodbooks-10k | none | -- | Three CSVs from GitHub, ~5 MB |
| MusicBrainz | none | 1/s | **Throttles clients whose User-Agent names no contact** |
| Deezer | none | -- | Supplies the audience count MusicBrainz lacks |

The MusicBrainz finding cost an hour: without a contact URL in the User-Agent,
3 of 4 requests returned 503. With one, 4 of 4 succeeded. It was the whole
failure, not a rate problem.

### Gaps this leaves

- **Books have no synopsis at all** (0%). goodbooks-10k carries none. Open
  Library returned a description for 5 of 5 probed ISBNs plus curated subjects,
  at two requests per book -- roughly 6 hours for 10,000, or less with modest
  concurrency. This is the single largest remaining hole in the text bridge.
- **Music has no text and thin tags** (2.2 average). MusicBrainz has no
  biography. This is accepted rather than solved: music tags describe sound
  while every other domain's describe story, so the domain earns its place
  through the composer link, not through content similarity.
- **Anime synopsis is 72%**, held down by the MAL rows that win the primary
  election where AniList carries no entry.

---

## Done: manga, the second domain

The first thing to actually test the architecture's one claim. Five domains were
declared and one was populated, so "adding a domain is one file plus one registry
line" had never been checked. It is now, and it held -- with one caveat.

```
3,300 manga fetched in 2.1 min       synopsis 91%, avg 7.5 tags, creators 99%
catalog 17,787 works                 anime 14,490 + manga 3,297
```

**What it fetches is the interesting part.** Not AniList's manga catalogue: the
ids come from the relation edges already sitting in the anime rows' `raw_json`,
following `ADAPTATION` and `SOURCE` only. Those are the manga and light novels
our anime were adapted from. The edges were kept during the anime ingest so that
mining them later would cost no further requests, and it did not.

That makes this a cross-domain pair with **hard links rather than inferred
similarity**. Everywhere else the bridge has to guess that a book resembles an
anime from shared tags and text; here the two are the same story and AniList
says so. It is the reference case for judging the content bridge: a model that
cannot connect an anime to the manga it was literally adapted from is not going
to connect it to an unrelated novel either.

### The invariant held, but only after fixing the abstraction

Adding manga was one new file and one registry line -- and no new settings, since
it reuses the AniList ones. But it was not that at first: the GraphQL client, the
retry loop and the cache were welded into `anime_anilist.py`. Rather than copy
250 lines, the shared machinery moved to `anilist.py` and both domains became
thin subclasses differing in three things: the domain, the `type:`, and where
the ids come from. That is what the invariant asks for when a source cannot
plug in cleanly -- fix the abstraction, do not work around it.

The one genuine schema change was widening the `Domain` literal in `schemas.py`,
which that file's docstring already named as the sanctioned exception.

### Three bugs this turned up

- **`json_each` exposes its own `id` column.** `GROUP BY id` in the adaptation
  query bound to the JSON array index rather than the manga id, silently
  collapsing distinct manga together. Caught by a test, not by inspection.
  Renamed to `manga_id`.
- **Staff arrive in no useful order.** 7% of manga listed an assistant or a
  translator as their first creator -- including one ahead of Hajime Isayama on
  *Shingeki no Kyojin*. Credits are now ranked, story and art first.
- **Translators are not creators.** AniList lists them per language, ~1,150
  across this catalogue. Left in, "same person" would have bridged two unrelated
  manga because they share a Spanish translator. Edition credits -- translation,
  lettering, editing, typesetting -- are now excluded; both numbers are 0% and 0
  after the fix.

And one trap fixed while it was still cheap: the response cache was keyed by URL
and id batch but **not by the query**, so adding `staff` to the shared query
would have returned cached payloads silently missing it for 30 days. The key now
carries a query fingerprint.

### Known gap

The anime rows predate `staff` being requested, so they carry studios but no
`Original Creator` -- which is the person-level bridge to manga in its purest
form, naming the author whose work a series adapts. The cache fingerprint means
the next `anime_anilist` run refetches and picks it up; that is ~10 minutes and
has not been done yet.

---

## Done: one popularity scale across sources

`items.popularity` is a raw audience count and every source counts something
different: MAL members frozen in 2018, MAL members live in 2026, AniList users
with it on a list. Sorting on it compared yardsticks -- Jikan's *least* popular
row (160,982) outranked AniList's most popular (1,048,042), so Jikan held the
top seven slots of every default listing on vintage alone.

`popularity_norm` (migration 4, built by `popularity.py` right after linking)
stores a second opinion beside the raw number. Raw `popularity` is never
touched: it is the honest source datum.

```
domain  source   method     rows    anchors  knots
anime   mal      reference  14,474        0      0   broadest coverage, so it defines the scale
anime   anilist  curve      12,837   12,821     20
anime   jikan    offset         25       13      0   x0.341 -- the ~3x vintage gap, recovered
```

*Fullmetal Alchemist: Brotherhood*, the case this bug was always described
with:

| | raw | calibrated |
|---|---|---|
| `anilist` | 724,564 | 853,610 |
| `jikan` | 3,709,700 | 1,265,703 |
| `mal` | 1,199,091 | 1,199,091 |

A 5.1x spread for one work becomes 1.48x.

### Why not percentile-within-source

The fix this roadmap prescribed for a year assumes every source samples the same
population. Jikan's rows are the *top* 25 anime, so ranking them among
themselves puts its weakest row -- still a top-25 title -- at percentile 0.0,
"least popular in the catalog". It inverts the error instead of fixing it, and
any future top-N endpoint walks into the same trap. `test_a_top_slice_source_is
_not_sent_to_the_bottom` is that scenario, pinned.

### Calibrating on overlap instead

Where two sources carry the same work -- which `links` already knows exactly --
the pair of numbers is an anchor. Fit a mapping from the source's scale onto the
reference's using those anchors. This never assumes a source's rows are
representative, only that the works it *shares* are typical of it. Jikan's 13
shared works were enough to recover a x0.341 factor, which is the 3x vintage gap
the README described from the start.

The reference is whichever source covers the most of the domain, decided from
the data. Naming a preferred source would mean editing `popularity.py` to add
one, which the project invariant forbids. A domain's first source is its own
reference, so books arriving with nothing to calibrate against simply work.

### The mapping is a curve, because a formula was the wrong shape

The first implementation fitted a power law (a straight line in log space).
Measured against the 12,821 anime anchors it carried a systematic tilt:
overestimating the quiet end by 1.47x and underestimating the popular end by
0.74x, a two-fold swing across the range that shows up as sources interleaving
wrongly. It also pushed AniList's calibrated maximum to 2.7M, well past MAL's
real maximum of ~1.46M.

Binning the anchors and interpolating between their medians assumes only that
the relationship increases, which is the one thing that must be true:

```
                median |residual|   bias, quiet end -> popular end
power law            x1.56              1.47x  ->  0.74x
monotone curve       x1.46              1.22x  ->  0.87x
```

The residual x1.46 is not scale error and no monotone mapping will remove it:
MAL and AniList are different audiences that genuinely disagree about individual
works. Calibration removes the systematic difference, not the honest one.

Three tiers by how much evidence there is, degrading rather than failing:
**curve** (200+ anchors), **offset** (3+, a median log ratio that one outlier
cannot drag), **quantile** (none -- the only path that reintroduces the
representative-sample assumption, so it logs a warning and is surfaced in the
bootstrap report).

---

## Done: AniList, and one row per work

Ingested 2026-08-29. `anime_anilist` requested the 14,478 MAL ids already in the
catalog, 50 per GraphQL call, and `recsys.link` collapsed the duplicates that
created.

```
requested        14,478 MAL ids     from the existing catalog, popular first
returned         12,837 records     1,641 ids AniList does not carry
duration            9.3 min         290 requests, one 429, self-healed
entity resolution
  works           12,829            groups of 2+ rows describing one thing
  rows spanned    25,679
  collapsed       12,850            hidden from listings, still on disk
catalog          14,490 works       from 27,340 stored rows
```

**Synopsis coverage went from 7.3% to 80.0%** of visible works (11,593 of
14,490). Average tags per work went from 3.3 on the CSV rows to 6.9 overall,
7.4 on the AniList rows that now represent most works.

Which source represents a work, after ranking purely on content:

| Source | Works it represents | Avg tags |
|---|---|---|
| `anilist` | 12,561 | 7.4 |
| `mal` | 1,929 | 3.3 |
| `jikan` | 0 | -- |

The 1,929 MAL-represented works are mostly ones AniList does not carry: obscure
2018-era entries. Jikan represents none -- its rows are real synopses but carry
fewer tags than AniList's, so they lose the ranking on content.

**These numbers are the second pass.** The first ranked rows by description
*length* after "has a description", which is a proxy that measures the wrong
thing: the CSV dump's `background` is production trivia ("created by screenplay
writer Jun Maeda and directed by Seiji Kishi") and is routinely longer than a
real plot summary. That handed 120 works, *Angel Beats!* and *Elfen Lied* among
them, to a trivia blurb over AniList's synopsis. Ranking on tag count before
length fixes it and has no equivalent failure mode -- 20 tags against 6 is
richer by any reading. Relinking is a sub-second rebuild with no re-ingest.

### Resolution, not merging

`links` records that rows describe one work and which one represents it. Reads
collapse on that; nothing is merged and nothing is deleted. The losing rows stay
on disk and stay reachable by uid, because:

- **Phase 3 wants the union.** AniList brings text and ranked tags; the dump
  brings a 2018 member count from a different era. `link.work_members()` returns
  a work's full set for exactly that.
- **A source stays removable.** Drop an ingester, rebuild links, and the catalog
  is what the remaining sources say. After a merge there is no way back.

**The representative is chosen by content, never by source name** -- a synopsis
first, then more tags, then more ratings, with a deterministic tiebreak so
rebuilds never reshuffle the catalog. AniList wins today because it carries
text, but the rule never names AniList. A source-preference list would put source
names inside `link.py` and `db.py` and make "add a source" mean "edit them".

Verified precise rather than greedy: searching *Fullmetal Alchemist:
Brotherhood* returns three results -- the series, the OVA collection and the
4-Koma Theater -- because those are three works with three MAL ids. The series'
own three rows (`anilist`, `jikan`, `mal`) collapsed to one.

### What this cost, and what it did not

The predicted cost was real and is now paid: the catalog holds 27,340 rows to
show 14,490 works. The predicted *risk* -- near-duplicates in search until a
linker existed -- never materialised, because the linker shipped in the same
stretch of work.

Entity resolution here is an exact integer join on `external_ids.mal_id`, which
every anime source populates. It is not the hard version of the problem. Linking
a book to a film with no shared id is, and nothing here solves that.

---

## Done: the MAL ratings matrix

| File | Size | Contents | Used? |
|---|---|---|---|
| `AnimeList.csv` | 10 MB | 14,478 anime | ✅ Phase 0 |
| `animelists_cleaned.csv` | 2.11 GB | 31,284,030 list entries | ✅ `anime_mal_ratings` |
| `UserAnimeList.csv` | 4.65 GB | ~80M entries, raw | ❌ see coverage below |
| `users_cleaned.csv` | 15 MB | 108,712 users + demographics | ❌ no consumer yet |

31,284,030 rows read → **19,172,125 ratings**, 12,111,905 skipped as unscored
(`my_score` = 0 means "on the list, unrated"). 106,402 users, 6,598 anime,
mean score 7.59, timestamps 2006-09 to 2018-05. ~75 s; the database goes from
34 MB to 1.3 GB.

**The contract question is settled: a sibling, not a subclass.** `Ingester` is
fetch → `UnifiedItem` → `upsert_items`, and all three are wrong for a rating.
`ratings_mal_csv.py` shares `IngestResult` and the `ingest_runs` row, duplicates
the ~20-line batching loop, and satisfies the `IngestRunner` Protocol that
`bootstrap.py` and `registry.py` now run against. Adding a ratings source is
still one file plus one registry line — the same seam as a catalog source.

### The caveat that only showed up after loading it

**`animelists_cleaned.csv` is not just cleaned, it is filtered.** It covers only
the 6,668-anime `anime_cleaned.csv` subset, so **6,598 of the 14,503 catalog
items (45%) carry any rating at all**. The roadmap's earlier read of it — "same
shape, sane dates" — was incomplete.

That is fine for a first CF model: the covered 45% is the half people actually
watch, and every one of the 19.2M ratings joins to a real catalog row (zero
orphans). It is not fine for evaluating coverage or cold-start behaviour, where
the missing 8,000 items are exactly the interesting cases. `UserAnimeList.csv`
(80M entries, full catalog, unix-epoch dates) is the upgrade path when that
matters — the loader already reads both, via `MAL_RATINGS_FILENAME`.

## Next step, and why

**Build `recsys/models/`.** The data constraint below means two models, not
one; the within-domain CF half is now fully fuelled and is the place to start.
Ahead of it, Phase 2's parquet export is the natural bridge: `ratings` has no
secondary index because a bulk full-scan export is its only intended reader.

---

## Personalisation, and how the domains get bridged

### The short version

Loom knows nothing about you until you rate something. Once you do, three things
happen, and only the third one is hard.

1. Your rating becomes an ordinary row in `ratings`, under `local:` instead of
   `mal:`. You are user 106,403 in a table that already holds 106,402 others.
2. **Inside one domain that is enough.** Anime recommendations come from "people
   who liked what you liked also liked X", computed from 19.2M real ratings.
3. **Across domains that trick is impossible.** No human being appears in both
   the MAL data and the Goodreads data, so "people who liked this anime also
   liked this book" has never been observed by anybody. That is the bridge
   problem, and the rest of this section is the fix.

### Why ten ratings is enough, not ten thousand

The 106,402 MAL users already did the expensive work: they established which
anime resemble which. Your ratings do not have to build that map, only to drop a
pin on it. Ten to twenty ratings locate you; after that the curve flattens. This
works only because your rows share a table and a 0-10 scale with the corpus,
which is what the Phase 3 schema bought.

### The bridge: compare attributes, not titles

A title exists in one domain only. An attribute does not.

*Steins;Gate* and *Dark Matter* have nothing in common as titles, and no shared
audience anywhere in our data. But "time travel", "psychological", "thriller",
"paranoid protagonist" describe both. So the fix is to stop matching titles and
start matching attributes: turn what you liked into a **profile made of
concepts**, then score every item in every domain against that profile.

You are the only person in the system who consumes across domains. That is far
too small a sample to *learn* a cross-domain model from, but it is exactly enough
to *apply* one, because the attributes are shared by construction.

### A recommendation, start to finish

You rate *Steins;Gate* 10.

1. **Read its attributes.** Tags: sci-fi, thriller, psychological, time travel.
   Plus creators, studio, year.
2. **Ask the corpus what those mean.** 19.2M ratings say people who loved it also
   loved *Monster*, *Erased*, *Paranoia Agent*. Those share psychological and
   thriller, but *not* sci-fi. So the profile weights psychological heavily and
   sci-fi lightly. The corpus decided that, not a human.
3. **Discount common attributes.** "Action" sits on a third of the catalog and
   says almost nothing. "Time travel" is rare and says a lot. Rare attributes
   carry the weight.
4. **You now have a profile:** a weighted bag of concepts, with no anime in it.
5. **Score other domains against it.** Every book gets a match score. Blake
   Crouch's *Dark Matter* -- thriller, psychological, parallel worlds -- ranks
   high. So does the *Steins;Gate* light novel, but for a much better reason
   (below).
6. **Show the reason.** "Because you rated Steins;Gate 10 -- both are
   psychological + time travel." That sentence is also the edge label in the
   graph view, which is this same machinery drawn instead of ranked.

### Three bridges, strongest first

| Bridge | How it links | Confidence | Where the data is |
|---|---|---|---|
| **Same work** | An anime and the novel it adapts are *one work* in two domains | Certain | `external_ids` today, Phase 2's `links` table |
| **Same person** | Author of the novel, director, studio | High | `UnifiedItem.creators`, already populated |
| **Same concepts** | Shared tags and genres, rarity-weighted | Moderate | `UnifiedItem.tags`, already populated |
| **Same story shape** | Synopsis text in one embedding space | Highest | **Available** -- 80% coverage via AniList |

Adaptations are the quiet winner. A large share of anime adapt manga or light
novels; a large share of films adapt books. Every one is a hard, non-guessed link
between two domains, and Loom already stores the ids needed to find them. "You
loved the anime, here is the novel it came from, and three more by the same
author" needs no model at all -- only the `links` table Phase 2 already plans.

Text would be the strongest bridge of all: one sentence-embedding model reads an
anime synopsis and a book blurb into the same space, with no alignment step at
all. Loom cannot use it yet because the CSV dump ships no synopsis -- which makes
this the clearest argument yet for a source that carries real text.

### Where the synopsis text comes from: AniList

**No dump has it.** All three anime tables in the Kaggle archive (`AnimeList.csv`,
`anime_cleaned.csv`, `anime_filtered.csv`) share the same 31 columns, and the
closest thing to prose in any of them is `background` at 7.3% coverage. The
archive is a dead end for text.

**AniList's public GraphQL API has it**, and it lands on top of the data Loom
already holds. Probed live on 2026-08-29, no auth, no key:

```
POST https://graphql.anilist.co        HTTP 200
x-ratelimit-limit: 30                  (per minute)

MAL    1 -> AniList     1  Cowboy Bebop      synopsis   424 chars
MAL 5114 -> AniList  5114  Fullmetal A. B.   synopsis  1430 chars
MAL 9253 -> AniList  9253  Steins;Gate       synopsis   741 chars
```

Three things arrive in the same request, and the second one may matter more than
the synopsis:

1. **Real synopsis text**, a few hundred to ~1,500 characters per title. This is
   what unblocks the strongest bridge -- one embedding model over an anime
   synopsis and a book blurb, no vocabulary alignment needed.

2. **A far richer tag vocabulary than MAL's ~43 genres**, and ranked. Observed
   for *Steins;Gate*: `Time Manipulation`, `Time Loop`, `Alternate Universe`,
   `Memory Manipulation`, `Conspiracy`, `Tragedy`, `Denpa`. Compare MAL's four
   genres for the same title. The worked example earlier in this section assumed
   attributes like "time travel" and "psychological" existed; on MAL data they
   mostly do not, and on AniList data they do. This lifts the documented
   "thin vocabulary caps the profile" weakness directly.

3. **Adaptation edges** -- `relations` carries `SOURCE` / `ADAPTATION` links.
   Observed: *Cowboy Bebop* and *Fullmetal Alchemist: Brotherhood* both link to
   their source manga. That is the "same work" bridge, the strongest of the
   three, delivered by the same call that fetches the text.

**`idMal` is returned on every record**, which is exactly the join key already
stored in `external_ids.mal_id` on all 14,478 CSV rows. The link is an exact
integer match, not fuzzy title matching.

#### Cost

50 records per page via `idMal_in`, so ~290 requests for the whole catalog.
At the observed 30/min limit that is **~10 minutes, once**. `ingest/ratelimit.py`
already does sliding-window limiting; 30/min is a settings value, not new code.

#### Consequences worth accepting before starting

- **It is a third source, not an edit to existing rows.** Rows land as
  `anime:anilist:{id}` beside `anime:mal:{id}`, because `item_uid` is derived and
  a source never writes into another source's row. Nothing already stored is
  touched -- consistent with the additive rule above.
- **So the catalog roughly doubles in visible rows, with near-duplicates in
  search, until Phase 2's linker lands.** This is the real cost of the approach
  and it argues for doing entity resolution in the same stretch of work.
- **But it makes Phase 2 easier, not harder.** Reconciling `anime:anilist:X` with
  `anime:mal:Y` is an exact join on `mal_id`. No fuzzy matching, no thresholds.
  The hard version of entity resolution stays hard; this pair is trivial.
- **AniList covers ANIME and MANGA only.** Visual novels and games are absent, so
  *Steins;Gate* returns no source edge -- its origin is a visual novel. Expect
  adaptation coverage to be good for manga/light-novel sources and empty for the
  rest.
- **Descriptions carry `<br>` tags** even with `asHtml: false`. Strip at map
  time. And they are third-party text going into a UI that inserts through
  `textContent` only -- that rule already covers it, but this is the source that
  makes it matter.

#### Manga is the reason to take the whole thing

AniList's `MANGA` type is a second domain, with synopses and the same tag
vocabulary, joined to anime by explicit adaptation edges. That is a cross-domain
pair with **hard links rather than inferred ones** -- the only such pair Loom is
likely to get. It is the cleanest possible test of whether the bridge works at
all, before books and movies arrive with only content similarity to go on.

#### Text for the other domains, when they land

| Domain | Source of text | Join key | Notes |
|---|---|---|---|
| Anime / manga | **AniList GraphQL** | `idMal` -> `external_ids.mal_id` | Verified above |
| Movies / TV | **TMDB** `overview` | MovieLens `links.csv` carries TMDB ids | Exact join, free key |
| Books | OpenLibrary / Google Books | ISBN, in goodbooks-10k | Coverage varies; unverified |
| Books | UCSD Goodreads (McAuley) | Goodreads book id | Believed to ship descriptions; **check before committing** |

The pattern repeats: the ratings dumps carry ids but no prose, and a metadata API
joined on those ids supplies the prose. Budget one text-enrichment source per
domain rather than expecting any single dump to be complete.

### Making tag vocabularies line up

Each source names things differently: "Sci-Fi" on MAL, "science fiction" on
Goodreads, "Sci-Fi" again as a MovieLens genre. Two steps:

1. **A hand-written vocabulary file** mapping source tags to canonical concepts.
   Unglamorous, and most of the win. It is authored reference data rather than
   catalog data, so it belongs in the repo beside the code.
2. **Anchors for the rest.** Tags that appear identically in two domains, plus
   the adaptation links above, pin the spaces together. Anything left over is
   aligned against those fixed points rather than guessed.

### What this costs in schema: almost nothing

- Your ratings: existing `ratings` table, `local:` prefix. **No migration.**
- Cross-domain links: the `links` table Phase 2 already plans.
- Feature vectors: parquet under `DATA_DIR`, as Phase 2 already plans.
- Tag vocabulary: one authored file in the repo.

The bridge is mostly *use* of things already scheduled, not new invention.

### Nothing already stored gets touched

Worth stating as a rule, because it is easy to violate: the bridge is
**additive**.

- Raw tags stay verbatim in `items.tags`, and the untouched source payload stays
  in `raw_json`. Canonicalisation happens in a separate layer at feature-build
  time, so a wrong mapping is fixed and rebuilt **without re-ingesting anything**
  -- the same reasoning that put `raw_json` there in the first place.
- Two sources describing one work still stay two rows. `links` records that they
  are the same; it never merges them.
- New tables only, through new migrations. Nothing shipped is edited.

### Split the heavy part from the personal part

Item-to-item similarity is a property of the corpus, not of you. Rating something
does not change how similar two anime are -- only which neighbourhoods you sit
in. So build the model layer along that seam:

- **Heavy, computed once, offline:** similarity matrices, feature vectors, tag
  weights. Rebuilt only when the catalog or the corpus changes.
- **Light, recomputed per rating, in milliseconds:** your profile, and scoring it
  against those precomputed artifacts.

Then "gets better the more you use it" costs a fast re-score per rating. Built
instead as one monolithic fit-on-everything step, every new rating costs minutes.

### Day one, when you have rated nothing

Best answer: **import an existing library.** A MAL or AniList export is a list of
scored anime in exactly the shape `ratings` wants -- the same loader pattern as
`ratings_mal_csv.py`, writing `local:` rows. Hundreds of ratings on first launch
and no cold start at all. Given the catalog is already MAL-shaped, this is the
cheapest large win left on the roadmap.

Failing that, fall back to popularity plus content similarity to whatever was
just viewed -- and say in the UI that it is not personalised yet, rather than
pretending it is.

### Where this will be weak, honestly

- **Content beats nothing, but it loses to CF.** Cross-domain suggestions will be
  less sharp than within-domain ones. Present them as such.
- **Shallow matches are the failure mode.** "You like sci-fi anime, here is a
  sci-fi book" is technically correct and useless. Rarity weighting and
  corpus-learned tag weights are what prevent it; if recommendations feel
  obvious, those are the dials to turn.
- **MAL ships ~43 genres.** A thin vocabulary caps how fine-grained a profile can
  get until real synopsis text arrives.

### Explicit ratings first; behaviour logging is a separate yes/no

Views, dwell time and dismissals are richer than stars, and are what most "learns
as you use it" systems actually run on. They also need an `events` table, a
migration, and a deliberate decision to record behaviour rather than opinions.
Do explicit ratings first: unambiguous, no migration, enough to prove the loop.

Related and already reopened: `ratings_mal_csv.py` deliberately drops
`my_status` and `my_watched_episodes`. If implicit feedback becomes a goal, that
is the corpus-side equivalent already sitting in the file, 75 seconds away.

### It stays local

All of it lives in `%LOCALAPPDATA%` beside the catalog. No account, no server, no
telemetry -- a real advantage of the desktop architecture, and free to keep, as
long as nothing later ships a "sync" feature without revisiting this.

---

## The similarity graph view

An animated web of what you have consumed, nodes drawn together by similarity —
the visual idiom of a neural-network diagram, glowing interconnected nodes,
applied to the catalog. Hover an anime and the things it pulls on light up.

**The premise is right: the data is a byproduct.** A content model recommends by
computing item-to-item similarity and keeping the nearest neighbours. That
*is* the graph — recommendation collapses the neighbour list into a ranking,
this view keeps it and draws it. Nothing extra needs to be modelled or stored;
one route (`/api/similar/{item_uid}?k=`) reads what Phase 3 already produces.

So the honest cost is not the intelligence, it is the rendering and one missing
prerequisite.

### It is a similarity graph, not a neural network

Worth keeping the names straight in the code even if the visual borrows the
idiom. The neural network, if the content model ends up using embeddings at all,
is the thing that *computes* the similarities; what gets drawn is a k-nearest-
neighbour graph over items. Naming the module `similarity_graph` and not
`neural_net` avoids a file that lies about what it holds.

### The prerequisite: Loom does not know what *you* consume

`ratings` holds 106,402 MAL users. That is training data, not you. Nothing in
the app records the local user's own library, so "what you consume" has no
source yet.

Cheap to fix, and no migration: `user_uid` is already namespaced by dataset via
`make_user_uid`, so your own ratings are rows under a reserved `local:` prefix
in the same table. The model treats you as one more user; the graph view seeds
itself from `WHERE user_uid LIKE 'local:%'`. What is missing is only the UI
affordance to rate something, plus optionally an importer for a MAL/AniList
export so the library does not start empty.

### What is genuinely not free

- **No build step, no CDN.** That rule kills d3-force, sigma and cytoscape
  unless one is vendored in as a single committed file. A hand-rolled force
  simulation on `<canvas>` is ~150 lines for a few hundred nodes and keeps the
  dependency count at zero. Recommended over vendoring.
- **Scale forces an ego-network.** 14,503 nodes is a grey smear and the browser
  will not animate it. Draw your items plus their top-k neighbours, capped
  around 300–800 nodes, expanding on click.
- **Labels are third-party text.** Canvas `fillText` is fine, but any HTML
  tooltip goes through `textContent` like everything else in the UI.

### The risk worth designing against

**Force-directed layouts lie by default.** Distance in the final layout is not
similarity — it is wherever the simulation happened to settle, and if every node
links to every other the result is an undifferentiated hairball that looks
profound and says nothing. Mitigation is to threshold hard from the start: top-k
edges only, and only above a similarity floor. Treat the layout as navigation,
never as measurement.

### Why it is worth more than a toy

The roadmap already commits to cross-domain recommendations coming from content,
because no user rates across two of these datasets. That claim is otherwise very
hard to inspect. A graph where an anime sits adjacent to a novel, with a visible
edge and a reason for it, is the clearest available demonstration that the
cross-domain model works — or the fastest way to see that it does not. It is a
debugging tool for the similarity model that happens to be pleasant to look at.

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

- **Within a domain:** collaborative filtering works, and works well — 19.2M
  anime ratings across 106k users is plenty.
- **Across domains:** recommendations must come from **content** — shared tags,
  text embeddings, genre space. This is exactly why `UnifiedItem` collapses
  every domain into one table: it is the only thing that makes an anime and a
  novel comparable.

So Phase 3 is really two models, not one. Anything promising cross-domain CF is
promising something the data cannot support.

The one exception is the local user, who does rate across domains — see
personalisation above. That is a sample size of one: enough to apply a shared
content space across domains, never enough to learn one.

A cross-domain dataset with overlapping users would change this. None of the
common public ones have it.

---

## Known issues carried forward

- ~~Cross-source popularity ordering is wrong.~~ **Resolved**, by calibrating
  on overlap rather than by percentile ranking. See below.

- ~~13 duplicate works across sources.~~ **Resolved.** 12,829 works now span
  25,679 rows, and reads show one each. `link.py` joins on
  `external_ids.mal_id` and marks a representative; nothing is merged, so every
  source row stays on disk and `link.work_members()` returns a work's full set
  for Phase 3.

- **The CSV dump has no synopsis** — and no other file in the archive does
  either; all three anime tables share the same 31 columns. `description` falls
  back to `background` (production trivia) at 7.3% coverage, so FTS leans almost
  entirely on titles for the CSV half of the catalog. **Resolved.** AniList now
  represents 12,561 of the 14,490 works and synopsis coverage is 80.0%. What
  remains is the 1,929 works AniList does not carry, which keep the dump's 7.3%
  and stay text-poor -- they are mostly obscure 2018-era entries.

- **The ratings file stamps "no timestamp" as 1970-01-01.** 97,093 of the
  19.2M rows carry it. Left alone it reads as a real date and would land in
  every time-ordered train/test split Phase 3 makes, so `_parse_rated_at`
  normalises it — and the zeroed MySQL date, and a bare `0` — to NULL. MAL
  launched in 2004, so the sentinel is unambiguous.

- **Jikan is degraded to its warm cache.** Page 1 of `/top/anime` returns 200;
  everything deeper 504s, as do search, seasons, relations, recommendations, and
  `/anime/{id}` for any non-popular id. Not routable around — see
  `PHASE0_TODO.md` for the measured endpoint matrix and the sampling trap that
  makes id lookups look usable. Re-run `bootstrap.py` once it recovers; upserts
  are idempotent.

---

## Storage

Usage after the ratings load: **1.3 GB** catalog + ratings, 9.4 GB kagglehub
cache. `C:` has ~508 GB free, so **the external drive is still not needed.**
Worth knowing: the ratings table is ~97% of that database, and re-running the
loader rewrites it in ~75 s, so it is cheap to drop and rebuild.

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
