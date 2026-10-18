"""LLM-as-judge evaluation of the security agent against golden fixtures.

Runs the security node once per fixture with the built-in AppSec prompt (or a
persona prompt file, e.g. a Harness2 winner) and scores the result with a
judge model. Requires OPENROUTER_API_KEY; this is deliberately NOT part of the
offline pytest suite.

Usage:
    python scripts/eval_security_agent.py
    python scripts/eval_security_agent.py --output results.json --max-cases 3
    python scripts/eval_security_agent.py --prompt-file winner_persona.txt
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.nodes import is_approved, make_security_node
from src.agents.security.prompts import SECURITY_SYSTEM_PROMPT, with_review_emphasis
from src.core.config import settings
from src.core.llm import get_llm
from src.knowledge.retriever import load_default_retriever

FIXTURES = [
    {
        "id": "bola-documents",
        "expected": "reject",
        "planted_flaws": [
            "No object-level authorization: any authenticated user can read, overwrite or delete any document by id (BOLA).",
            "No tenant isolation: queries are filtered by doc_id only and all tenants share one encryption key.",
            "Full request bodies including document content are logged at INFO level.",
        ],
        "architecture": (
            "===ARCHITECTURE===\n"
            "DocVault is a multi-tenant document service. Documents live in one shared Postgres "
            "table (doc_id, tenant_id, owner_id, content); all tenants share a single encryption "
            "key. The API gateway validates JWTs and forwards requests to the documents service, "
            "which filters queries by doc_id only. The gateway logs full request bodies at INFO "
            "level, including document content. Deletion is synchronous and irreversible."
        ),
        "api_spec": (
            "===API SPEC===\n"
            "GET /documents/{doc_id}: returns document content for any valid JWT; no ownership or "
            "tenant check.\n"
            "PUT /documents/{doc_id}: overwrites content for any valid JWT.\n"
            "DELETE /documents/{doc_id}: deletes any document by id."
        ),
    },
    {
        "id": "payment-plaintext",
        "expected": "reject",
        "planted_flaws": [
            "Raw card data (card number, expiry, CVV) is received, stored in Postgres and forwarded by the backend; PCI-DSS requires tokenization or direct handling by the payment provider.",
            "Payment records are backed up nightly to S3 with default settings (no customer-managed encryption).",
            "Admin export is protected by a single shared password with no per-user authentication.",
            "No idempotency on payment creation; retries create duplicate charges.",
            "Any authenticated user can read any payment record by id.",
        ],
        "architecture": (
            "===ARCHITECTURE===\n"
            "Checkout API accepts payment requests and stores them in a payments table for "
            "reconciliation. The client posts the full card number, expiry and CVV; the API "
            "validates format, writes them to Postgres and asynchronously forwards them to the "
            "acquiring bank over HTTPS. The table is backed up nightly to an S3 bucket using "
            "default settings. A small admin dashboard (basic auth, one shared password) can "
            "export all payment records to CSV."
        ),
        "api_spec": (
            "===API SPEC===\n"
            "POST /payments {card_number, expiry, cvv, amount, currency}: stores and forwards card "
            "data. No idempotency key.\n"
            "GET /payments/{payment_id}: any authenticated user can fetch any payment record.\n"
            "POST /admin/export: dumps all payments to CSV behind the shared admin password."
        ),
    },
    {
        "id": "reset-token-leak",
        "expected": "reject",
        "planted_flaws": [
            "Password reset tokens are returned in the API response body, leaking them to anyone who can call POST /auth/reset.",
            "Reset tokens never expire and are 6-digit numeric codes, making them brute-forceable.",
            "No rate limiting or account lockout on reset confirmation or login.",
        ],
        "architecture": (
            "===ARCHITECTURE===\n"
            "Auth service issues JWTs. Password recovery: the client calls POST /auth/reset with an "
            "email; the service generates a reset token, stores it in Redis with no expiry, and "
            "returns it in the JSON response so the frontend can link directly to the reset page. "
            "The reset token is a 6-digit numeric code. Responses are identical for known and "
            "unknown emails. Account lockout is disabled to avoid support tickets; login JWTs "
            "expire after 7 days and there is no revocation list."
        ),
        "api_spec": (
            "===API SPEC===\n"
            "POST /auth/reset {email}: returns {reset_token} in the response body.\n"
            "POST /auth/reset/confirm {token, new_password}: resets the password; no attempt limit, "
            "no expiry.\n"
            "POST /auth/login {email, password}: issues a 7-day JWT."
        ),
    },
    {
        "id": "ssrf-webhooks",
        "expected": "reject",
        "planted_flaws": [
            "SSRF: the server fetches tenant-supplied URLs with only a startswith(http) check and follows redirects, so internal services and cloud metadata endpoints are reachable.",
            "Unrestricted resource consumption: unbounded search limit with full-table scans, unlimited worker queue submissions and a 5-minute server-side fetch timeout.",
        ],
        "architecture": (
            "===ARCHITECTURE===\n"
            "NotifyHub lets tenants register webhook URLs. On events, the service fetches the "
            "registered URL server-side and streams the response back to the tenant dashboard as a "
            "'test result', including status code and body. URLs are validated only by checking "
            "they start with 'http'; the fetcher follows redirects and has a 5-minute timeout. "
            "The search endpoint accepts an unbounded 'limit' parameter and runs a full-table "
            "scan; no rate limiting is applied and the worker queue accepts unlimited submissions."
        ),
        "api_spec": (
            "===API SPEC===\n"
            "POST /webhooks {url}: registers a URL; no allowlist.\n"
            "POST /webhooks/{id}/test: server fetches the URL and returns status and body.\n"
            "GET /search?q=&limit=: unbounded limit, full table scan."
        ),
    },
    {
        "id": "tokenized-payments-safe",
        "expected": "approve",
        "planted_flaws": [],
        "architecture": (
            "===ARCHITECTURE===\n"
            "Checkout API never sees card data: the client SDK tokenizes card details directly with "
            "the payment provider and the API receives only a provider-issued payment token. "
            "Payments are processed through the provider API with idempotency keys; the payments "
            "table stores token, amount, currency, status and provider reference only. All "
            "endpoints require JWTs with per-tenant scopes. Rate limiting is enforced per user and "
            "per IP. Logs are redacted; no PII or tokens in logs. Secrets live in a managed secrets "
            "service and rotate. Retries are idempotent."
        ),
        "api_spec": (
            "===API SPEC===\n"
            "POST /payments {payment_token, amount, currency, idempotency_key}: creates a payment; "
            "JWT + tenant scope; idempotency key required.\n"
            "GET /payments/{payment_id}: returns the payment owned by the caller's tenant; "
            "object-level authorization enforced.\n"
            "POST /refunds {payment_id, amount}: same scoping; audit-logged without sensitive data."
        ),
    },
    {
        "id": "scoped-docs-safe",
        "expected": "approve",
        "planted_flaws": [],
        "architecture": (
            "===ARCHITECTURE===\n"
            "DocVault multi-tenant document service. Every query is scoped by tenant_id taken from "
            "the validated JWT, and object-level checks verify owner or explicit share grants "
            "before any read or write. Each tenant's documents are encrypted with a per-tenant KMS "
            "key; keys are never shared across tenants. TLS everywhere; Postgres row-level security "
            "as a second layer. Audit logs record actor, action and doc_id only. Rate limiting per "
            "tenant; signed, short-lived download URLs (5 minutes)."
        ),
        "api_spec": (
            "===API SPEC===\n"
            "GET /documents/{doc_id}: 404 unless the caller's tenant owns it or has a share grant.\n"
            "PUT /documents/{doc_id}: owner or editor grant required.\n"
            "DELETE /documents/{doc_id}: owner only; soft delete with 30-day recovery.\n"
            "POST /documents/{doc_id}/share {grantee_email, role}: owner only; scoped grant."
        ),
    },
]

JUDGE_SYSTEM_PROMPT = """\
You are an impartial evaluator for a security review agent. You are given a system \
design, the expected review outcome, the planted flaws (if any) and the agent's actual \
findings. Use the design text as ground truth.

Return strict JSON with exactly these keys:
{
  "verdict_correct": true or false,
  "flaws_caught": [0-based indexes of planted flaws the agent clearly identified],
  "hallucinated_findings": ["short descriptions of findings that are not grounded in the design text or misstate it"],
  "notes": "one or two sentences"
}

Rules:
- A planted flaw counts as caught only if the agent identified the same problem; paraphrases count.
- If the design is safe, "flaws_caught" must be [].
- Justification text in an APPROVED report is not a finding.
- Output strict JSON only, no markdown fences and no extra text.
"""


def parse_json_object(text: str) -> dict | None:
    cleaned = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        start, end = cleaned.index("{"), cleaned.rindex("}")
        parsed = json.loads(cleaned[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_judge_prompt(fixture: dict, findings: str, verdict: str) -> str:
    if fixture["planted_flaws"]:
        flaws = "\n".join(
            f"{index}. {flaw}" for index, flaw in enumerate(fixture["planted_flaws"])
        )
        expected_line = "reject (CHANGES REQUESTED)"
    else:
        flaws = "(none - this design is safe)"
        expected_line = "approve (APPROVED)"
    return (
        "DESIGN UNDER REVIEW (architecture):\n"
        f"{fixture['architecture']}\n\n"
        "DESIGN UNDER REVIEW (API spec):\n"
        f"{fixture['api_spec']}\n\n"
        f"EXPECTED OUTCOME: {expected_line}\n"
        f"PLANTED FLAWS:\n{flaws}\n\n"
        f"AGENT VERDICT: {verdict}\n"
        "AGENT FINDINGS:\n"
        f"{findings}"
    )


def judge_case(judge_llm, fixture: dict, findings: str, verdict: str) -> tuple[dict | None, str]:
    response = judge_llm.invoke(
        [
            SystemMessage(content=JUDGE_SYSTEM_PROMPT),
            HumanMessage(content=build_judge_prompt(fixture, findings, verdict)),
        ]
    )
    return parse_json_object(response.content), response.content


def run_case(
    agent_llm, judge_llm, fixture: dict, system_prompt: str, retriever=None
) -> dict:
    node = make_security_node(agent_llm, system_prompt=system_prompt, retriever=retriever)
    state_out = node(
        {
            "architecture": fixture["architecture"],
            "api_spec": fixture["api_spec"],
            "revision_count": 0,
        }
    )
    findings = state_out["security_findings"]
    verdict = "APPROVED" if is_approved(findings) else "CHANGES REQUESTED"

    result = {
        "id": fixture["id"],
        "expected": fixture["expected"],
        "verdict": verdict,
        "verdict_correct": (verdict == "APPROVED") == (fixture["expected"] == "approve"),
        "findings": findings,
    }

    judged, raw = judge_case(judge_llm, fixture, findings, verdict)
    if judged is None:
        result["judge_error"] = raw[:500]
        return result

    planted = len(fixture["planted_flaws"])
    caught = [
        index
        for index in judged.get("flaws_caught") or []
        if isinstance(index, int)
        and not isinstance(index, bool)
        and 0 <= index < planted
    ]
    result.update(
        {
            "flaws_planted": planted,
            "flaws_caught": len(caught),
            "hallucinated_findings": [
                str(item) for item in judged.get("hallucinated_findings") or []
            ],
            "judge_notes": str(judged.get("notes") or ""),
        }
    )
    return result


def summarize(cases: list[dict]) -> dict:
    total = len(cases)
    judged = [case for case in cases if "judge_error" not in case]
    planted = sum(case["flaws_planted"] for case in judged)
    caught = sum(case["flaws_caught"] for case in judged)
    safe = [case for case in cases if case["expected"] == "approve"]
    safe_ok = sum(1 for case in safe if case["verdict"] == "APPROVED")
    return {
        "cases": total,
        "verdict_accuracy": round(
            sum(1 for case in cases if case["verdict_correct"]) / total, 3
        )
        if total
        else None,
        "flaw_detection_rate": round(caught / planted, 3) if planted else None,
        "cases_with_hallucinated_findings": sum(
            1 for case in judged if case["hallucinated_findings"]
        ),
        "judge_failures": total - len(judged),
        "safe_cases_approved": f"{safe_ok}/{len(safe)}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM-as-judge evaluation of the security agent against golden fixtures."
    )
    parser.add_argument("--output", default="eval_results.json")
    parser.add_argument(
        "--prompt-file",
        default=None,
        help="Security persona prompt file (as returned by /project/optimize). "
        "It is layered on top of the built-in AppSec prompt, as in the harness.",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Override the judge model (defaults to OPENROUTER_MODEL).",
    )
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    fixtures = FIXTURES[: args.max_cases] if args.max_cases else FIXTURES

    if args.prompt_file:
        persona = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        system_prompt = with_review_emphasis(persona)
        prompt_source = args.prompt_file
    else:
        system_prompt = SECURITY_SYSTEM_PROMPT
        prompt_source = "builtin"

    agent_llm = get_llm()
    judge_llm = get_llm(temperature=0.0, model=args.judge_model)
    retriever = load_default_retriever() if settings.SECURITY_RAG_ENABLED else None

    cases = [
        run_case(agent_llm, judge_llm, fixture, system_prompt, retriever)
        for fixture in fixtures
    ]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_model": settings.OPENROUTER_MODEL,
        "judge_model": args.judge_model or settings.OPENROUTER_MODEL,
        "prompt_source": prompt_source,
        "summary": summarize(cases),
        "cases": cases,
    }

    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Wrote {args.output}")
    for key, value in report["summary"].items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
