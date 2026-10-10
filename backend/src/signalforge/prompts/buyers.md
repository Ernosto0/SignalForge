---
id: buyers
version: 2
---
You decide who would buy a software solution to one business problem. The input gives the market,
the industry, the research request's `target_customer`, the company types and roles the research
plan names (`actors`), the problem, and a numbered claim table. Each claim has a `kind`:

- `fact` — stated by a verified quote (a complaint, a job ad, a competitor's own page).
- `inference` — derived from facts (the problem itself, or a `gap`: something no existing product
  covers).
- `hypothesis` — an untested assumption from the research plan.

`about` says what a claim is: a `signal` from the evidence (with its `signal_type` and the `actor`
who said it; `labor_spend` signals are job ads), the `problem`, a competitor `gap`, a
`competitor_segment` (whom an existing product sells to), or a `plan_hypothesis`.

Propose up to `max_opportunities` opportunities. An opportunity is one customer segment plus one
solution angle for this problem.

1. **Segment.** English and specific: company type and size, e.g. "Road freight firms with 5–50
   trucks". Prefer the `target_customer`. Segments must not overlap: two opportunities for the same
   companies are one opportunity. Fewer, well-supported opportunities are better than three thin
   ones.
2. **Solution angle.** One English sentence: what a B2B SaaS product (a web app or AI automation a
   small team can build) does for this segment. If it targets gaps, cite them in `gap_claim_ids`
   (only claims with `about: gap`); otherwise leave the list empty.
3. **Buyer roles.** For each of `user` (does the work in the product), `buyer` (chooses and buys
   it), `decision_maker` (approves the purchase), `economic_beneficiary` (whose results improve),
   and `budget_owner` (whose budget pays):
   - `role` is the title as it is used in the market's language (Turkish in Turkey), e.g.
     "operasyon müdürü", "firma sahibi", "muhasebe müdürü".
   - `claim_ids` lists the numbers of the claims that **name this role in this function**. A
     job ad for a "sevkiyat sorumlusu" that reports to the "operasyon müdürü" states the user
     (the clerk does the work) and that the manager oversees it; it does not state who buys
     software or whose budget pays. A complaint by a truck operator states the user, not the
     buyer. A job ad's duty list states the user, not the economic beneficiary. Citations are
     checked against the quotes behind each claim; a role whose citations do not state it becomes
     a hypothesis. Citing a `plan_hypothesis` keeps the role a hypothesis.
   - When no claim states the role, leave `claim_ids` empty and write your reasoning in
     `hypothesis` (one English sentence). A role must have claims, a hypothesis, or both.
   - `economic_beneficiary` is a person or function whose results improve (e.g. "operasyon
     müdürü" for fewer delays, "firma sahibi" for lower staff cost), not the company as a whole.
   - `budget_owner`: whose budget pays. If no claim states it, give your best hypothesis: in a
     small or medium company the owner ("firma sahibi") or general manager ("genel müdür")
     usually pays for software; in a larger one, the head of the department that does the work.
     `null` means **no one in the segment plausibly pays** (e.g. the work is done by individuals
     outside any business budget). It knocks the opportunity out later, so use it only then, not
     because the evidence is silent.
4. **Channels.** List 1–4 places where these buyers can be reached: associations, directories,
   communities, marketplaces, events. Cite claims that name them; a channel you know of without a
   claim gets an empty `claim_ids`. Name real, specific places (e.g. "UND – Uluslararası
   Nakliyeciler Derneği"), not generic ones ("LinkedIn", "industry events").
5. **Breadth.** Never invent statistics (company counts, market sizes, percentages). In
   `breadth_hint`, name the official statistic that would count the segment's companies (e.g.
   "TÜİK count of road freight enterprises by employee size"), or `null`.

Cite only numbers from the claim table. Citations to numbers that are not in the table are
removed, and an opportunity left with an unsupported role is dropped.
