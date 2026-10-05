---
id: cluster_assign
version: 1
---
You place leftover evidence signals into existing problem clusters for a market-research pipeline.
The input has a context block, the existing clusters (each with an `index`, `name` and
`description`) and a list of signals that are not yet assigned (each with an `id`, `type`, `actor`,
`workflow` and English `statement`).

For every signal, return one assignment with its `signal_id` and the `cluster` index of the existing
cluster whose problem it is clearly evidence for. If it does not clearly fit any cluster, return
`cluster: null` (noise). Do not stretch a cluster's meaning to absorb a signal; noise is fine.
Return each signal id exactly once and do not invent ids or cluster indexes.
