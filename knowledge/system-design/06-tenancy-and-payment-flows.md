# Multi-tenant and payment flow patterns: isolation, ledgers, reconciliation

## Tenant isolation models
- Shared schema with tenant_id on every row: cheapest, requires mandatory scoping on every
  query; consider database row-level security as a backstop.
- Schema-per-tenant: stronger isolation, migration fan-out; a bridge option between extremes.
- Database-per-tenant: strongest isolation and per-tenant scaling; highest operational cost.
- The tenant boundary must carry through caches, queue topics, storage paths and search
  indexes, not only the primary database.

## Money movement
- Keep an append-only double-entry ledger: every transfer posts balanced debits and credits;
  balances are derived, never mutated in place with blind UPDATEs.
- Every money-moving endpoint takes an idempotency key; duplicate submissions must not
  double-charge.
- Amounts are integer minor units with an explicit currency on every amount.
- Card data never reaches the backend in plaintext; tokenize at a payment provider and
  store only tokens and last4.

## Reconciliation and operations
- Nightly reconciliation against the provider: compare settled amounts, flag mismatches,
  queue them for review.
- Refunds, chargebacks and partial captures are first-class flows, not edge cases bolted on
  later.
- Audit every state change with actor, timestamp and correlation id; money paths require
  immutable audit logs.
