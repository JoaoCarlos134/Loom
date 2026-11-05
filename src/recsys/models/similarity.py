"""Nearest-neighbour queries over the embedding matrix.

Brute force, deliberately. Eighty thousand vectors of 384 float32 is 120 MB and
one matmul against the whole catalog takes single-digit milliseconds -- an
approximate index would add a heavy dependency, a build step and a recall
tradeoff to solve a problem this size does not have. Revisit at ten million
items, not before.

The vectors were L2-normalised when written, so a dot product *is* cosine
similarity and nothing here needs to normalise again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Neighbour:
    item_uid: str
    domain: str
    score: float


class SimilarityIndex:
    """The embedding matrix, loaded once and queried many times."""

    def __init__(self, uids, domains, matrix) -> None:
        self.uids = list(uids)
        self.domains = list(domains)
        self.matrix = matrix
        self._position = {uid: i for i, uid in enumerate(self.uids)}

    def __len__(self) -> int:
        return len(self.uids)

    @classmethod
    def load(cls, path: Path) -> SimilarityIndex:
        import numpy as np
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        dim = table.schema.field("vector").type.list_size
        flat = table.column("vector").combine_chunks().flatten().to_numpy(zero_copy_only=False)
        matrix = np.asarray(flat, dtype="float32").reshape(-1, dim)
        return cls(
            table.column("item_uid").to_pylist(),
            table.column("domain").to_pylist(),
            matrix,
        )

    def has(self, item_uid: str) -> bool:
        return item_uid in self._position

    def vector_for(self, item_uid: str):
        index = self._position.get(item_uid)
        return None if index is None else self.matrix[index]

    def similar_to_vector(
        self, vector, *, k: int = 10, domain: str | None = None, exclude: set[str] | None = None
    ) -> list[Neighbour]:
        """The k closest works to a vector, optionally within one domain.

        ``domain`` is what makes this a *cross-domain* recommender rather than a
        related-items widget: asking for the films nearest an anime is the whole
        point, and it is one filtered argsort away.
        """
        import numpy as np

        scores = self.matrix @ np.asarray(vector, dtype="float32")

        mask = None
        if domain is not None:
            mask = np.fromiter(
                (d == domain for d in self.domains), dtype=bool, count=len(self.domains)
            )
        if exclude:
            # Built from positions, not by testing all 80,000 uids: callers
            # exclude every row that lost its primary election, tens of
            # thousands of uids, on every request.
            keep = np.ones(len(self.uids), dtype=bool)
            positions = [self._position[u] for u in exclude if u in self._position]
            keep[positions] = False
            mask = keep if mask is None else (mask & keep)
        if mask is not None:
            if not mask.any():
                return []
            scores = np.where(mask, scores, -np.inf)

        take = min(k, int(mask.sum()) if mask is not None else len(self.uids))
        if take <= 0:
            return []
        # argpartition finds the top k without sorting all 80,000.
        top = np.argpartition(-scores, take - 1)[:take]
        top = top[np.argsort(-scores[top])]
        return [
            Neighbour(self.uids[i], self.domains[i], float(scores[i]))
            for i in top
            if scores[i] != -float("inf")
        ]

    def similar_to(
        self, item_uid: str, *, k: int = 10, domain: str | None = None
    ) -> list[Neighbour]:
        """The k works most like this one. Never returns the query itself."""
        vector = self.vector_for(item_uid)
        if vector is None:
            return []
        return self.similar_to_vector(vector, k=k, domain=domain, exclude={item_uid})
