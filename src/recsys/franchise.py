"""Same-franchise suppression: what "what next?" must not be answered with.

Rate *DEATH NOTE* and the nearest forty works to your taste were, measured on
the real catalog, twenty-one more *Death Note*: the manga, its one-shots, eight
Viz volumes filed under books, six films and two TV rows. Every one a correct
neighbour and none a recommendation.

Two mechanisms hide them, because the catalog knows about them in two ways.

**Relation edges**, where a source asserts them. ``relations`` holds AniList's
sequel, side-story and adaptation edges, and
``relations.franchise_representatives`` walks them. That covers anime and manga
and stops there -- no edge leaves AniList.

**Titles**, for everything else. A TMDB film and an Open Library volume share
no identifier with the anime they belong to, and the only thing the catalog
holds that connects them is that they are *called* the same thing. So a
candidate is held back when its title is one of yours or extends one:
``Death Note``, ``Death Note: The Last Name``, ``Death Note, Vol. 1``.

**This is not ``links``, and the difference is why a title match is allowed
here.** ``link.py`` refuses fuzzy matching outright, and rightly: a false link
hides a real work behind an unrelated one, everywhere, for good. A false match
here costs one slot on one page of suggestions, and ``same_franchise=true``
shows what was held back. The stakes set the bar.

It is still guarded, because titles collide:

* **Only the titles of the works actually asked about are used**, never those
  of their relatives. The first version took names from the whole franchise,
  and measured over 1,500 popular anime it held back the *MONSTER* manga for
  anyone who rated *Golden Kamuy*, whose franchise includes an episode by that
  name. A franchise of forty works has forty chances at a collision; the work
  you rated has one.
* An exact match always counts. Two works with one name are a franchise or a
  coincidence, and the embedded document *leads with the title* -- the
  coincidence ranks high for being a string match, which is no reason to
  recommend it.
* A longer title counts when it extends one of two or more words
  (``Death Note Relight``). A one-word title needs punctuation or a numeral
  after it: ``Monster: The Movie`` and ``Alien 3`` extend ``Monster`` and
  ``Alien``; ``Monster House`` and ``Alien Nation`` do not. A leading article is
  not a word for this count, or rating *The Boy* would hide *The Boy and the
  Heron*.
* It runs the other way only across a marked subtitle or a numbering, and
  only down to a name of two or more words: rating *Attack on Titan Season 2*
  holds back *Attack on Titan*, but rating *Fate/Zero* holds back nothing
  called *Fate*.
* Titles too short to mean anything (``K``, ``X``) are never used.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from recsys.schemas import UnifiedItem

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_ARTICLES = frozenset({"the", "a", "an"})

#: Punctuation that, standing between a title and whatever follows, marks a
#: subtitle. A bare space does not: that is just a longer name. Nor does a
#: comma, which is as often a list -- *Love, Election & Chocolate* is not an
#: entry in a series called *Love*.
_SUBTITLE_MARKS = frozenset(":;(/[~|–—")  # noqa: RUF001 - the dashes are the point

#: Words that number an entry rather than name a different thing. The roman
#: numerals stop short of ``v`` and ``x`` on purpose: *Hunter x Hunter* is not
#: the tenth *Hunter*.
_NUMBERING = frozenset({
    "season", "part", "vol", "volume", "movie", "film", "chapter", "book", "episode",
    "ii", "iii", "iv", "vi", "vii", "viii", "ix",
})


def _fold(text: str) -> str:
    """Case and accents removed, so *Pokémon* and *POKEMON* are one title."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def title_key(title: str) -> tuple[str, ...]:
    """A title reduced to what two spellings of it have in common."""
    return tuple(_TOKEN_RE.findall(_fold(title)))


def _usable(key: tuple[str, ...]) -> bool:
    """Whether a title is long enough to identify anything.

    Three characters for Latin script, two otherwise: *蟲師* is a distinctive
    title and *K* is not. A leading article is not counted, so nothing is ever
    filed under ``the``.
    """
    joined = "".join(key[1:] if key and key[0] in _ARTICLES else key)
    return len(joined) >= (3 if joined.isascii() else 2)


def _is_phrase(key: tuple[str, ...]) -> bool:
    """Two or more words, not counting a leading article."""
    return len(key) - (1 if key and key[0] in _ARTICLES else 0) >= 2


def _heads(title: str) -> list[tuple[tuple[str, ...], bool]]:
    """Every proper prefix of ``title``, and whether a subtitle starts there.

    "Starts there" means the next thing is punctuation or a numbering -- the
    places a title is conventionally cut: ``Death Note | : The Last Name``,
    ``Attack on Titan | Season 2``.
    """
    folded = _fold(title)
    spans = [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(folded)]
    words = tuple(word for word, _, _ in spans)
    out: list[tuple[tuple[str, ...], bool]] = []
    for n in range(1, len(words)):
        following = words[n]
        between = folded[spans[n - 1][2] : spans[n][1]]
        marked = (
            following.isdigit()
            or following in _NUMBERING
            or " - " in between
            or any(mark in between for mark in _SUBTITLE_MARKS)
        )
        out.append((words[:n], marked))
    return out


def extended_titles(title: str) -> list[tuple[str, ...]]:
    """Every shorter title that ``title`` would count as an entry of.

    *Death Note: The Last Name* extends ``death note``; *Monster House* extends
    nothing, because one word followed by another word is just a longer name.
    """
    return [head for head, marked in _heads(title) if marked or _is_phrase(head)]


def base_titles(title: str) -> list[tuple[str, ...]]:
    """``title`` itself, and what it is called with its subtitle cut off.

    Stricter than :func:`extended_titles` twice over, because this derives the
    names everything else is tested against. The cut must be *marked* --
    *Attack on Titan* does not become ``attack on`` -- and what is left must
    still be a phrase: measured on the catalog, one-word stems are where the
    collisions live. *Mission: Yozakura Family* yields ``mission`` and hides
    *Mission: Impossible*; *Fate/Zero* yields ``fate`` and hides *Fate: The
    Winx Saga*. The price is that rating only *Naruto: Shippuuden* no longer
    holds back a film called plain *Naruto* by its title -- its relation edges
    still do, wherever the catalog has them.
    """
    return [
        title_key(title),
        *(head for head, marked in _heads(title) if marked and _is_phrase(head)),
    ]


@dataclass
class Franchise:
    """Everything a set of works is known to belong with.

    ``uids`` are representative uids reached through relation edges; ``titles``
    are the names the works themselves go by. Both are needed: the first is
    exact and blind outside AniList, the second reaches every source and has
    to be hedged.
    """

    uids: set[str] = field(default_factory=set)
    titles: set[tuple[str, ...]] = field(default_factory=set)

    def covers(self, item: UnifiedItem) -> bool:
        """Whether ``item`` is more of the same rather than something new."""
        if item.item_uid in self.uids:
            return True
        if not self.titles:
            return False
        if any(title_key(t) in self.titles for t in (item.title, *item.alt_titles)):
            return True
        return any(head in self.titles for head in extended_titles(item.title))


def franchise_of(conn: sqlite3.Connection, item_uids: Iterable[str]) -> Franchise:
    """The franchises of ``item_uids``, merged into one thing to test against.

    Merged because the caller's question is: does this candidate belong with
    *anything* here? ``/api/similar`` passes the one work being looked at,
    ``/api/recommend`` everything you have rated.

    The works themselves are in ``uids`` too. Both callers already exclude
    them, and leaving them out would mean a second row of a work you rated --
    reachable by uid, not by listing -- had to be remembered separately.
    """
    from recsys.link import representative_uid, work_members
    from recsys.relations import franchise_representatives

    result = Franchise()
    # Names come from every row of each work, not just its representative: the
    # AniList row has the romaji title and the dump row the English one, and a
    # TMDB film is as likely to go by either.
    rows: set[str] = set()
    for uid in item_uids:
        result.uids.add(representative_uid(conn, uid))
        result.uids |= franchise_representatives(conn, uid)
        rows.update(work_members(conn, uid))

    ordered = sorted(rows)
    for start in range(0, len(ordered), 400):
        chunk = ordered[start : start + 400]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT title, alt_titles FROM items WHERE item_uid IN ({marks})", chunk
        ):
            for title in (row["title"], *json.loads(row["alt_titles"] or "[]")):
                result.titles.update(key for key in base_titles(title) if _usable(key))
    return result
