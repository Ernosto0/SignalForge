"""Independent sources (plan §7.3): which documents count as one source.

Counting rule (conservative): one independent source is one document after collapsing duplicate
groups — ``near_dup`` / ``syndicated`` text (dedupe stage), ``same_author`` (the same salted
author hash on several documents) and ``same_quote`` (the same quoted passage on several
documents, e.g. one complaint shown on two listing pages of a complaint site; both extract
stage). Several authors inside one forum thread still count as one source.
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


def _shared_key_groups(
    keys_by_document: Mapping[int, Iterable[str]], rule: str
) -> list[DuplicateGroup]:
    """Groups of ≥2 documents linked by sharing a key (transitively)."""
    documents_by_key: dict[str, set[int]] = defaultdict(set)
    for document_id, keys in keys_by_document.items():
        for key in keys:
            documents_by_key[key].add(document_id)
    uf = UnionFind(keys_by_document)
    for document_ids in documents_by_key.values():
        first, *rest = sorted(document_ids)
        for other in rest:
            uf.union(first, other)
    return [DuplicateGroup(rule=rule, document_ids=g) for g in sorted(uf.groups()) if len(g) > 1]


def same_author_groups(authors_by_document: Mapping[int, Iterable[str]]) -> list[DuplicateGroup]:
    """Groups of ≥2 documents that share an author hash."""
    return _shared_key_groups(authors_by_document, "same_author")


def same_quote_groups(
    quotes_by_document: Mapping[int, Iterable[str]], min_words: int
) -> list[DuplicateGroup]:
    """Groups of ≥2 documents that carry the same quoted passage (casefolded word tokens).

    Quotes shorter than ``min_words`` are ignored: a short stock phrase ("kargo teslim
    edilmedi") written by two different people must not merge their sources.
    """
    keys: dict[int, set[str]] = {}
    for document_id, quotes in quotes_by_document.items():
        tokens = (match_tokens(q) for q in quotes)
        keys[document_id] = {" ".join(t) for t in tokens if len(t) >= min_words}
    return _shared_key_groups(keys, "same_quote")


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
