# Secrets, Encryption and Safe Observability

Secrets management:
- Secrets live in a managed secrets service (Vault, AWS/GCP/Azure secret managers) with rotation; never in code, config files, environment dumps or CI logs.
- Least-privilege credentials per service; no shared admin passwords for dashboards or exports; per-user authentication for admin tooling.
- Service-to-service auth uses workload identity or mTLS, not static shared keys where avoidable.

Encryption and data protection:
- TLS 1.2+ in transit everywhere, including internal service-to-service traffic.
- Encryption at rest with customer-managed keys for sensitive data and backups; default provider settings are insufficient for PII/payment-adjacent data.
- PII minimized: collect only what the feature requires; define retention and deletion (right to erasure) paths.

Observability without leakage:
- Audit logs record actor, action, object id and timestamp; never tokens, credentials, full request bodies containing PII, or card data.
- Errors returned to clients are generic; stack traces and internals stay server-side.
- Log access itself is restricted and monitored; log retention follows policy.

Finding patterns: full request bodies logged at INFO, shared admin password, default-configured backups, secrets in config files.
