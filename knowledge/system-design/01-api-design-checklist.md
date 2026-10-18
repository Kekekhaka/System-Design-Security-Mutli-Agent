# API design checklist: endpoints first, pagination, idempotency, versioning

Design endpoints before infrastructure; the API surface drives storage and scaling choices.

## Endpoint conventions
- Model resources and sub-resources; use plural nouns, no verbs in paths.
- Use standard methods: GET (read), POST (create), PUT/PATCH (update), DELETE.
- Every endpoint declares its authentication and authorization requirement in the spec.
- Stable error taxonomy: 400 validation, 401 unauthenticated, 403 unauthorized, 404 absent,
  409 conflict, 429 throttled, 5xx transient; include a machine-readable error code and
  a correlation id in every error body.

## Pagination and filtering
- Prefer cursor/keyset pagination over offset for large or changing collections; the cursor
  encodes the last key, not a row number.
- Cap page size (default 20-50, max 100); never return unbounded lists.
- Filters are explicit query parameters with validated types; reject unknown parameters.

## Idempotency and retries
- All non-GET endpoints accept an idempotency key; the server stores the request hash and
  the response for replay.
- Client retries are safe: the same key returns the original response instead of
  duplicating side effects.
- Webhook consumers deduplicate by event id; at-least-once delivery is the default contract.

## Versioning and evolution
- Version in the path (/v1/) or via media type; never break a published contract.
- Add fields freely; remove or change types only in a new version.
- Deprecation policy: announce, dual-serve, then retire with a sunset date.
