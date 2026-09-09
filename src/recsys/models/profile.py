"""A taste profile: one vector standing for everything you liked.

``similarity.py`` answers "what is like this item". This answers "what is like
*you*", which is the same query with the point moved: instead of one work's
vector, the centroid of the works you rated well. Because that centroid lives
in the same 384-dimension space as every domain, asking for the books nearest
it works having rated nothing but anime -- the thing collaborative filtering
structurally cannot do here, since all 19.2M corpus ratings are anime.

**Which ratings count.** The ones at or above your own mean. Not a fixed bar on
the 1-10 scale: people do not share one, and MAL's corpus mean of 7.59 says
plainly that "5" is not the middle of how anyone actually rates. Your mean is
the only threshold that is yours, and it needs no constant.

That one rule also covers the cases a threshold usually special-cases. A single
rating is its own mean, so it counts and a profile exists after one click.
Rate ten things 8 and all ten are at the mean, so all ten count rather than
none. There is no ``n >= 2`` guard anywhere below because nothing needs one.

**What is deliberately not here: a negative term.** Rocchio would subtract a
centroid of what you disliked, and it might well help. It also introduces a
weight with no defensible value -- this project's rule is that a model claim
needs a measurement, and there is no held-out set for a taste profile the way
adaptation edges are one for the content model. Excluding what you rated below
your average is the weaker claim the evidence supports; subtracting it is a
tuning exercise waiting for data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from recsys.models.similarity import SimilarityIndex


@dataclass
class Profile:
    """A taste vector, with the arithmetic behind it kept visible.

    ``n_rated`` counts every rating you have; ``n_used`` counts the ones that
    were both in the embedding matrix and at or above your mean. The gap
    between them is the honest caveat on any recommendation built from this --
    a profile drawn from three of your ninety ratings is not the profile the
    number ninety suggests.
    """

    vector: Any
    n_rated: int
    n_used: int


def build_profile(index: SimilarityIndex, ratings: Mapping[str, float]) -> Profile | None:
    """The centroid of the works you rated at or above your own mean.

    ``ratings`` maps listing-space uids to scores -- what ``db.rated_uids``
    returns, already translated through ``links``, because the matrix holds
    only the row that represents each work.

    ``None`` when there is nothing to build from: no ratings, or none of the
    rated works are in the matrix (a catalog rated after the last
    ``scripts/embed.py`` run). A caller must distinguish that from "no
    recommendations", because the fix is different -- rate something, versus
    rebuild the matrix.
    """
    import numpy as np

    placed = {uid: score for uid, score in ratings.items() if index.has(uid)}
    if not placed:
        return None

    mean = sum(placed.values()) / len(placed)
    liked = [uid for uid, score in placed.items() if score >= mean]

    centroid = np.mean([index.vector_for(uid) for uid in liked], axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm == 0.0:
        # Every liked vector cancelled every other. Vanishingly unlikely with
        # real embeddings and not worth a fabricated answer if it happens: the
        # matrix is queried by dot product, and a zero vector would score every
        # work in the catalog identically at 0.0.
        return None

    return Profile(vector=centroid / norm, n_rated=len(ratings), n_used=len(liked))
