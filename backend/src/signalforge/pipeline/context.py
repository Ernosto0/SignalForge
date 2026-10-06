from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import CacheMode, Defaults, Settings, get_defaults, get_settings
from signalforge.db.session import get_session_factory
from signalforge.packs import MarketPack, load_pack
from signalforge.providers.cache import CacheStore
from signalforge.providers.fetch import CachedFetcher, Fetcher
from signalforge.providers.llm import LLMService, OpenAIClient
from signalforge.providers.search import CachedSearch, make_search_provider


@dataclass
class RunContext:
    """Everything a stage needs: DB, cached providers, market pack, cache mode."""

    settings: Settings
    defaults: Defaults
    db: sessionmaker[Session]
    cache: CacheStore
    pack: MarketPack
    search: CachedSearch
    fetcher: CachedFetcher
    llm: LLMService
    run_id: int | None = None

    @classmethod
    def create(
        cls,
        *,
        cache_mode: CacheMode | None = None,
        pack_id: str | None = None,
        run_id: int | None = None,
        db: sessionmaker[Session] | None = None,
    ) -> "RunContext":
        settings = get_settings()
        defaults = get_defaults()
        db = db or get_session_factory()
        cache = CacheStore(db, cache_mode or settings.cache_mode, defaults.cache.ttl_days)
        api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        return cls(
            settings=settings,
            defaults=defaults,
            db=db,
            cache=cache,
            pack=load_pack(pack_id or settings.default_market_pack),
            search=CachedSearch(make_search_provider(settings, defaults.search.timeout_s), cache),
            fetcher=CachedFetcher(Fetcher(defaults.fetch), cache),
            llm=LLMService(
                OpenAIClient(api_key, defaults.llm.timeout_s, max_retries=defaults.llm.max_retries),
                cache,
                db,
                settings,
                defaults.llm.pricing,
                defaults.llm.max_output_tokens,
                run_id=run_id,
            ),
            run_id=run_id,
        )
