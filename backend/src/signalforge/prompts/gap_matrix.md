---
id: gap_matrix
version: 2
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

   The fixed dimensions ask what a small business needs: a published price it can afford
   (`price_for_smb`; a plan price for a small business shows `yes` or `no`, prices only on
   request show `partial`), the market's language (a `localization` claim shows `yes`),
   integration with e-documents (e-Fatura, e-İrsaliye, e-Arşiv), and a quick setup it can do
   itself (`setup_effort`; installation by the vendor's technicians or a long onboarding is
   `partial`).

Every non-`unknown` cell is checked: a cell citing a missing claim, or another product's claim,
is turned into `unknown`.
