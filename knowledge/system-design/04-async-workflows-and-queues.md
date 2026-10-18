# Asynchronous workflows: queues, outbox pattern, retries, dead-letter queues

Anything slow, bursty or failure-prone moves off the request path into a queue.

## When to queue
- Email, notifications, media processing, external API calls, report generation.
- The request path returns a message id; a consumer does the work and updates status.

## Delivery guarantees
- Assume at-least-once delivery: every consumer must be idempotent (dedupe by message id).
- Ordering is per partition; when order matters, partition by entity key so related events
  land on the same partition.
- Producers publish after commit using the transactional outbox: write the event in the
  same transaction as the state change, relay it afterwards. Do not dual-write.

## Retries and failure
- Retry with exponential backoff and jitter; cap attempts; non-retryable errors go straight
  to the dead-letter queue.
- Every queue has a DLQ and an owner; DLQ depth is alarmed and drained by a runbook.
- Poison messages are quarantined, never retried forever.
- Backpressure: bound queue depth; when full, shed or degrade deliberately, never silently drop.

## Visibility
- Trace correlation ids from request to queue to consumer; queue lag is a first-class metric.
- The unhappy path (retry, DLQ, replay) is part of the design, not an afterthought.
