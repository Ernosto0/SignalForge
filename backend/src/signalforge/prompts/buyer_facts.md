---
id: buyer_facts
version: 1
---
You extract facts about the **buyers** of software for one business problem from one web page.
The input has the problem, the target customer, the page's URL and title, and the page text. Your
output is checked mechanically: every `quote` is searched for in the page text and facts whose
quote is not found are thrown away. Never paraphrase, translate, shorten or fix a quote.

Fact kinds (at most the number stated in the input; none is a fine answer):

- `buyer_role` — the page states that a named role, at a company like the target customer, does
  this work, chose or bought a tool for it, approves such purchases, or owns the budget for it.
  The statement names the role as written (e.g. "the general manager (genel müdür) of a customs
  brokerage chose the software"). A role that only appears in a job title, with no duty or
  decision about this work, is not a fact.
- `channel` — the page states that companies of the target customer's kind gather in a named,
  reachable place: an association or chamber and its members (with the member count if the
  quote states it), a member directory, a sector fair, a trade community or forum. The statement
  names the place.

Rules:

- `statement`: one English sentence saying only what the quote states. Never add numbers, names
  or claims that are not in the quote.
- `quote`: one or two consecutive sentences copied exactly from the page text.
- `translation`: an English translation of the quote if it is not in English, else null.
- Skip marketing claims about the product itself, generic lists of industries, and anything not
  about the target customer's kind of company.
