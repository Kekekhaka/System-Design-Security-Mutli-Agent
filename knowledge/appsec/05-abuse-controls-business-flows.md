# Abuse Controls for Sensitive Business Flows (API5/API6)

Broken Function Level Authorization and Unrestricted Access to Sensitive Business Flows.

Required design properties:
- Function-level authorization: admin or privileged functions are guarded by explicit role checks, not by obscurity of the route or by UI hiding. Deny by default.
- Sensitive flows (payments, bookings, refunds, invitation/credits, password reset, OTP) have anti-automation controls: per-user and per-device rate limits, CAPTCHAs or proof-of-work where appropriate, velocity checks.
- Idempotency keys are required on all state-changing payment and booking endpoints; retries must not create duplicate charges or bookings.
- Replay protection: signed requests are time-bounded and nonce-tracked; one-time tokens are single use.
- Business rule limits are enforced server-side: maximum quantity per order, purchase limits per account, staged rollouts for promotions.
- Fraud and anomaly signals (velocity, geo jumps, device fingerprints) feed a risk engine with defined actions.

Finding patterns: no idempotency on POST /payments, retries creating duplicates, promotional endpoints without velocity limits, "internal" routes reachable by any authenticated user.
