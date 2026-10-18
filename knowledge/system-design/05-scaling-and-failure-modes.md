# Scaling and failure modes: capacity estimation, stateless services, circuit breakers

## Capacity estimation
- Convert users to numbers: DAU, peak-to-average ratio (2-10x), requests per user per day,
  average payload size.
- QPS = DAU x requests-per-user / 86400, multiplied by the peak factor.
- Storage/year = writes per day x record size x 365 x replication factor.
- Size caches from the working set, not the whole dataset; bandwidth = QPS x payload size.
- State assumptions explicitly; a design review checks the math, not vanity numbers.

## Stateless services
- Services hold no session state; sessions live in signed tokens or a shared store so any
  instance can serve any request.
- Autoscale on the saturated resource (CPU, queue lag, open connections); warm pools for
  spiky traffic.
- Connection pools are sized to database limits, not to instance count times pool size.

## Failure modes
- Enumerate single points of failure and remove or mitigate them: redundancy, failover,
  tested restores.
- Bulkheads: one slow dependency must not exhaust the shared thread or connection pool.
- Circuit breakers open after repeated failures and fail fast; a half-open probe tests recovery.
- Timeouts everywhere, budgeted end to end; each caller timeout is shorter than its caller's.
- Graceful degradation: define what the system does when each dependency is down (serve
  stale, disable a feature, queue for later).
- Retries need backoff, jitter and budgets; retry storms turn a blip into an outage.

## Operations
- Liveness and readiness checks are distinct; deploys are rolling with connection draining.
- Load-test the failure paths, not only the happy path; run game days on the top three.
