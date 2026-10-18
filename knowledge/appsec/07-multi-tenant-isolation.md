# Multi-Tenant Isolation

Isolation between customers ("tenants") is a security boundary, not a filtering preference.

Required design properties:
- tenant_id is derived server-side from the verified JWT/session, never from a client-supplied parameter or header.
- Every query on tenant-owned data is scoped by tenant_id as part of the query itself (row-level security or mandatory query builders). Shared tables are acceptable only with enforced scoping on every path, including aggregated and analytics queries.
- Object-level authorization is applied on top of tenant scoping for shared resources (explicit share grants, roles).
- Encryption keys are per-tenant (envelope encryption) so a data leak cannot decrypt across tenants; key rotation is defined.
- Caches, queues, search indexes, file/object storage buckets, logs and backups are all tenant-partitioned or keyed by tenant; cross-tenant cache key collisions are a classic leak.
- Admin and support tooling is itself multi-tenant aware with audited break-glass access.

Finding patterns: single shared encryption key for all tenants, queries filtered only by object id, tenant_id accepted from request payload, share features without owner checks.
