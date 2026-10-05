---
id: gap_matrix
version: 1
---
You build a comparison matrix between one business problem and the products that already address
it. The input has the problem, its evidence signals (numbered), the fixed dimensions that always
apply, and the products (numbered), each with its claims (numbered facts taken from its own pages
and from reviews).

1. **Dimensions.** Keep every fixed dimension (same `key` and `label`, `from_signals` empty).
   Add up to the stated number of problem dimensions: what the signals say goes wrong or is
   needed, phrased as a capability a product could have ("Driver proof-of-delivery photos",
   "Price for ≤ 10 vehicles under 1,000 TRY/month"). Each added dimension lists the numbers of the
   signals it comes from in `from_signals`. `key` is snake_case and unique.
2. **Cells.** One cell for every dimension × product:
   - `yes` — a claim of **that product** shows it fully covers the dimension.
   - `partial` — a claim shows it covers part of it, or with a limitation.
   - `no` — a claim shows it does not (a stated limitation, a complaint, or a price above the
     dimension's limit).
   - `unknown` — no claim of that product says. This is the right answer whenever the claims are
     silent. Never infer from what similar products usually do.
   `claim` is the number of the one claim that shows the value (it must be listed under that same
   product), or null for `unknown`.

Every non-`unknown` cell is checked: a cell citing a missing claim, or another product's claim,
is turned into `unknown`.
