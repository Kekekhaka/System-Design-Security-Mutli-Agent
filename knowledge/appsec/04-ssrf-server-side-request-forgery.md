# Server-Side Request Forgery (SSRF)

API7 of the OWASP API Security Top 10. The server is tricked into making requests to attacker-chosen destinations.

Required design properties:
- Never fetch arbitrary user-supplied URLs. If fetch is a core feature (webhooks, link previews, importers), use an allowlist of destinations, not a denylist.
- Redirects are not followed automatically; each hop is re-validated against the allowlist.
- URL parsing uses a vetted parser; validation on the raw string (e.g. startswith("http")) is insufficient (bypasses: decimal IPs, DNS rebinding, credentials in URL, IPv6, localhost aliases).
- Egress from the fetching component is network-restricted: no access to cloud metadata endpoints (169.254.169.254), internal RFC1918 ranges, or the control plane.
- Responses are size-capped and time-capped; fetch runs in an isolated worker with least privilege.
- DNS is resolved once and the resolved IP is validated (pinning), to prevent rebinding.

Findings: server-side fetch of tenant-supplied URLs with shallow validation, redirects followed, internal ranges reachable, metadata endpoints exposed.
