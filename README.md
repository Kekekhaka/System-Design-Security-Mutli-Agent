# Lekgotla

*Lekgotla* (Sesotho, Setswana and Sepedi) is a council of experts who meet to decide
something. This is the System Design + Security multi-agent: a team of nine experts that
designs a system, audits it for security, and, on request, builds, tests and reviews it.


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

Model: `deepseek/deepseek-v4.1-flash` via OpenRouter (1M context). Chosen by a benchmark of the real designer and audit stages: the most reliable and consistent of the DeepSeek Flash models tried (check OpenRouter for current prices).
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
follows) and one file per role in `roles/`. Nine experts, each written as a
senior professional with a definition of done and clear escalation points:

| Role | Does | Model |
|---|---|---|
| team lead | sizes and staffs the job, holds the gates, signs off | OpenRouter |
| business analyst | bounds a vague idea; requirements with IDs (pass 1); final docs (pass 2) | OpenRouter |
| designer | the house designer (a senior system architect); every run | OpenRouter |
| security engineer | audits every design, and the real code changes in build runs | OpenRouter |
| backend engineer, frontend engineer | implement the approved API spec | opencode, via OpenRouter |
| QA analyst | writes tests against the acceptance criteria | opencode, via OpenRouter |
| reviewer | reviews the real diff against the design and the contract | OpenRouter |
| DevOps engineer | writes the deploy plan when something deploys | OpenRouter |

They joined from the ProjectCosmos Claude Code agent team (which also had an
ideologist and a lead engineer; those duplicated the business analyst and the
team lead, so they were retired). They follow **this team's house standards**,
which sit at the top of `TEAM_RULES.md` and win wherever a role file differs:
English output; input treated as untrusted data; findings grounded in
evidence, never invented names; the Critical / High / Medium / Low severity
scale and the six finding classes; the APPROVED / CHANGES REQUESTED verdict
computed in code (any Critical or High blocks); the house design framework
with its `===ARCHITECTURE===` / `===API SPEC===` output; the API spec as the
contract; every loop up to `MAX_REVISIONS`; and the security baseline.

```python
from src.agents.team.roles import load_roles, role_prompt

roles = load_roles()             # dict[str, Role]
prompt = role_prompt("designer") # team rules + the designer role body
```

### Team pipeline

`src/agents/team/graph.py` runs the roles as a team, next to the original
supervisor graph (which is unchanged):

```
design (every run):  team lead plan -> business analyst* -> designer <-> security engineer
build (--build):     worktree -> backend engineer* -> frontend engineer* -> QA (writes tests)
                     -> test run -> reviewer -> security engineer (code audit) -> build gate
                        ^--------------- every finding, until all gates pass ---------'
close:               devops engineer* -> business analyst docs* -> team lead sign-off
                                                         * only when the team lead staffs them
```

- The **team lead** sizes the job (small / quick / full), staffs only the roles
  it needs and records why the others were skipped. Its JSON plan is checked in
  code: unknown roles are dropped, the designer and security engineer always
  run, a build always has an engineer, QA and the reviewer, and an unreadable
  plan falls back to the default quick plan.
- If the scope is unclear, the team lead's questions **stop the run** for the
  operator instead of being guessed. Rerun with the answers.
- The **business analyst** writes requirements with IDs (FR-nn, AC-nn), which
  the designer must trace its design to.
- Every loop follows the house rule: up to **`MAX_REVISIONS`** rounds, whatever
  the mode. After that the run **escalates** to the operator.
- The sign-off's verdict line (APPROVED / CHANGES REQUESTED) is **computed in
  code**; the model writes the rest.
- Every role leaves a handoff note, and routing decisions go into a decision log.

### Build runs

With `--build --repo <path>`, an APPROVED design is built:

- The engineers and QA are **opencode** runs (`OPENCODE_MODEL`, default
  `openrouter/deepseek/deepseek-v4.1-flash`) in a **git worktree** of the repository on
  a new branch `team/<run id>`. The operator's checkout is never touched, and
  untracked files such as `.env` are not in a worktree.
- Worktrees live under `TEAM_WORKTREE_ROOT` (default: `<system temp>/team-worktrees`),
  which must be **outside every git repository**; the run refuses to start
  otherwise. Nested inside a repository, opencode adopts that repository as its
  workspace (seen in testing: the engineer wrote into this project).
- **Isolation guard:** around every opencode run, the target repository and this
  project are checked with `git status`. A write outside the worktree stops the
  build at once and is reported; it never loops.
- Variables that look like secrets (keys, tokens, passwords, `OPENROUTER_*`,
  `LLM_*` and so on) are removed from the environment opencode and the tests run
  with, and the tests run without writing bytecode. The only key a build process gets is
  `OPENCODE_OPENROUTER_API_KEY`, and only the opencode process (see *OpenRouter only*).
  Tool caches never count as
  changes.
- Each opencode run's output is saved next to its brief (`briefs/*.log`).
- **The code audit is scoped to the diff.** For an existing system the design document
  can be older than the code, and an unscoped audit re-reports every known weakness on
  every change (a header layout change was once sent back for authentication). The
  audit is told to report only what the diff introduces or worsens. Keep the design
  documents you pass in current anyway.
- **A stalled model call does not end the run.** Every model stage is run again once
  after a timeout (and logged in the decisions). Team runs cap a single call at
  `TEAM_CALL_TIMEOUT_SECONDS` (600, instead of the shared 1200), so a stall costs at
  most one such wait. A second timeout still stops the run, with everything finished so
  far saved in the run folder.
- The pipeline runs `TEAM_TEST_COMMAND` in the worktree and records the real
  result; QA's opinion is not the gate.
- QA may only change test files: the pipeline compares file contents before and
  after QA's run, and any product file QA touched fails the build gate.
- The reviewer and the security engineer review the **real diff**, in the house
  finding format, with the verdict computed in code.
- The build gate passes only when the tests pass, QA stayed in scope, the review
  is APPROVED and the code audit is APPROVED. Otherwise every finding goes back
  to the engineers in one brief, up to `MAX_REVISIONS` rounds.
- **Nothing is committed, pushed or merged.** The script prints the branch and
  how to review or discard it.

```powershell
python scripts/run_team.py "Design a booking API for a small clinic."
python scripts/run_team.py --brief-file brief.md --answers "Single clinic; no payments."
python scripts/run_team.py --brief-file brief.md --build --repo C:/path/to/repo
```

Settings (in `.env`): `OPENCODE_BIN`, `OPENCODE_MODEL`, `OPENCODE_OPENROUTER_API_KEY`, `ENGINEER_TIMEOUT_SECONDS`
(default 1800), `TEAM_TEST_COMMAND` (default `python -m pytest -q`),
`TEAM_TEST_TIMEOUT_SECONDS` (900), `BUILD_DIFF_MAX_CHARS` (60000) and
`TEAM_WORKTREE_ROOT` and `TEAM_CALL_TIMEOUT_SECONDS` (600).

The script prints each step as it finishes and saves the artifacts after every
step, so a crash or timeout keeps the finished work. Artifacts go to
`runs/<timestamp>/` (gitignored). Exit codes: 0 approved, 1 could not run,
2 escalated, 3 the team lead needs answers.

### OpenRouter only

Every model call goes through OpenRouter: the designer, the auditors, the harness, the
export and the opencode build engineers alike.

- **The Python side** builds every model client in one function, `get_llm`, which refuses
  any base URL that is not `openrouter.ai` (look-alike hosts such as
  `openrouter.ai.example.com` do not pass). `ALLOW_NON_OPENROUTER=true` is the explicit
  opt-out, for example for a local model server.
- **Build runs** need `OPENCODE_MODEL` to be `openrouter/<vendor>/<model>`. opencode needs
  a key of its own, and the build agent runs commands in the worktree, so anything in its
  environment can be read by what it runs. Create a **dedicated OpenRouter key with a small
  credit limit** at openrouter.ai/settings/keys and put it in `.env` as
  `OPENCODE_OPENROUTER_API_KEY`. It goes to the opencode process only; git and the tests
  never see it, and your main `OPENROUTER_API_KEY` never reaches a build. It must differ
  from the main key. A build refuses to start if the model is not an OpenRouter model, the
  key is missing, or the key is the main one.
- The review and revision scripts record `provider_base_url` in their manifests.
- Other providers named inside a system you review (for example Azure OpenAI in the
  student-card app) are part of that system's description, not calls this project makes.

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
`approved`, `verified_against_brief`, `total_revisions` and `stop_reason`. For the
payment example you should see the security agent flag the PCI-DSS violation, the
designer switch to tokenization, and the second audit approve.

`approved` means the audit left no Critical or High findings. The audit also sees
the original brief, and a capability the brief explicitly requires but the design
lacks is a High "Requirement not met" finding. `verified_against_brief` is true
when the latest audit had the brief and listed no such finding. It is the auditor
model's judgement, not a test, so still read the design against your brief.

The loop runs until the audit approves or `MAX_REVISIONS` is reached, and it
also stops early when a revision leaves at least as many Critical + High
findings as the round before. That run ends unapproved with
`stop_reason: "no_progress"`, saving the remaining rounds. Set
`STOP_WHEN_NOT_IMPROVING=false` to always use every round.

## Deploying on a server

`docker compose up -d --build` runs the API as a non-root user, restarts both
services after a reboot, and publishes the API on the server's `127.0.0.1:8000`
only: reach it over SSH or Tailscale, never the public IP (Docker's published
ports bypass `ufw`). Start the server's `.env` from `.env.production.example`.
With `APP_ENV=production` the app refuses to start with dev login on,
`CORS_ORIGINS=*`, no database, or no `HARNESS_MAX_MODEL_CALLS` cap.

With dev login off, a service such as OpenClaw gets its own token:

```bash
docker compose exec api python scripts/mint_service_token.py --subject openclaw --out /app/data/openclaw.token
```

Its subject is `svc:openclaw` (a namespace users cannot take), it lasts up to
30 days, and its `token_id` is printed. Revoke one token by adding its id to
`REVOKED_TOKEN_IDS` and restarting; revoke all by rotating `JWT_SECRET`.

After every deploy:

```bash
python3 scripts/smoke_test.py --token "$(cat openclaw.token)"   # on the server
docker compose exec api python scripts/smoke_test.py --live     # model call + Postgres
```

The first checks health, that dev login is off, that requests without a valid
token get 401, that the service token is accepted (without starting a paid run),
and that nothing but SSH listens on a public address. Each line reads PASS,
SKIP or FAIL; the exit code is 1 if anything failed.

## Harness² (prompt optimization)

`POST /project/optimize` runs the Propose-Probe-Compose loop (`src/harness/optimizer.py`):
it asks the model for `HARNESS_CANDIDATES` (default 3) security reviewer personas,
probes them by running the full design + audit graph for each in parallel
(`asyncio.gather`), and returns the persona whose final design passed in the
fewest revisions. Personas are layered on top of `SECURITY_SYSTEM_PROMPT`
(they add emphasis, never remove the OWASP / MITRE / source->sink rules), and
every candidate's final design is re-audited by one fixed baseline auditor that
decides `approved`. A persona that approves weak designs therefore cannot win.
A failing probe is logged and skipped; the call fails (502) only if every probe fails.
Each user can have one optimization in flight (429 otherwise); with
`DATABASE_URL` set this is a Postgres advisory lock, so it holds across workers.
The endpoint applies the same budget as `scripts/run_harness.py`:
`HARNESS_REASONING_EFFORT` (default `none`), `HARNESS_MAX_REVISIONS`, and, when
`HARNESS_MAX_MODEL_CALLS` is set, a 403 before any model call if the worst case
(candidates x 2 attempts x (rounds x 4 + 3 judge calls) + 1) is above it. With
the defaults that worst case is 163 calls, so on a hosted server set
`HARNESS_MAX_REVISIONS=1` or `2` and a cap.

```bash
curl -X POST http://localhost:8000/project/optimize \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"requirements": "Design a simple payment processing API where users can pay for items using a credit card."}'
```

The response includes `winning_prompt`, the winning design (`final_architecture`,
`api_spec`, `security_audit`, `approved`, `total_revisions`) and a `probes` summary
comparing every candidate.

### Harness2 learns

`scripts/run_harness.py` runs one optimization offline and keeps what it learned
in a small persona library. Each run exploits the best persona, asks the model
for one challenger improved from that persona's recorded weaknesses, fills the
rest from the seeds, re-audits every final design with the fixed baseline
auditor, and ranks candidates by baseline approval, then severity-weighted
findings (Critical 10, High 5, Medium 2, Low 1), then revisions.

```powershell
python scripts/run_harness.py --brief "Design a booking API for a guest house."
python scripts/run_harness.py --brief-file brief.md --candidates 2 --max-revisions 2
python scripts/run_harness.py --brief-file brief.md --dry-run   # model calls, no run
python scripts/harness_report.py --trend 20
python scripts/harness_champion.py show
python scripts/harness_champion.py promote p_abc123 --yes --reason "3 clean wins"
python scripts/harness_champion.py rollback --yes
```

The store defaults to `data/harness/personas.json` (git-ignored) and the history
to `data/harness/history.jsonl`. Writes are atomic; a corrupt store is moved
aside as `personas.json.bad` and the seeds are used. History stores a keyed task
hash, never the task text. A candidate that fails is retried once, then recorded
and skipped; the run fails only if every candidate fails, and even then the
history is written.

Personas are validated before use: at most 1200 characters of plain text, no
`APPROVED` / `CHANGES REQUESTED` output-protocol words, and no instruction to
ignore or override the baseline rules. `with_review_emphasis` is unchanged.

A champion is promoted only by the operator, after at least three wins on two
different tasks and a golden-fixture check (run with the same logic as
`scripts/eval_security_agent.py`) showing no loss of planted-flaw recall and no
increase in hallucinated findings. Opt the team pipeline in with
`HARNESS_USE_CHAMPION=true`; its design and code audits then layer the champion's
emphasis on the baseline prompt and the team log records the persona id. With it
off, behaviour is exactly as before.

| Setting | Default | Meaning |
|---|---|---|
| `HARNESS_STORE_PATH` | `data/harness/personas.json` | Persona library path |
| `HARNESS_HISTORY_PATH` | sibling `history.jsonl` | Append-only run history |
| `HARNESS_MAX_REVISIONS` | `0` (use `MAX_REVISIONS`) | Per-candidate revision cap |
| `HARNESS_LIBRARY_CAP` | `12` | Active personas; the worst is retired above it |
| `HARNESS_REASONING_EFFORT` | `none` | Reasoning for harness runs |
| `HARNESS_USE_CHAMPION` | `false` | Use the champion in the team audits |
| `HARNESS_HMAC_KEY` | empty | HMAC key for store/history integrity |
| `HARNESS_MAX_MODEL_CALLS` | `0` (estimate + 2) | Hard per-run call budget |
| `HARNESS_SANITY_FIXTURES` | `1` | Baseline sanity checks before a run |

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


pgrep -af retry.sh || nohup bash retry.sh > retry.log 2>&1 &
tail -3 retry.log