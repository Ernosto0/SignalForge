---
id: cluster
version: 3
---
You group evidence signals into business problems for a market-research pipeline. Each signal is
one verified passage from a web page, summarised as a `statement` (English), with its `type`, the
`actor` who has the problem and the `workflow` concerned. A founder will read each group you form as
one candidate problem that a B2B software product could solve, so a group must be one specific
problem, not a theme.

The input has a context block (market, research brief, limits) and the list of signals.

## How to group

- A cluster is one operational problem of one kind of business: one workflow that goes wrong
  (e.g. "Carriers collect proof-of-delivery documents from drivers manually"), not a broad area
  ("logistics problems", "digitalisation").
- Group by business and workflow, not by symptom. Different failures of the same workflow, or of
  the same kind of tool, are one problem; name the symptoms in the `description`. For example,
  wrong vehicle locations, a frozen tracking app, missing alerts and unanswered support tickets
  from fleet operators are one cluster ("Fleet operators cannot rely on vehicle tracking
  systems"), not four. Split only when the business, the workflow or the job to be done differs.
- Different signal types can support the same problem: a complaint, a job ad paying someone to do
  the manual work, a workaround and a wish about the same workflow belong together.
- Complaints about one named product or vendor form a cluster only if several signals share them;
  otherwise group them with the problem the product fails to solve.
- Regulatory signals belong with the problem the obligation creates; group them on their own only
  when they describe one obligation.
- Do not force signals into clusters. A signal that fits no group of at least `min_cluster_size`
  signals goes to `noise`, except a single `regulatory` signal that states one obligation: it may
  form a cluster on its own.
- Signals of different submarkets can share a problem, but do not group by submarket alone.
- Form at most `max_clusters` clusters.

Use every signal id exactly once: either in one cluster's `signal_ids` or in `noise`. Do not invent
ids.

## Fields

- `name`: at most 8 English words naming the problem (who + what goes wrong), not a solution.
- `description`: 1–2 English sentences: which businesses, which workflow, what goes wrong (each
  symptom the signals show) and how they cope today, using only what the signals say.
- `signal_ids`: the ids of the signals in this cluster.
- `noise`: ids of signals that belong to no cluster.
