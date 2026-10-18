# Broken Object Level Authorization (BOLA)

API1 of the OWASP API Security Top 10. The single most common API vulnerability.

Problem: endpoints accept an object identifier (document id, order id, user id) and act on it without verifying that the caller is allowed to access that specific object. Object identifiers are not secrets; attackers enumerate them.

Required design properties:
- Every endpoint that takes an object id performs an authorization check on (subject, action, object) before any read or write.
- Deny by default: missing check must fail closed, not open.
- Authorization is enforced server-side in the service layer, never only in the gateway, UI or filters.
- Responses for unauthorized access return generic errors (404/403) without leaking existence details.
- Batch/bulk endpoints apply the same per-object checks; wildcard or "list all" paths are explicitly scoped.

Review checklist: for each endpoint, name the check that binds caller to object. If no such check exists in the design, it is a finding.
