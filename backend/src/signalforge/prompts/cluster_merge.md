---
id: cluster_merge
version: 1
---
You merge problem clusters for a market-research pipeline. The signals of one research run were
clustered in several separate parts, so the same problem can appear in more than one part under
slightly different names. The input has a context block (including `max_clusters`) and the list of
part clusters, each with an `id`, `name`, `description`, `size` and a few sample statements.

Combine part clusters that describe the same specific problem (same kind of business, same workflow,
same failure) into one cluster. Keep clusters about different problems separate, even if they are in
the same broad area. Return at most `max_clusters` clusters.

Every part-cluster id must appear in exactly one returned cluster's `members` (a cluster that merges
with nothing is returned alone). Do not invent ids.

- `name`: at most 8 English words naming the problem (who + what goes wrong).
- `description`: 1–2 English sentences, using only what the part clusters say.
- `members`: the ids of the part clusters merged into this cluster.
