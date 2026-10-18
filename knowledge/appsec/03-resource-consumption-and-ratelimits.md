# Unrestricted Resource Consumption (API4)

Availability and cost abuse: endpoints that let a caller consume unbounded CPU, memory, storage, bandwidth or money.

Required design properties:
- Rate limiting at multiple layers (per IP, per user, per tenant, per endpoint); explicit quotas for expensive operations (search, export, report generation).
- Pagination is mandatory on list endpoints, with a server-enforced maximum page size; no unbounded limit or offset parameters.
- Request body size caps, upload limits, timeouts on all outbound and inbound calls.
- Queue depth limits, backpressure, and per-tenant concurrency caps; workers reject over-capacity submissions instead of buffering without bound.
- Full-table scans and regex-heavy queries are bounded; expensive queries have timeouts and indexes.
- Third-party API calls have budgets and circuit breakers.
- Autoscaling is not a substitute for explicit limits; cost ceilings per tenant where billing is pay-as-you-go.

Finding patterns: unbounded "limit" parameter, 5-minute server timeouts, unlimited queue submissions, no rate limiting on search/export.
