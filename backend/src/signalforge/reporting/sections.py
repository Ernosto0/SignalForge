"""The 11 sections of a full opportunity report: title and the writer's guidance (the claim table
of each is built in ``tables.py``)."""

TITLES: dict[str, str] = {
    "problem": "The problem",
    "evidence": "Evidence",
    "who_has_it": "Who has it",
    "current_solutions": "Current solutions",
    "gaps": "Gaps",
    "buyers": "Buyers",
    "economics": "Economics",
    "risks": "Risks",
    "proposed_product": "Proposed product",
    "mvp": "MVP",
    "validation_experiment": "Validation experiment",
}

GUIDANCE: dict[str, str] = {
    "problem": "State the problem in 2–4 bullets: what goes wrong, for whom, how it shows up.",
    "evidence": "One bullet per strong piece of evidence (a signal fact): what the source says "
    "and who says it. Do not quote; the reader sees the excerpt next to the bullet.",
    "who_has_it": "Who feels the problem: company types, roles, how they work today.",
    "current_solutions": "What competitors and workarounds exist today, who they serve, and their "
    "prices where a claim states them. Name only competitors named in the claims.",
    "gaps": "Where current solutions fall short, using the gap claims and the competitor facts "
    "behind them.",
    "buyers": "Who uses, buys, decides and pays, and how to reach them. Roles that rest on a "
    "hypothesis claim must be hedged.",
    "economics": "What the problem costs a company and what it could pay: the assumptions, the "
    "value range, the price ceiling, competitor price anchors. Use the extra sources for the "
    "stored model's numbers. Label unsourced assumptions as assumptions.",
    "risks": "The main reasons this could fail: weak or hypothetical evidence, low factor levels, "
    "competition, founder-fit barriers. Hedge hypotheses.",
    "proposed_product": "Recommend what to build, as kind recommendation (uncited allowed). Tie it "
    "to the gap and the user's workflow.",
    "mvp": "Recommend the smallest first version, as kind recommendation (uncited allowed), "
    "within the founder's constraints in the extra sources.",
    "validation_experiment": "Recommend how to run the stored experiment (extra sources), as kind "
    "recommendation. Do not change its test or pass criterion.",
}
