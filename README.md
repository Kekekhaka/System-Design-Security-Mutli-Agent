# System Design + Security Multi-Agent

A LangGraph loop where a **Designer** agent produces a system design (following a
senior-engineer 6-step framework) and an **AppSec** agent audits it. Rejected
designs go back to the designer with the findings until approved or the revision
cap is reached.

```
START -> supervisor -> designer -> supervisor -> security -+-> (rejected) designer ...
                                        ^                  |
                                        +------------------+
                                        (approved or cap hit) -> FINISH
```

The security audit itself is a pipeline (`src/agents/security/graph.py`):
**scope** (trust boundaries, sensitive data flows, abuse cases) -> **analyze**
(persona-driven findings) -> **validate** (grounds and drops unverified findings,
then returns a structured `SecurityReport` via tool calling). The verdict is
computed in code from finding severities - any Critical or High finding means
CHANGES REQUESTED - so text in the design cannot talk the auditor into
approving, and a reply that is not a valid report fails closed. The report must
carry its findings list and a matching `finding_count`; a report whose list
disagrees with its own count or summary is rejected rather than approved. With
`SECURITY_RAG_ENABLED=true` (default), scope and analyze receive relevant control
guidance retrieved from the curated corpus in `knowledge/appsec/` - currently a
deterministic, dependency-free BM25 retriever (`src/knowledge/retriever.py`)
by default. Set `RAG_BACKEND=chroma` (after
`pip install -r requirements-chroma.txt`) to use ChromaDB embeddings instead;
both backends serve the same corpus through the same interface.

The designer is grounded at design time too: with `DESIGN_RAG_ENABLED=true`
(default) it receives relevant architecture patterns from
`knowledge/system-design/` (API design, caching, queues, scaling, tenancy and
payment flows) via the same retriever, so designs start from a house playbook
instead of from scratch.

Model: `deepseek/deepseek-v4-flash` via OpenRouter (1M context, ~$0.09/M in, ~$0.18/M out).
Set `OPENROUTER_FALLBACK_MODEL` to another OpenRouter model id for automatic
failover on rate limits or provider outages; startup logs a warning while it is
unset. `LLM_CALL_TIMEOUT_SECONDS` (default 1200) is a hard deadline on every model call: a
provider that keeps a connection alive can stall a request indefinitely (a call was
seen pending for six hours), so a call that overruns fails with `ModelCallTimeout`
instead of hanging, and is not retried. `LLM_MAX_TOKENS` (default 32000) is the per-call output budget,
reasoning included: DeepSeek V4 Flash can reason through a small budget and
return no text. Any stage that returns empty or truncated output raises
`ModelOutputError` instead of being treated as "no findings" or a blank design.

## Team roles

The role prompts live in `src/agents/team/`: `TEAM_RULES.md` (rules every role
follows) and one markdown file per role in `roles/` (team lead, ideologist,
business analyst, architect, security engineer, DevOps engineer, lead engineer,
backend engineer, frontend engineer, reviewer, QA analyst). They joined from
the ProjectCosmos Claude Code agent team, so they follow **this team's house
standards**, which sit at the top of `TEAM_RULES.md` and win wherever a role
file differs: English output; input treated as untrusted data; findings
grounded in evidence, never invented names; the Critical / High / Medium / Low
severity scale and the six finding classes; the APPROVED / CHANGES REQUESTED
verdict computed in code (any Critical or High blocks); the house design
framework with its `===ARCHITECTURE===` / `===API SPEC===` output; the API spec
as the contract; the revision loop up to `MAX_REVISIONS`; and the security
baseline. Each file's front matter gives the pipeline `stage`, the `artifact`
it produces and `used_when` to staff it.

Load them with `src.agents.team.roles`:

```python
from src.agents.team.roles import load_roles, role_prompt

roles = load_roles()              # dict[str, Role]
prompt = role_prompt("architect") # team rules + the architect role body
```

### Team pipeline

`src/agents/team/graph.py` runs the roles as a team, next to the original
supervisor graph (which is unchanged):

```
team lead (plan) -> ideologist* -> business analyst* -> architect <-> security engineer -> team lead (sign-off)
                                                         * only when the team lead staffs them
```

- The **team lead** sizes the job (small / quick / full), staffs only the roles
  it needs and records why the others were skipped. Its JSON plan is checked in
  code: unknown roles are dropped, the architect and security engineer always
  run, and an unreadable plan falls back to the default quick plan.
- If the scope is unclear, the team lead's questions **stop the run** for the
  operator instead of being guessed. Rerun with the answers.
- The **business analyst** writes requirements with IDs (FR-nn, AC-nn), which
  the architect must trace its design to.
- The revision loop follows the house rule: up to **`MAX_REVISIONS`** security
  rounds, whatever the mode. After that the run **escalates** to the operator.
- The sign-off's verdict line (APPROVED / CHANGES REQUESTED) is **computed in
  code**; the model writes the rest.
- Every role leaves a handoff note, and routing decisions go into a decision log.

```powershell
python scripts/run_team.py "Design a booking API for a small clinic."
python scripts/run_team.py --brief-file brief.md --answers "Single clinic; no payments."
```

Artifacts go to `runs/<timestamp>/` (gitignored). Exit codes: 0 approved,
1 could not run, 2 escalated, 3 the team lead needs answers.

Not built yet (phase 3): an engineer role that writes code with opencode in an
isolated branch, a security review of the real diff, and QA that runs the tests.

## Setup

```powershell
python -m pip install -r requirements.txt
copy .env.example .env    # then put your OPENROUTER_API_KEY in .env
```

In `.env`, also set `JWT_SECRET` to 32+ random characters - the app refuses to
start without one (or with the example placeholder):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

For local testing with the `/auth/token` helper, set `ENABLE_DEV_AUTH=true`. It
is off by default because it mints a token for any username without a password.

## Run

```powershell
python -m pytest              # offline tests - no API key needed
python scripts/demo.py        # run one design cycle from the CLI
uvicorn src.main:app --reload # start the API
docker compose up --build     # API + Postgres together (reads .env if present)
```

## API

```bash
# 1. Mint a dev token (requires ENABLE_DEV_AUTH=true; off by default)
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "keke"}'

# 2. Trigger the design + audit loop
curl -X POST http://localhost:8000/project/design \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "requirements": "Design a simple payment processing API where users can pay for items using a credit card.",
    "session_id": "test-1"
  }'

# 3. Optional: watch the loop live over SSE (node, token and final events)
curl -N -X POST http://localhost:8000/project/design/stream \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"requirements": "Design a payment API.", "session_id": "test-1"}'
```

`session_id` must be 1-64 characters of letters, digits, `_` or `-`, and
`requirements` is capped at 20,000 characters. Each user's sessions are isolated
by the thread id `proj-{user_id}:{session_id}`.

The response includes `final_architecture`, `api_spec`, `security_audit`,
`approved` and `total_revisions`. For the payment example you should see the
security agent flag the PCI-DSS violation, the designer switch to tokenization,
and the second audit approve.

## Harness² (prompt optimization)

`POST /project/optimize` runs the Propose-Probe-Compose loop (`src/harness/optimizer.py`):
it asks the model for `HARNESS_CANDIDATES` (default 3) security reviewer personas,
probes them by running the full design + audit graph for each in parallel
(`asyncio.gather`), and returns the persona whose final design passed in the
fewest revisions. Personas are layered on top of `SECURITY_SYSTEM_PROMPT`
(they add emphasis, never remove the OWASP / MITRE / source->sink rules), and
every candidate's final design is re-audited by one fixed baseline auditor that
decides `approved`. A persona that approves weak designs therefore cannot win.
A failing probe is logged and skipped; the call fails only if every probe fails.
Each user can have one optimization in flight (429 otherwise); with
`DATABASE_URL` set this is a Postgres advisory lock, so it holds across workers.

```bash
curl -X POST http://localhost:8000/project/optimize \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"requirements": "Design a simple payment processing API where users can pay for items using a credit card."}'
```

The response includes `winning_prompt`, the winning design (`final_architecture`,
`api_spec`, `security_audit`, `approved`, `total_revisions`) and a `probes` summary
comparing every candidate.

## Evaluation

`scripts/eval_security_agent.py` is an LLM-as-judge evaluation of the security
agent against six golden fixtures (4 designs with planted flaws, 2 provably
sound designs). It runs the security node once per fixture, then asks a judge
model to score flaw detection, false accusations and verdict correctness, and
writes a JSON report (default `eval_results.json`).

```powershell
python scripts/eval_security_agent.py                 # built-in AppSec prompt
python scripts/eval_security_agent.py --max-cases 3   # cheap smoke run
python scripts/eval_security_agent.py --prompt-file winner_persona.txt
```

`--prompt-file` accepts a persona prompt as returned by `/project/optimize`
(layered on top of the built-in AppSec prompt, as in the harness), so you can compare
candidate personas or track regressions between prompt revisions. This makes
real API calls and is intentionally not part of the offline pytest suite; the
report JSON is diffable for CI or dashboards.

## Export (design to OpenAPI + SQL)

The design loop produces documents, not code. `scripts/export_design.py` turns a
finished, approved design into machine-readable specs. The input is a JSON file
holding the `final` event of `/project/design/stream` (keys `approved`,
`final_architecture`, `api_spec`, `security_audit`, `total_revisions`), saved as
`result.json` or any name you pass:

```powershell
python scripts/export_design.py result.json                    # writes export/result/
python scripts/export_design.py result.json --out export/guest-house
```

It writes `openapi.yaml`, `schema.sql`, the three design documents (`design.md`,
`api-spec.md`, `security-audit.md`) and a `manifest.json`. Two LLM calls run in
parallel: a few cents, and a large API can take several minutes. If it fails with a
truncation error, raise `EXPORT_MAX_TOKENS` (default 64000). The export runs with
reasoning off (`EXPORT_REASONING_EFFORT=none`): measured on DeepSeek V4 Flash, `none`
used 0 reasoning tokens while `low` and `minimal` used more than the default, and
with reasoning on the export ran out of tokens before writing anything. A design the security audit did not approve
is refused unless you pass `--allow-unapproved`. `export/` is gitignored.

The output is generated from the design text, not guaranteed. The script checks the
OpenAPI document's structure (valid YAML, version, every path has operations with
responses, every `$ref` resolves, every operation has a security requirement or an
explicit `security: []`) and the SQL's structure (a `CREATE TABLE` exists, parentheses
balance, no `DROP` / `TRUNCATE` / `DELETE`). With `pip install sqlglot` the SQL is also
parsed, which catches most syntax errors but nothing semantic. Anything flagged is
listed in `manifest.json`, printed, and gives exit code 2. Neither file has been
executed: apply `schema.sql` to a scratch Postgres and validate `openapi.yaml` with an
OpenAPI validator before relying on them. The security audit covers the design, not
this output.

## Reviewing an existing system

Two scripts point the agents at a system that already exists. Both read a folder holding
`architecture.md` and `api-spec.md` (what the system does today, written factually from
its README and code) and send them to the configured model provider, so only use material
you are allowed to share.

```powershell
# 1. Security review: scope -> analyze -> validate. Writes security-report.md, scope.md,
#    draft-findings.md and a manifest into the output folder.
python scripts/review_system.py reviews/my-system

# 2. Revision: the designer + auditor loop applies a change brief (what to adopt, keep and
#    defer) to the existing design and re-audits it. Produces a revised DESIGN, not code.
python scripts/revise_system.py reviews/my-system --brief reviews/my-system/change-brief.md
```

What a test on a real project (a Django student-card system) showed:

- **The audit is only as accurate as its description.** A vague sentence produced a false
  High finding; missing deployment facts hid real ones (root container, one worker).
- **One run is not enough.** Repeating the same input found about half the same topics.
  Run it two or three times and merge the findings.
- **Design level only.** It cannot see code bugs, and it takes the document at its word.
- **Fast mode (`LLM_REASONING_EFFORT=none`) suits a single audit pass** (about 75 s instead
  of about 5 min) but its severities are inflated and it finds fewer issues. Do not use it
  for the revision loop: the designer then over-engineers and the loop fails to converge.
- **Read the revised design, not just the verdict.** An approved revision once turned out to
  be a patch with invented field names. The revision rules now demand a complete spec and
  exact names from the existing description.
- Nothing is built or run. The audit covers the design text only.

## Notes

- Security rules live in `src/agents/security/prompts.py`. The analyze persona
  already carries the v2.0 review discipline (evidence-first, finding
  classification taxonomy, source->sink tracing, CWE/CVSS and MITRE ATT&CK
  grounding). To adopt your full v2.0 skill text, run
  `scripts/eval_security_agent.py --prompt-file <file>` first and compare
  detection against the current persona before changing the default.
- The designer clears `security_findings` after consuming the feedback; the
  supervisor only routes. This is what makes the loop self-correcting.
- The RAG corpora are plain markdown: `knowledge/appsec/*.md` for the audit
  pipeline and `knowledge/system-design/*.md` for the designer. Add or edit
  entries without code changes; `KNOWLEDGE_DIR` / `DESIGN_KNOWLEDGE_DIR`
  override the locations, `SECURITY_RAG_*` / `DESIGN_RAG_*` toggle them.
  Retrieval runs offline with BM25 by default; `RAG_BACKEND=chroma` switches to
  ChromaDB (persisted in `CHROMA_DIR`, default `.chroma/`, re-synced from the
  markdown on startup; its embedding model downloads on first use). Behind a
  TLS-inspecting corporate proxy that download can fail: point `SSL_CERT_FILE`
  at a bundle containing your corporate root CA, or pass your own
  `embedding_function` to `ChromaRetriever`.
- Session threads are kept in memory by default. Set `DATABASE_URL` and the
  startup lifespan swaps in an `AsyncPostgresSaver` (tables are created via
  `setup()`); sessions then survive restarts. Without it, startup logs a
  warning; set `REQUIRE_DATABASE=true` (docker compose does) to make a missing
  `DATABASE_URL` a startup error instead. On Windows, psycopg's async driver
  cannot use uvicorn's default event loop; with `DATABASE_URL` set, start with
  `uvicorn src.main:app --loop asyncio:SelectorEventLoop` (startup says so if
  you forget). Docker/Linux needs nothing extra.
- `APP_ENV=production` refuses to start while `ENABLE_DEV_AUTH` is on,
  `CORS_ORIGINS` is `*`, or `DATABASE_URL` is missing.
- Set `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` to trace every design,
  audit and harness invocation (Langfuse LangChain callbacks).
- `CORS_ORIGINS` (comma-separated) controls which browser origins may call the
  API; default `*`. Lock it down in prod.
- `ENABLE_DEV_AUTH` (default false) enables the passwordless token-minting
  endpoint for local testing. Never enable it in prod; startup logs a warning
  when it is on.
- This folder sits inside OneDrive: `.env` (with your API key) syncs to the
  cloud. Move the project to a plain local folder (e.g. `C:\dev`) if that is a
  concern. `.env` is gitignored.
