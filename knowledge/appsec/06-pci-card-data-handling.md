# Card Data Handling and PCI-DSS Scope

PCI-DSS requirements for any system that touches payment cards.

Non-negotiables:
- Primary Account Numbers (PAN), expiry and CVV must never be received, processed, stored or forwarded by your backend in plaintext. Card data goes directly from the client (or SDK) to the payment provider / vault; the backend receives only a provider-issued token or reference.
- CVV/CVC must never be stored under any circumstances, even encrypted.
- Tokenization or a full payment-provider delegation is the expected pattern; "encrypted before storage" in your own database still keeps you in PCI scope and is not the default recommendation.
- Card data is never logged, never in error traces, never in analytics events.
- If any card flow touches the system, key management (KMS/HSM), network segmentation, and quarterly scans apply; scope reduction is the goal.

Also required around payments: TLS everywhere, idempotency on charge creation, 3-D Secure where applicable, refunds restricted to owner/tenant, admin payment exports behind per-user auth with audit logging.

Finding patterns: backend receiving PAN/CVV, storing them in a database or backups, forwarding card data to internal services, shared admin export passwords.
