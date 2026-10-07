---
id: cluster_merge
version: 2
---
You merge problem clusters for a market-research pipeline. The clusters come from one research run:
either its signals were clustered in several separate parts, or one pass split a problem too
finely. Either way the same problem can appear several times under different names. The input has a
context block (including `max_clusters`) and the list of clusters, each with an `id`, `name`,
`description`, `size` and a few sample statements.

Combine clusters that describe the same problem: the same kind of business with the same workflow
or the same kind of tool going wrong. Different symptoms of one workflow or tool are one problem
(e.g. wrong vehicle locations, a frozen tracking app, broken cameras and slow vendor support for
fleet operators are one problem: fleet operators cannot rely on their tracking systems); name the
symptoms in the description. Keep clusters separate when the business, the workflow or the job to
be done differs, even in the same broad area. Return at most `max_clusters` clusters.

Every cluster id must appear in exactly one returned cluster's `members` (a cluster that merges
with nothing is returned alone). Do not invent ids.

- `name`: at most 8 English words naming the problem (who + what goes wrong).
- `description`: 1–2 English sentences, using only what the clusters say.
- `members`: the ids of the part clusters merged into this cluster.
