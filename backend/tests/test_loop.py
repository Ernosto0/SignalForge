"""The bounded research loop (agents/loop.py): guards, budgets and what it hands back.

The model is scripted: each answer is chosen by the number of steps already in the history, so
the same script replays deterministically (and hits the LLM cache on a re-run).
"""

import re

from fakes import PACK, PLAN, FakeLLM, FakeSearch, make_context

from signalforge.agents.loop import LoopAction, LoopPage, _Observation, render_input, run_loop
from signalforge.config import get_defaults
from signalforge.pipeline.runner import create_run
from signalforge.prompts import load_prompt
from signalforge.providers.search import SearchHit, SearchLocale
from signalforge.providers.urls import canonicalize_url

DEFAULTS = get_defaults()
BUDGET = DEFAULTS.verify.model_copy(update={"max_steps": 8, "max_searches": 3, "max_fetches": 2})


def scripted(*actions: LoopAction):
    """LoopAction handler: answer number n+1 once n steps are in the history; then finish."""

    def answer(prompt_input: str) -> LoopAction:
        history = prompt_input.split("# History\n", 1)[1].split("# Budget left", 1)[0]
        done = len(re.findall(r"^step \d+:", history, re.MULTILINE))
        if done < len(actions):
            return actions[done]
        return LoopAction(action="finish", reason="script done")

    return answer


def search(query: str, site: str | None = None) -> LoopAction:
    return LoopAction(action="search", query=query, site=site, reason="r")


def fetch(hit_id: int) -> LoopAction:
    return LoopAction(action="fetch", hit_id=hit_id, reason="r")


def _loop(db, *actions: LoopAction, search_provider=None, budget=BUDGET, **kw):
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    llm = FakeLLM({LoopAction: scripted(*actions)})
    ctx = make_context(db, run_id, search_provider or FakeSearch(), llm)
    pages: list[LoopPage] = []

    def on_page(page: LoopPage) -> str:
        pages.append(page)
        return "1 signal kept"

    trace = run_loop(
        ctx,
        stage="verify",
        prompt=load_prompt("verify_loop"),
        goal="verify a problem",
        budget=budget,
        allowed_domains=kw.get("allowed", ["kariyer.net"]),
        known_urls=kw.get("known_urls", set()),
        known_queries=kw.get("known_queries", set()),
        on_page=on_page,
    )
    return trace, pages, llm


def test_search_then_fetch_reads_the_page_and_records_everything(db) -> None:
    trace, pages, _ = _loop(db, search("irsaliye elle giriş forum"), fetch(2))
    assert trace.stop_reason == "finish" and trace.steps == 3
    assert [s.query for s in trace.searches] == ["irsaliye elle giriş forum"]
    assert len(trace.searches[0].hits) == 3
    (page,) = pages
    assert page.hit.id == 2 and page.source == "text" and page.text
    assert trace.actions[1]["outcome"] == "1 signal kept"
    assert trace.summary()["fetches"] == 1


def test_fetch_must_name_a_hit_from_this_loop(db) -> None:
    # No search yet: hit 1 does not exist. Two rejections in a row stop the loop.
    trace, pages, _ = _loop(db, fetch(1), fetch(7))
    assert pages == [] and trace.stop_reason == "rejections"
    assert trace.rejections == {"unknown_hit": 2}


def test_known_and_repeated_urls_are_rejected(db) -> None:
    known = {canonicalize_url("https://forum.com/shared")}
    trace, pages, _ = _loop(db, search("irsaliye takibi"), fetch(1), fetch(2), fetch(2),
                            known_urls=known)  # fmt: skip
    assert trace.searches[0].hits[0].known
    assert [p.hit.id for p in pages] == [2]
    assert trace.rejections == {"known_url": 1, "duplicate_url": 1}


def test_duplicate_invalid_and_known_queries_are_rejected(db) -> None:
    trace, _, _ = _loop(
        db,
        search("irsaliye excel takibi"),
        search("Excel İrsaliye takibi"),  # same words, other case / order
        search("irsaliye -excel takip"),  # operator
        search("gümrük evrakı forum"),
        search("depo sayım"),
        known_queries={"depo sayım"},
        budget=BUDGET.model_copy(update={"max_rejections_in_row": 3}),
    )
    assert [s.query for s in trace.searches] == ["irsaliye excel takibi", "gümrük evrakı forum"]
    assert trace.rejections == {"duplicate_query": 2, "invalid_query": 1}


def test_site_hint_survives_only_for_offered_domains(db) -> None:
    trace, _, _ = _loop(
        db,
        search("lojistik operasyon uzmanı", site="kariyer.net"),
        search("irsaliye takibi", "x.com"),
    )
    queries = [s.query for s in trace.searches]
    assert queries == ["lojistik operasyon uzmanı site:kariyer.net", "irsaliye takibi"]
    # FakeSearch ignores `site:`, so every hit of the hinted query is off-site.
    assert all(h.off_site for h in trace.searches[0].hits)
    assert not any(h.off_site for h in trace.searches[1].hits)


def test_budgets_stop_the_loop(db) -> None:
    trace, _, _ = _loop(
        db,
        search("a b"),
        search("c d"),
        search("e f"),
        search("g h"),  # searches exhausted
        budget=BUDGET.model_copy(update={"max_rejections_in_row": 5}),
    )
    assert len(trace.searches) == 3 and trace.rejections == {"no_searches_left": 1}
    words = ["irsaliye", "gümrük", "depo", "filo", "yakıt", "sevkiyat", "fatura", "stok", "rota"]
    many = BUDGET.model_copy(update={"max_searches": 20, "max_steps": 4})
    trace, _, _ = _loop(db, *(search(f"{w} takibi") for w in words), budget=many)
    assert trace.stop_reason == "max_steps" and trace.steps == 4
    tight = BUDGET.model_copy(update={"max_searches": 1, "max_fetches": 2})
    trace, _, _ = _loop(db, search("a b"), fetch(1), fetch(2), budget=tight)
    assert trace.stop_reason == "budget" and len(trace.pages) == 2


class SnippetSearch:
    name = "snippet-fake"

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        return [
            SearchHit(rank=1, url="https://www.linkedin.com/posts/a-1", title="Operasyon",
                      snippet="Sevkiyatları hâlâ Excel ile takip ediyoruz."),
        ]  # fmt: skip


def test_snippet_only_domains_are_read_from_the_snippet(db) -> None:
    trace, pages, _ = _loop(db, search("sevkiyat excel"), fetch(1), search_provider=SnippetSearch())
    (page,) = pages
    assert page.page is None and page.source == "snippet"
    assert page.text == "Operasyon\nSevkiyatları hâlâ Excel ile takip ediyoruz."
    assert trace.searches[0].hits[0].access == "snippet_only"


def test_a_rerun_replays_from_the_llm_cache(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    llm = FakeLLM({LoopAction: scripted(search("irsaliye takibi"), fetch(2))})
    ctx = make_context(db, run_id, FakeSearch(), llm)

    def go():
        return run_loop(ctx, stage="verify", prompt=load_prompt("verify_loop"), goal="g",
                        budget=BUDGET, allowed_domains=[], known_urls=set(), known_queries=set(),
                        on_page=lambda p: "ok")  # fmt: skip

    first = go()
    calls = len(llm.calls)
    again = go()
    assert len(llm.calls) == calls and again.llm_cache_hits == again.steps == first.steps


def test_long_histories_shorten_the_oldest_observations_first() -> None:
    obs = [_Observation("A" * 100, "a"), _Observation("B" * 100, "b"), _Observation("C" * 100, "c")]
    full = render_input("g", ["x.com"], obs, {"steps": 1}, max_chars=1000)
    assert "A" * 100 in full and "C" * 100 in full
    short = render_input("g", ["x.com"], obs, {"steps": 1}, max_chars=150)
    history = short.split("# History\n", 1)[1]
    assert history.startswith("a\n\nb\n\n" + "C" * 100)
    assert short.index("# Goal") < short.index("# History") < short.index("# Budget left")
