# Caching patterns: cache-aside, TTL, invalidation, stampede protection

Cache only what is expensive to compute or read and tolerable to serve slightly stale.

## Patterns
- Cache-aside: read through the cache, populate on miss, invalidate on write. Default choice.
- Write-through when writes are rare and reads must be immediately consistent with the cache.
- Write-behind only with a durable queue and replay semantics.
- Negative caching for repeated misses (404s, empty results) with a short TTL.

## Correctness
- Define the tolerated staleness window per key: what changes if the value is TTL seconds old?
- Invalidate explicitly on writes for correctness-critical reads; TTL alone is for tolerant data.
- Cache keys include tenant, identity and authorization scope; never serve per-user data
  under a shared key.
- Do not cache secrets, tokens or payment data outside the systems that own them.

## Protection
- Stampede: on miss, use single-flight or a lock so one caller recomputes while others wait
  or serve stale.
- Add jitter to TTLs to avoid synchronized expiry waves.
- Cache failure degrades to the origin; the origin must survive full miss-traffic (capacity
  check), otherwise declare the cache as required infrastructure with its own redundancy.
- Metrics: hit rate, eviction rate, origin fallback rate, hot-key skew.
