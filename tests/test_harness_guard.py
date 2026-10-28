"""The critical-rule guard: catches explicit gaps, never a sound design."""

import pytest

from src.harness.guard import critical_rule_guard


@pytest.mark.parametrize(
    "text",
    [
        "Authentication is disabled for the MVP.",
        "The orders API: no authentication required.",
        "No authorization checks on /orders/{id}.",
        "Admin routes skip the authentication middleware.",
        "Any user can read any invoice.",
        "The service trusts the tenant id from the request header.",
        "Encryption at rest: TODO.",
        "TODO: add authorization to the export route.",
        # A public-sounding word is not a public route (round-3 review findings).
        "The admin API returns a status code; no authentication required.",
        "The token endpoint is internal; no authentication required.",
        "The admin API: no authentication required; it is a public endpoint.",
        "DELETE /users/{id}: no authentication required - token is optional.",
        "Internal status endpoint: no authentication required.",
        "GET /users/{id}/tokens - no authentication required.",
        "GET /healthcare/records - no authentication required.",
        "GET /users/{id}/token - no authentication required.",
        # A public path elsewhere on the line does not exempt another route (round 4).
        "DELETE /users/{id}: no authentication required - see /token for the flow.",
        "Admin export: no authentication required; mirrors /health response shape.",
        "The /token route and DELETE /users/{id}: no authentication required.",
        "No authentication required for GET /health or DELETE /users/{id}.",
        # A public route first does not exempt a gap stated on another route (round 5).
        "GET /health or DELETE /users/{id}: no authentication required.",
        "GET /health - the admin export at /admin/export has no authentication required.",
        "GET /health - the admin export has no authentication required.",
        "POST /token - no authentication required; returns a user session token.",
    ],
)
def test_guard_fires_on_explicit_gaps(text):
    assert critical_rule_guard(text, "")


@pytest.mark.parametrize(
    "text",
    [
        "GET /health - no authentication required (liveness probe).",
        "POST /auth/login - no authentication required; rate limited to 5/min.",
        "POST /webhooks/stripe - no authentication required beyond the Stripe signature.",
        "GET /docs and /openapi.json: no authentication required in development only.",
        "| GET /healthz | none | no authentication required |",
        "- `GET /api/v1/health` - no authentication required",
        "2. POST /auth/register - no authentication required; email is verified first.",
        "POST /oauth/token - no authentication required (client credentials in the body).",
        "GET /health, GET /readyz: no authentication required.",
        "Every route requires a bearer JWT. The TODO list for the security team: rotate keys.",
        "Passwords are hashed with argon2id; tenant id comes from the verified token.",
    ],
)
def test_guard_passes_sound_designs(text):
    assert critical_rule_guard(text, "") is None


def test_guard_reads_the_api_spec_too():
    assert critical_rule_guard("Sound architecture.", "DELETE /users/{id}: no authentication required")
