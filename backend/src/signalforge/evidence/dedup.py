"""Document-level duplicate detection (plan §7.3, §11): exact text hash + MinHash near-duplicates.

Duplicates collapse into one independent source. A group whose members all live on one domain is a
``near_dup`` (same page under several URLs, mirrors, templated pages); a group spanning domains is
``syndicated`` (copied or republished text). Author- and quote-based grouping (``same_author``,
``same_quote``) needs excerpts and happens in extraction (evidence/independence.py).
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from datasketch import MinHash

from signalforge.config import DedupeDefaults
from signalforge.text import match_tokens


@dataclass(frozen=True)
class DocText:
    id: int
    domain: str
    text: str


@dataclass(frozen=True)
class DuplicateGroup:
    rule: str  # near_dup | syndicated | same_author | same_quote
    document_ids: list[int]


def text_hash(text: str) -> str:
    """sha256 over Turkish-casefolded word tokens, so case/whitespace/punctuation variants match."""
    return hashlib.sha256(" ".join(match_tokens(text)).encode("utf-8")).hexdigest()


def shingles(tokens: list[str], k: int) -> set[str]:
    if len(tokens) <= k:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


class UnionFind:
    def __init__(self, ids: Iterable[int]) -> None:
        self._parent = {i: i for i in ids}

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for i in self._parent:
            out[self.find(i)].append(i)
        return [sorted(g) for g in out.values()]


def find_duplicates(docs: list[DocText], cfg: DedupeDefaults) -> list[DuplicateGroup]:
    """Groups of ≥2 documents that are the same text or near-duplicates. Singletons are omitted."""
    uf = UnionFind(d.id for d in docs)
    by_hash: dict[str, int] = {}
    # Every pair is compared: at V0 scale (≤ a few hundred documents) that is fast, and LSH
    # banding misses ~15% of pairs right at the threshold. Switch to MinHashLSH when runs grow.
    sketches: dict[int, MinHash] = {}
    for doc in docs:
        tokens = match_tokens(doc.text)
        digest = hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()
        if (first := by_hash.setdefault(digest, doc.id)) != doc.id:
            uf.union(first, doc.id)
            continue
        if len(tokens) < cfg.min_words:
            continue
        m = MinHash(num_perm=cfg.num_perm)
        m.update_batch(s.encode("utf-8") for s in shingles(tokens, cfg.shingle_words))
        for other, sketch in sketches.items():
            if m.jaccard(sketch) >= cfg.near_dup_threshold:
                uf.union(other, doc.id)
        sketches[doc.id] = m

    domains = {d.id: d.domain for d in docs}
    return [
        DuplicateGroup(
            rule="near_dup" if len({domains[i] for i in g}) == 1 else "syndicated",
            document_ids=g,
        )
        for g in sorted(uf.groups())
        if len(g) > 1
    ]
