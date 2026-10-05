"""Independent sources (plan §7.3): which documents count as one source.

Counting rule (conservative): one independent source is one document after collapsing duplicate
groups — ``near_dup`` / ``syndicated`` text (dedupe stage) and ``same_author`` (the same salted
author hash on several documents, extract stage). Several authors inside one forum thread still
count as one source.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping

from signalforge.evidence.dedup import DuplicateGroup, UnionFind
from signalforge.text import match_tokens

# Placeholder names shared by many different people; they must not merge sources.
_GENERIC_AUTHORS = {
    "anonim", "anonymous", "misafir", "ziyaretci", "ziyaretçi", "guest", "admin", "yonetici",
    "yönetici", "kullanici", "kullanıcı", "user", "uye", "üye", "editor", "editör", "moderator",
}  # fmt: skip
_GENERIC = {" ".join(match_tokens(name)) for name in _GENERIC_AUTHORS}


def author_hash(name: str | None, salt: str) -> str | None:
    """Salted sha256 of a casefolded author name; ``None`` for empty or placeholder names.

    Raw names are personal data (KVKK) and are never stored; the hash only serves independence
    counting.
    """
    if not name:
        return None
    key = " ".join(match_tokens(name))
    if not key or key in _GENERIC:
        return None
    return hashlib.sha256(f"{salt}\x00{key}".encode()).hexdigest()


def same_author_groups(authors_by_document: Mapping[int, Iterable[str]]) -> list[DuplicateGroup]:
    """Groups of ≥2 documents that share an author hash."""
    documents_by_author: dict[str, set[int]] = defaultdict(set)
    for document_id, hashes in authors_by_document.items():
        for h in hashes:
            documents_by_author[h].add(document_id)
    uf = UnionFind(authors_by_document)
    for document_ids in documents_by_author.values():
        first, *rest = sorted(document_ids)
        for other in rest:
            uf.union(first, other)
    return [
        DuplicateGroup(rule="same_author", document_ids=g)
        for g in sorted(uf.groups())
        if len(g) > 1
    ]


def source_units(document_ids: Iterable[int], groups: Iterable[Iterable[int]]) -> dict[int, int]:
    """Document id -> independent-source id (the smallest document id in its unit).

    Groups may overlap and may mention documents outside ``document_ids``; those are ignored.
    """
    ids = set(document_ids)
    uf = UnionFind(ids)
    for group in groups:
        members = sorted(i for i in group if i in ids)
        for other in members[1:]:
            uf.union(members[0], other)
    return {i: uf.find(i) for i in ids}
