# Broken Authentication (API2)

Issues with identity verification and credential handling.

Required design properties:
- Password reset tokens: high-entropy (>= 128 bits), single use, short expiry (minutes), delivered only through a verified out-of-band channel (email link), never returned in an API response body.
- Login and reset endpoints are rate limited and apply lockout / exponential backoff after repeated failures per account and per IP.
- Credential stuffing: monitor and block by device/IP reputation; identical responses for known and unknown accounts to avoid user enumeration.
- Sessions/JWTs: short-lived access tokens, refresh rotation, revocation list or denylist, no long-lived tokens (multi-day) for sensitive operations.
- Multi-factor authentication available for admin and high-risk flows.
- Credentials never logged; authorization headers redacted.

Finding patterns: reset token in response body, no expiry, numeric/short tokens, no rate limiting, no revocation, shared admin passwords, disabled lockout "for support reasons".
