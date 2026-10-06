"""Market packs (plan §9): per-country language, search locale, phrases, source registry, seeds."""

from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from signalforge.config import get_settings
from signalforge.providers.search import SearchLocale

Tier = Literal["high", "medium", "low"]


class SourceEntry(BaseModel):
    domain: str
    category: str
    tier: Tier
    access: Literal["fetch", "snippet_only"]
    notes: str | None = None


class EconomicReference(BaseModel):
    name: str
    value: float
    currency: str
    unit: str
    as_of: date
    source_url: str


class OfficialSource(BaseModel):
    domain: str
    about: str


class Mandate(BaseModel):
    name: str
    seeds: list[str]


class Regulatory(BaseModel):
    official_sources: list[OfficialSource] = []
    mandates: list[Mandate] = []


class Submarket(BaseModel):
    name_en: str
    name_tr: str


class Industry(BaseModel):
    id: str
    name_en: str
    name_tr: str
    terms: list[str] = []
    submarkets: list[Submarket] = []


class MarketPack(BaseModel):
    id: str
    version: str
    country: str
    language: str
    currency: str
    report_language: str
    search: SearchLocale
    default_tier: Tier
    pain_phrases: dict[str, list[str]]  # signal type -> phrases
    sources: list[SourceEntry]
    economics: list[EconomicReference]
    regulatory: Regulatory
    industries: dict[str, Industry]
    # Gap-matrix dimension -> search terms for a product's own site (competitors stage).
    competitor_probes: dict[str, str] = {}

    def source_for(self, domain: str) -> SourceEntry | None:
        """Registry entry for a domain or its closest listed parent domain."""
        domain = domain.lower().removeprefix("www.")
        by_domain = {s.domain: s for s in self.sources}
        labels = domain.split(".")
        for i in range(len(labels) - 1):
            if entry := by_domain.get(".".join(labels[i:])):
                return entry
        return None

    def tier_for(self, domain: str) -> Tier:
        entry = self.source_for(domain)
        return entry.tier if entry else self.default_tier


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@cache
def load_pack(pack_id: str, packs_dir: Path | None = None) -> MarketPack:
    root = (packs_dir or get_settings().market_packs_dir) / pack_id
    if not (root / "pack.yaml").is_file():
        raise FileNotFoundError(f"market pack {pack_id!r} not found in {root.parent}")
    industries = [Industry.model_validate(_read(p)) for p in sorted(root.glob("industries/*.yaml"))]
    probes = root / "competitor_probes.yaml"
    return MarketPack.model_validate(
        {
            **_read(root / "pack.yaml"),
            "pain_phrases": _read(root / "pain_phrases.yaml"),
            "sources": _read(root / "sources.yaml")["sources"],
            "economics": _read(root / "economics.yaml")["references"],
            "regulatory": _read(root / "regulatory.yaml"),
            "industries": {i.id: i for i in industries},
            "competitor_probes": (_read(probes) or {}) if probes.is_file() else {},
        }
    )
