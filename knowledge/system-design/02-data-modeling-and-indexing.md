# Data modeling: access patterns first, indexes, partitioning, hot keys

Model the data for the queries the system actually runs, not for entity purity.

## Process
- List the access patterns (who reads and writes what, at what rate) before drawing tables.
- Choose relational storage when transactions and joins dominate; document or wide-column
  stores when access is key-based and scale is extreme; justify the choice explicitly.
- One writer owns each piece of state; make ownership explicit in the design.

## Indexes
- Index every query path behind a hot endpoint; composite index column order follows
  equality columns first, then range columns.
- Every index is paid for on writes; drop unused ones.
- Avoid unbounded scans; if a query needs one, paginate or precompute the result.

## Partitioning and hot keys
- Partition or shard by the dominant access dimension (tenant, user, time) and check that
  load spreads evenly across partitions.
- Append-heavy or time-series data: partition by time and archive cold partitions.
- Hot keys (a celebrity user, a viral product) get salted keys, per-key caches, or tiers.
- Rebalancing must be online; a shard split cannot require downtime.

## Consistency choices
- State which operations require strong consistency (money movement, inventory decrement,
  uniqueness) and which can be eventually consistent (feeds, analytics, counters).
- Counters and aggregates live in dedicated stores or are recomputed; never lock hot rows
  for statistics.
