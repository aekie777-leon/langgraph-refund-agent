# LangGraph Refund Agent

[![CI](https://github.com/aekie777-leon/langgraph-refund-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/aekie777-leon/langgraph-refund-agent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2F855A.svg)](LICENSE)

A production-oriented, multi-tenant customer-service agent built with
LangGraph, FastAPI, PostgreSQL, React, and transactional Outbox/Inbox workers.
It keeps policy decisions deterministic, requires human confirmation before
state-changing actions, and makes identity, tenant, retry, and audit boundaries
explicit.

Version: `1.0.0`

> **Portfolio scope:** this repository demonstrates production engineering
> practices with synthetic local data. It is not deployed to production and
> makes no production-readiness claim for the included model, safety policy, or
> local infrastructure.

![OpsPilot local showcase console](docs/assets/showcase-console.png)

## Review the project in five minutes

1. Start the no-cloud-key [local showcase](docs/v1.0_showcase.md) and run an
   actual Graph interrupt/resume flow.
2. Inspect the [versioned AI evaluation](docs/v1.0_evaluation.md): 57 English
   and Chinese scenarios with deterministic safety gates and committed JSON
   evidence.
3. Trigger the [fault demo](docs/v1.0_observability.md): Provider HTTP 500,
   persisted retry, successful delivery, signed webhook, and Inbox completion.
4. Use the [v1.0 release map](docs/v1.0_release.md) to trace architecture,
   security decisions, test evidence, and intentionally deferred production
   work.

## Engineering evidence

| Area | Evidence in this repository |
|---|---|
| Agent design | LangGraph state machine, structured model outputs, deterministic policy nodes, human-in-the-loop interrupts |
| Backend | Async FastAPI boundaries, strict Pydantic contracts, PostgreSQL repositories, idempotency and optimistic concurrency |
| Distributed reliability | Transactional Outbox/Inbox, leases and fencing, bounded retries, signed callbacks, replay-safe processing and redrive |
| Identity and security | OIDC/JWT verification, JWKS rotation, server-derived RBAC, tenant isolation, read-only SCIM assignment checks |
| AI safety and evaluation | Bilingual versioned dataset, real routing-function evaluation, negative boundaries, reproducible CI artifact |
| Product demonstration | React/TypeScript console, Docker Compose stack, persona-scoped case and Provider operations views |
| Quality | Python 3.11/3.12 CI with real PostgreSQL, Ruff, mypy, pytest, ESLint, Vitest, Playwright, package and container checks |

## Architecture

```mermaid
flowchart LR
    UI[React showcase console] -->|Bearer token| API[LangGraph Server + FastAPI]
    API --> AUTH[OIDC/JWKS runtime\nRBAC + tenant scope]
    API --> GRAPH[LangGraph workflow\nstructured AI + deterministic policy]
    GRAPH -->|interrupt / resume| UI
    GRAPH --> DB[(PostgreSQL\ndomain + audit + queues)]
    DB --> OUT[Outbox worker]
    OUT -->|signed command| PROVIDER[Provider adapter\nlocal simulator in showcase]
    PROVIDER -->|HMAC callback| API
    API --> DB
    DB --> IN[Inbox worker]
    IN --> DB
    API --> OPS[Case + Provider Ops APIs]
```

The local portfolio profile runs every component above with deterministic
model-facing adapters, synthetic identities, and loopback-only ports. The
production profile instead fails closed unless OIDC, SCIM, TLS PostgreSQL, and
secure Provider transport requirements are satisfied.

## What's new in v1.0.0

- Adds a self-contained React/TypeScript operations console backed by the real
  LangGraph workflow, PostgreSQL repositories, scoped internal APIs, and two
  fenced workers
- Provides prepared recruiter-friendly evidence paths for refunds, manual
  review, complaints, assignment, Provider lifecycle, and risk-aware handoff
- Adds 57 versioned bilingual AI and safety scenarios, deterministic release
  gates, and reproducible Markdown/JSON evidence verified in CI
- Demonstrates a real transient Provider failure through persisted
  `retry_scheduled`, `accepted`, and `processed` attempt history without
  exposing payloads or credentials
- Adds frontend lint, unit-test, and production-build CI while preserving the
  existing Python 3.11/3.12 and real PostgreSQL matrix
- Preserves migrations `0001`–`0008`, the Graph workflow, Provider wire
  contract, v0.9 identity boundary, and all existing domain APIs

## What's new in v0.9.0

- Replaces production demo authentication with one vendor-neutral async OIDC
  JWT runtime shared by FastAPI and LangGraph Server
- Verifies fixed issuer, audience, asymmetric algorithm, signature, time
  claims, and `kid`; JWKS caching has bounded refresh and fail-closed outage
  behavior
- Maps stable tenant/user claims and allowlisted groups into `AccessScope`;
  permissions remain server-derived RBAC and token permissions are rejected
- Adds a read-only SCIM 2.0 directory boundary so assignment accepts only an
  active same-tenant support agent or supervisor without identity enumeration
- Makes production startup fail closed on demo auth, Studio bypass, missing or
  unreachable OIDC/SCIM, plaintext Provider HTTP, or local/plaintext PostgreSQL
- Defines the external TLS PostgreSQL, gateway, readiness, rollback, privacy,
  and local-only Compose contract without deploying or storing production data
- Preserves the Graph workflow, Provider wire schema, migrations 0001-0008,
  canonical actor format, v0.8 data, and explicit local demo mode

## What's new in v0.8.0

- Adds a tenant-scoped Provider operations control plane with five strict
  internal FastAPI routes for safe queue inspection and manual redrive
- Requires the exact `supervisor` role plus explicit `provider_ops:read` or
  `provider_ops:redrive` permission at both Service and PostgreSQL boundaries
- Coordinates idempotent, concurrent-safe Outbox and Inbox recovery cycles
  with immutable actor/reason audit history and no synchronous Provider calls
- Redrives only terminal technical Outbox failures; Provider business
  rejections remain terminal and cannot be resent
- Reprocesses only failed, unleased Inbox messages through the ordinary Worker
  association and fencing path without changing callback payload/hash data
- Adds additive migration `0008`, real PostgreSQL HTTP-to-Worker E2E coverage,
  and strict response/OpenAPI field-whitelist audits

## What's new in v0.7.0

- Adds a versioned Provider command contract with canonical envelopes,
  deterministic signing, connection resolution, retry classification, and
  strict tenant and aggregate associations
- Persists transactional Outbox and Inbox messages, processing attempts, lease
  ownership, retry schedules, and immutable audit events in PostgreSQL
- Dispatches outbound Provider commands asynchronously through a standalone
  Outbox worker with rate limits, lease renewal, recovery, and safe failure
  coordination
- Receives authenticated HMAC callbacks through a size-limited FastAPI webhook
  boundary with replay-safe Inbox persistence and conflict detection
- Applies callbacks atomically through fenced Order Operation and Support Case
  finalizers, including duplicate, stale, retry, exhaustion, rollback, and
  concurrency behavior
- Runs separate Outbox and Inbox worker services in Docker Compose, keeping
  Provider credentials isolated from the inbound worker and LangGraph workflow

## What's new in v0.6.0

- Adds a trusted identity boundary: every request is authenticated through a
  swappable `IdentityProvider` (a deterministic demo provider for local
  development) before any resource is touched
- Restricts LangGraph threads, runs, and assistants through official
  authentication and authorization handlers; thread ownership is stamped and
  enforced with fail-closed filters
- Adds `customer_id` / `tenant_id` / `created_by` ownership to refunds,
  support cases, case events, order operations, and operation events
  (additive migration `0005`); legacy rows are quarantined in a reserved
  `legacy` tenant and are not visible to any live identity
- Enforces `AccessScope` in every repository read and write; single-resource
  lookups return 404 for both missing and unauthorized records (no
  enumeration oracle)
- Verifies order ownership for inquiries, refunds, cancellations, returns,
  exchanges, and delivery investigations; both order data sources
  (`orders.json` and `DemoOrderProvider`) share the same ownership rules
  and consistent demo ownership
- Protects the internal support-case API: `401` without credentials, `403`
  for permission violations, `404` for inaccessible resources
- Adds role-based access control for `customer`, `support_agent`, and
  `supervisor`, including a supervisor-only case assignment endpoint with an
  immutable `assigned` audit event (migration `0006`)
- Never stores raw tokens or bearer credentials in graph state, PostgreSQL,
  logs, or traces

## What's new in v0.5.1

- Keeps Graph-construction tests fully isolated from model credentials by using an operation-extraction fake
- Aligns the PostgreSQL automatic-operation integration test with the v0.5 Provider submission boundary
- Verifies the same CI suite on Python 3.11 and Python 3.12 without changing customer-facing behavior

## What's new in v0.5.0

- Adds intents for order cancellation, returns, exchanges, delivery issues, and current-thread support-case status
- Uses a dedicated order-operation subgraph with an explicit confirmation interrupt before every operation
- Persists eligible operations with an idempotency key, then sends automatic operations through a provider boundary
- Creates or updates a support case when an operation requires manual review or a delivery investigation
- Includes a deterministic in-memory demo provider for local development and Graph integration tests

## What's new in v0.4.0

- Deterministic handoff policy with explicit case types, priorities, and reason codes
- PostgreSQL support-case and immutable event persistence with idempotency and optimistic locking
- A PostgreSQL provider plus repository/service boundaries for future webhook or customer-service adapters
- LangGraph handoff nodes for risk, manual refund, confirmed human support, and formal complaints
- Internal FastAPI endpoints for case lookup, event history, and validated status transitions
- Focused unit, graph integration, API contract, and optional PostgreSQL round-trip tests

## What's new in v0.3.0

- A conservative bilingual JSON rule set for English and Chinese risk expressions
- Deterministic separation between hard-critical matches and contextual risk signals
- Structured LLM semantic classification across five severity levels and six categories
- Safety-first handling for high and critical risks without weakening the existing refund policy
- An interrupt-based choice when a low- or medium-risk message also contains an actionable order request
- Dedicated risk nodes, routing helpers, state fields, prompts, and offline tests

## Features

- Structured order-number detection with an OpenAI-compatible chat model
- Structured intent routing for refunds, order inquiries, and complaints
- Explicit human-support request detection with interrupt-based confirmation
- Structured classification of staff-conduct and other formal complaints
- Deterministic bilingual risk-rule matching before any semantic risk classification
- Structured semantic risk classification for self-harm, violence, legal, regulatory, reputation, and other risks
- Hard-critical risk handling that cannot be downgraded by the semantic classifier
- Order-priority confirmation when non-critical risk and an order request appear together
- Concise complaint responses that do not invent order or refund outcomes
- Deterministic refund-policy checks
- Human confirmation before an automatic refund is created
- Manual-review routing for refunds of 100 or more
- Safe multi-turn order context with explicit-reference checks
- PostgreSQL persistence with idempotent request creation
- Deterministic support-case creation for qualified risk and manual-refund triggers
- One unresolved case per thread and case type, with later triggers appended as events
- Offline unit and graph integration tests
- Bundled demonstration orders with dates relative to the current day

## Workflow

```text
User message
    -> check deterministic risk rules
       -> hard critical
          -> return a safety or human-review response
       -> otherwise classify semantic risk
          -> high / critical
             -> return a safety or human-review response
          -> none / low / medium
             -> classify business intent
                -> complaint
                   -> classify staff-conduct / other formal / ordinary complaint
                   -> confirm an explicit human-support request when present
                   -> normal complaint response or non-critical risk response
                -> order inquiry / refund request
                   -> confirm an explicit human-support request when present
                   -> low / medium risk
                      -> ask whether to handle the order now
                         -> continue with the order
                         -> continue with the risk concern
                   -> no risk
                      -> continue with the order
                   -> detect and find the order
                   -> order inquiry: return order information
                   -> refund request: check deterministic refund policy
                      -> reject an ineligible order
                      -> route a large refund to customer service
                      -> ask for refund confirmation
                         -> create one PostgreSQL refund request
                         -> cancel
    -> finalize support-case handoff
       -> no qualifying trigger: finish without a case database query
       -> qualifying trigger: create a case or append an idempotent event
```

## Risk handling

Risk rules live in `src/agent/data/risk_rules.json`. Each entry contains an ID, matching pattern, language, category, severity, and rule type. The matcher normalizes Unicode, apostrophes, letter case, and whitespace before applying conservative literal phrase matching.

- `hard_critical` rules represent explicit high-confidence danger. A match bypasses the semantic classifier and cannot be downgraded by the LLM.
- `risk_signals` provide context only. A signal is passed to the semantic classifier and does not independently decide the final severity or whether human handling is required.
- Messages without a hard-critical match are classified as `none`, `low`, `medium`, `high`, or `critical` using structured model output.
- A high or critical result ends the business flow with an appropriate safety or human-review response.
- A low or medium result may continue through business-intent routing. When an order inquiry or refund request is also present, the graph pauses and asks the user which concern to handle.

The rule matcher does not call an LLM, route the graph, or make a final human-review decision.

## Support-case handoff

Every completed graph turn passes through `finalize_case_handoff`. The node
converts existing structured graph facts into `HandoffPolicyInput`, applies the
deterministic handoff policy, and calls `CaseService` only when the policy
requires a case. Normal order inquiries, low-risk messages, and ordinary
expressions of dissatisfaction do not query the case repository.

The current graph integration creates cases for:

- hard-critical rule matches;
- semantic `medium`, `high`, or `critical` risks;
- refunds that require manual review;
- explicit human-support requests after user confirmation;
- structured staff-conduct complaints;
- other complaints that the classifier identifies as an explicit formal complaint.

Ordinary dissatisfaction still receives a complaint response without creating a
case. A human-support request is not persisted until the user confirms the
`human_handoff_confirmation` interrupt.

Case persistence requires the LangGraph `thread_id` and a stable triggering
`HumanMessage.id`. The message ID forms part of the idempotency key, so the graph
does not generate a random fallback. PostgreSQL failures remain visible as run
failures rather than reporting a handoff that was not saved.

### Internal support-case API

The custom FastAPI application exposes operational case endpoints under
`/internal/support-cases` alongside the LangGraph Agent Server API. The API
requires a valid bearer token (`401` without credentials) and enforces
role-based permissions (`403` when the caller lacks the permission, `404` for
inaccessible resources):

- `GET /internal/support-cases` lists cases with optional `status`, `priority`,
  `case_type`, `thread_id`, and `order_id` filters;
- `GET /internal/support-cases/{case_id}` returns one case;
- `GET /internal/support-cases/{case_id}/events` returns its immutable audit events;
- `POST /internal/support-cases/{case_id}/status` applies an idempotent status change;
- `POST /internal/support-cases/{case_id}/assign` assigns a case to a support
  agent (`supervisor` only) and appends an immutable `assigned` event.

Role permissions: customers read their own cases only; support agents read and
update cases assigned to them; supervisors manage the whole tenant queue and
can assign cases. See [`docs/internal_case_api.md`](docs/internal_case_api.md)
for request examples, error codes, lifecycle rules, and deployment limitations.

The status request requires a stable `request_id`; the audit actor is derived
from the authenticated identity and cannot be supplied by the caller. Moving a
case to `on_hold` additionally requires `on_hold_reason`. See
[`docs/internal_case_api.md`](docs/internal_case_api.md) for request examples,
error codes, lifecycle rules, and deployment limitations.

### Internal Provider operations API

The v0.8 control plane exposes five routes under
`/internal/provider-operations`: queue overview; Outbox and Inbox detail; and
Outbox and Inbox redrive. Both reads and writes require an authenticated
Supervisor with the exact Provider operations permission. Tenant scope comes
only from the authenticated identity. Missing and cross-tenant identifiers use
the same 404 response.

Redrive accepts only a stable `request_id` and one fixed reason code. It commits
an audit and new queue cycle synchronously but never calls a Provider or Worker;
the separate Worker later reclaims the item normally. Responses omit payloads,
customer/order/source-message content, Provider connection/reference data,
callback hashes, secrets and raw diagnostics. See
[`docs/v0.8_provider_operations.md`](docs/v0.8_provider_operations.md) for the
route contract, eligibility rules, migration impact, error codes, validation,
and rollback limitations.

Request ids are unique per tenant and queue kind (Outbox and Inbox are separate
namespaces). After migration `0008`, v0.7 Inbox Workers must not run against
cycle-aware/redriven data; drain old Inbox Workers before migration and keep
v0.8 Workers during route rollback unless a reviewed data-coordination gate
proves rollback compatibility. Provider Ops itself adds no new Provider
credential dependency, although the API lifespan still initializes the
existing v0.7 connection resolvers. Authenticated malformed JSON receives a
sanitized `422`; a valid request without credentials receives the shared `401`.

The v1.0 portfolio layer adds a sixth, read-only Supervisor route,
`GET /internal/provider-operations/attempts`, for a bounded tenant-scoped
timeline of payload-free Outbox and Inbox attempt evidence. See
[`docs/v1.0_observability.md`](docs/v1.0_observability.md).

### Resume an order-priority interrupt

When a run returns an `order_priority_confirmation` interrupt, resume the same thread rather than sending a new user message. To continue with the order, send:

```json
{
  "assistant_id": "agent",
  "command": {
    "resume": "handle_order"
  }
}
```

### Resume a human-support confirmation

When a run returns a `human_handoff_confirmation` interrupt, resume the same
thread with `confirm_handoff` to create a `general_support` case, or with
`continue_self_service` to return to the previously classified business flow.

```json
{
  "assistant_id": "agent",
  "command": {
    "resume": "confirm_handoff"
  }
}
```

To continue with the risk concern instead, use:

```json
{
  "assistant_id": "agent",
  "command": {
    "resume": "continue_risk"
  }
}
```

Use the boolean values `true` or `false` when resuming the separate refund-approval interrupt.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management
- PostgreSQL
- An OpenAI or OpenAI-compatible API key and model

## Setup

Clone the repository and install the project with development dependencies:

```bash
git clone <your-repository-url>
cd <repository-directory>
uv sync --extra dev
```

Copy the environment template:

```bash
cp .env.example .env
```

On PowerShell:

```powershell
Copy-Item .env.example .env
```

Fill in at least `OPENAI_API_KEY`, `OPENAI_MODEL`, and the PostgreSQL settings. `OPENAI_BASE_URL` can remain empty when the official OpenAI API is used. `CUSTOMER_SERVICE_CONTACT` optionally controls the contact text shown for manual review. For the v0.7+ asynchronous Provider flow, also configure `PROVIDER_CONNECTIONS_JSON` for outbound commands and `PROVIDER_WEBHOOK_CONNECTIONS_JSON` for inbound HMAC trust; the two credential sets are deliberately separate.

You may configure PostgreSQL with one connection string:

```dotenv
POSTGRES_URI=postgresql://user:password@localhost:5432/refund_agent
```

Alternatively, set the individual `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, and `POSTGRES_DB` variables shown in `.env.example`.

## Database initialization

Create the database, then apply all pending versioned migrations:

```bash
uv run python scripts/apply_migrations.py
```

Applied migration versions and SHA-256 checksums are recorded in
`case_management.schema_migrations`. Existing migrations must not be edited
after they have been applied; add a new numbered SQL file instead. The case
tables live in the separate `case_management` schema and do not modify
LangGraph's internal tables.

## Run locally

Install and run the LangGraph development server:

```bash
uv run --with "langgraph-cli[inmem]" langgraph dev --config langgraph.dev.json
```

The `uv run --with` form runs the CLI inside the project's virtual environment
so the installed `agent` package is importable. The server starts on port
`2024`.

Before talking to the API, configure demo identities in `.env`:

```dotenv
APP_ENV=development
IDENTITY_PROVIDER=demo
IDENTITY_DIRECTORY=none
DEMO_IDENTITY_TOKENS={"demo-customer-token":{"user_id":"customer-a","tenant_id":"tenant-demo","role":"customer"}}
```

Every request must then send `Authorization: Bearer demo-customer-token`.

The development command explicitly selects `langgraph.dev.json`. The committed
`langgraph.json` is the production-safe profile and must not be used to bypass
custom authentication during local Studio setup.

The Provider workers are separate processes and do not run inside LangGraph or
FastAPI. After applying migrations, start them in separate terminals when
testing the asynchronous Provider flow:

```bash
uv run python -m agent.integrations.worker_main
uv run python -m agent.integrations.inbox_worker_main
```

The API receives signed callbacks at
`POST /webhooks/providers/{provider_connection_id}` and only persists a
verified Inbox message. The Inbox worker performs the later domain update.
See [`docs/v0.7_step4_webhook_inbox.md`](docs/v0.7_step4_webhook_inbox.md) for
the signing contract, error boundary, finalization rules, and operational
limits.

### Run with Docker Compose

Docker Compose starts the Agent Server, PostgreSQL, Redis, and the separate
Outbox and Inbox workers. Copy `.env.example` to `.env`, then set
`OPENAI_API_KEY`, `OPENAI_MODEL`, `LANGSMITH_API_KEY`, a URL-safe
`POSTGRES_PASSWORD`, and `PROVIDER_CONNECTIONS_JSON`. Configure
`PROVIDER_WEBHOOK_CONNECTIONS_JSON` before accepting Provider callbacks.

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f langgraph-api
```

The API is available at `http://127.0.0.1:8000`. Check it with:

```bash
curl http://127.0.0.1:8000/ok
```

Compose supplies `DATABASE_URI`, `POSTGRES_URI`, and `REDIS_URI` inside the API container. The one-shot `case-migrations` service applies pending migrations before `langgraph-api` and both workers start, including when the `postgres-data` volume already exists. Migration failure prevents all three application processes from starting. The workers expose no ports: `outbox-worker` receives only its database and outbound Provider configuration, while `inbox-worker` receives only its database URI and worker ID.

Stop the services without deleting database data:

```bash
docker compose down
```

To also delete the local PostgreSQL data volume and initialize a fresh database on the next start:

```bash
docker compose down -v
```

### Runtime image provenance

The application image pins the official stable LangGraph Agent Server
`0.12.6-py3.11-wolfi` manifest and builds the Datadog `serverless-init` helper
from public Datadog Agent 7.81.2 source commit
`6dbfeceb7c8e1575803f209afaa62004293724d6` with the pinned glibc-based
`golang:1.26.6-bookworm` builder. The build checks the pinned toolchain, ELF
interpreter and shared-library resolution and directly executes the helper in
the final runtime before the image can succeed.
The original `/app/datadog-init` invocation and `ddtrace-run` path are retained;
source, compiler, and build tools remain outside the final stage. Apache-2.0,
NOTICE, and third-party license records are copied into
`/usr/share/licenses/datadog-init/`.

Before application layers are added, the build verifies and removes only the
known vulnerable helper from the pinned upstream rootfs, then copies that clean
rootfs into a scratch-based stage and reconstructs the upstream container
configuration. This intentional flattening prevents scanners from treating the
now-inaccessible vulnerable ancestor binary as part of the final image. It also
means upstream layer-level provenance is represented by the pinned digest and
documented config/filesystem parity evidence rather than retained ancestry.

Two release-graph modules are narrowly pinned to their first security-fixed
versions: `golang.org/x/net@v0.56.0` and
`google.golang.org/grpc@v1.82.1`. Go verifies their module checksums during the
builder stage. These overrides are part of the approved dependency-graph
divergence and must be retested with the helper when changed.

This source build is an explicit security exception to the upstream image: the
upstream helper was built from a modified dependency graph and supplied no
verifiable build attestation. The public 7.81.2 release graph is therefore not
byte-for-byte equivalent. Base, builder, source commit, Go build tags and
version linker flags are pinned in `Dockerfile`; updates require rebuilding,
runtime smoke tests, and a Critical/High scan of the resulting image. The final
runtime also constrains `cryptography` to `>=50,<51` under the Agent Server's
own constraints.

## Demonstration orders

The bundled data is intended only for local demonstration and tests.

| Order | Scenario |
| --- | --- |
| `ORD-10001` | Eligible automatic refund |
| `ORD-10002` | Eligible, manual review required |
| `ORD-10003` | Refund deadline expired |
| `ORD-10004` | Order not delivered |
| `ORD-10005` | Already refunded |
| `ORD-10006` | Invalid future delivery date |
| `ORD-10007` | Eligible on the seven-day boundary |
| `ORD-10008` | Confirmed, unfulfilled order for automatic cancellation testing |
| `ORD-10009` | Processing order for manual cancellation-review testing |
| `ORD-10010` | Shipped order with intentionally stalled tracking for delivery-investigation testing |
| `ORD-10011` | Recently delivered order for return or exchange testing |
| `ORD-10012` | Confirmed order whose local Showcase Provider fails once, then recovers |

## v0.5 order-operation lifecycle

The v0.5 operation subflow is deliberately separate from the existing refund
flow:

```text
detect order -> load current Provider snapshot -> extract one normalized request
-> apply deterministic policy -> request explicit confirmation
-> submit idempotently or create a manual-review / delivery-investigation case
```

The LLM can identify an intent, reason, delivery issue, or exchange variant. It
does not decide eligibility, case priority, or whether an order mutation is
permitted. A `submitted` result means that the local demo Provider accepted the
request; it does not claim that a real warehouse, carrier, or payment system
has completed the downstream work.

`src/agent/data/orders.json` remains the simple v0.4 order-lookup fixture used
by `search_order`. The richer v0.5 snapshots and idempotent submissions are
provided by the process-local `DemoOrderProvider`. They intentionally share the
demonstration order IDs but have different responsibilities; neither is a
production OMS, WMS, carrier, payment, or inventory integration.

## v0.5 Docker/API acceptance test

The following validation creates local demonstration records in the Compose
PostgreSQL database. Wait until the API service is healthy before calling the
health endpoint; an immediate `curl: (52) Empty reply from server` after a
rebuild can simply mean Uvicorn is still starting.

```powershell
docker compose up --build -d
docker compose ps
curl.exe http://127.0.0.1:8000/ok
```

Open `http://127.0.0.1:8000/docs`, create an assistant with
`graph_id: "agent"`, then create a thread. Use
`POST /threads/{thread_id}/runs/wait` for each scenario:

| Input | Expected first result | Resume / expected final result |
| --- | --- | --- |
| `Please cancel ORD-10008.` | `order_operation_confirmation` | Resume with `true`; `operation_status: submitted` |
| `Please cancel ORD-10009.` | `order_operation_confirmation` | Resume with `true`; P1 `order_operation_review` case and `manual_review` operation |
| `Tracking for ORD-10010 has not updated.` | delivery-investigation confirmation | Resume with `true`; P1 `delivery_investigation` case and no order-operation record |
| `What is my support request status?` in the same thread | No interrupt | Lists only that thread's support cases |

For an interrupted run, submit the next request to the same thread with:

```json
{
  "assistant_id": "YOUR_ASSISTANT_ID",
  "command": { "resume": true }
}
```

## Quality checks

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

The graph and internal-API unit tests use offline fakes and require no API key.
PostgreSQL repository and API round-trip tests are skipped unless
`CASE_TEST_POSTGRES_URI` points to a disposable test database. To run them explicitly:

```powershell
$env:CASE_TEST_POSTGRES_URI = "postgresql://test-user:test-password@127.0.0.1:55432/test-db"
uv run pytest -m postgres -p no:cacheprovider
```

Never point `CASE_TEST_POSTGRES_URI` at a production database. PostgreSQL tests
use unique tenant/test identifiers and must run only in a disposable database.
GitHub Actions provisions a disposable PostgreSQL 16 service and runs the
suite on both supported Python versions.

## Project layout

```text
.
|-- .github/workflows/ci.yml
|-- compose.yaml
|-- Dockerfile
|-- scripts/
|   `-- apply_migrations.py
|-- src/agent/
|   |-- cases/
|   |   |-- api.py
|   |   |-- api_errors.py
|   |   |-- api_models.py
|   |   |-- models.py
|   |   |-- policy.py
|   |   |-- postgres_repository.py
|   |   |-- repository.py
|   |   |-- runtime.py
|   |   `-- service.py
|   |-- sql/migrations/
|   |-- nodes/
|   |   |-- complaints.py
|   |   |-- cases.py
|   |   |-- intent.py
|   |   |-- orders.py
|   |   |-- refunds.py
|   |   `-- risk.py
|   |-- tools/
|   |-- data/
|   |   |-- orders.json
|   |   `-- risk_rules.json
|   |-- graph.py
|   |-- models.py
|   |-- order_context.py
|   |-- prompts.py
|   |-- risk_matcher.py
|   |-- routing.py
|   |-- schemas.py
|   |-- state.py
|   `-- webapp.py
|-- tests/
|   |-- integration_tests/
|   `-- unit_tests/
|-- .env.example
|-- langgraph.json
`-- pyproject.toml
```

`graph.py` is intentionally limited to workflow assembly. Prompts, model factories, state, conditional routing, deterministic order context, and domain nodes live in separate modules so each concern can be reviewed and tested independently.

## Security and production notes

- Never commit `.env` or real credentials. The repository ignores `.env` by default.
- The included order database is demonstration data, not customer data.
- The demo identity provider maps bearer tokens from `DEMO_IDENTITY_TOKENS`
  and is deterministic local-development infrastructure only. Production mode
  rejects demo authentication and requires configured OIDC plus read-only SCIM.
- Assignment (`POST /{case_id}/assign`) requires an active same-tenant target
  with a mapped `support_agent` or `supervisor` role. Missing, cross-tenant,
  inactive, and wrong-role targets share one non-enumerating result.
- A production service must authenticate users and verify that an order
  belongs to the requesting user.
- v0.9 supplies authorization and identity audit invariants, but a real
  deployment still requires managed secrets, gateway rate limits and log
  redaction, monitoring, and a real order service.
- Review the refund policy and manual-review threshold with the responsible business and legal teams.
- Risk and manual-refund handoffs are persisted as support cases, but no external customer-service queue adapter is connected yet.
- The internal support-case API is authenticated and role-protected from
  v0.6. Compose binds it to `127.0.0.1`; do not expose it to an external
  network without additional gateway controls such as rate limiting.
- The Provider operations API is Supervisor-only and field-whitelisted. The
  v0.9 production contract requires an internal gateway and independently
  verified Bearer tokens; forwarded identity headers are never trusted.
- v0.9 does not perform or approve a real production deployment. See
  `docs/v0.9_identity_access.md` for the exact production envelope and runbook.
- v0.9 intentionally does not include an operations UI, retention cleanup,
  Worker heartbeat/Prometheus metrics, a distributed limiter or external
  queue, or dynamic Provider key rotation.
- Human-request and formal-complaint detection use LLM structured output and therefore require production evaluation against representative multilingual conversations.
- The bundled risk rules are intentionally conservative demonstration data, not a complete safety or compliance vocabulary.
- Semantic risk classification depends on the configured model and may vary across wording or languages. Validate the policy, prompts, and responses with qualified safety, legal, and compliance reviewers before production use.

## License

Released under the MIT License. See `LICENSE`.

---

# LangGraph 退款 Agent

这是一个使用 LangGraph 构建的小型风险感知客服助手。它可以识别订单操作、退款申请、订单查询、物流问题和投诉，在 LLM 语义风险分类前执行确定性风险规则检测，使用确定性规则判断业务资格，在会改变状态的操作前请求用户确认，并把订单操作、退款申请、人工工单和不可变事件保存到 PostgreSQL。

版本：`0.9.0`

## v0.9.0 新增内容

- 使用 vendor-neutral 的异步 OIDC JWT runtime 替代生产 demo 认证，FastAPI
  与 LangGraph Server 共用同一配置、校验器和 claim policy；
- 固定 issuer、audience、非对称算法白名单并校验签名、时间 claim 与 `kid`，
  JWKS 使用有界缓存、单次 unknown-kid refresh 与 fail-closed outage 语义；
- tenant/user 只来自受信 claim，角色只来自 allowlisted group mapping，权限
  继续由服务端 RBAC 推导并拒绝 token 自带 permissions；
- 新增只读 SCIM 2.0 人员目录，工单只能分配给同 tenant、active 且为客服或
  supervisor 的目标，同时避免跨租户身份枚举；
- 生产启动会拒绝 demo auth、Studio bypass、不可用的 OIDC/SCIM、明文
  Provider HTTP，以及本地或非 TLS PostgreSQL；
- 明确外部 TLS PostgreSQL、gateway、readiness、rollback、隐私和本地 Compose
  契约，但不执行真实部署，也不保存生产 secrets；
- 保持 Graph workflow、Provider wire schema、0001-0008 migration、actor 格式、
  v0.8 数据和显式本地 demo 模式兼容。

## v0.8.0 新增内容

- 新增租户隔离的 Provider 运维控制面，通过五个严格的内部 FastAPI 路由
  安全查看队列并执行人工 redrive；
- 读取和恢复均要求准确的 `supervisor` 角色以及对应的
  `provider_ops:read` / `provider_ops:redrive` 权限，Service 与 PostgreSQL
  边界都会重复校验；
- 通过不可变的操作人/固定原因审计，实现幂等、并发安全的 Outbox / Inbox
  新周期恢复，HTTP 请求不会同步调用 Provider；
- Outbox 只恢复终止的技术失败；Provider 业务拒绝保持终止状态且禁止重发；
- Inbox 只恢复失败且无 lease 的消息，并由普通 Worker 重新执行完整关联与
  fencing 校验，不修改 callback payload 或 hash；
- 新增 additive migration `0008`、真实 PostgreSQL HTTP-to-Worker E2E，以及
  严格的响应/OpenAPI 字段白名单审计。

## v0.7.0 新增内容

- 新增版本化 Provider 命令契约，包括 canonical envelope、确定性签名、
  连接解析、重试分类，以及严格的租户与聚合关联校验；
- 在 PostgreSQL 中事务性持久化 Outbox、Inbox、处理 Attempt、lease 归属、
  重试时间与不可变审计事件；
- 通过独立 Outbox Worker 异步分发 Provider 命令，并实现限流、lease 续租、
  过期恢复与安全失败协调；
- 通过受请求大小限制的 FastAPI Webhook 边界接收 HMAC 验签回调，并提供
  replay-safe Inbox 持久化与冲突检测；
- 使用带 fencing 的订单操作与工单 Finalizer 原子应用回调，覆盖重复、过期、
  重试耗尽、事务回滚与并发场景；
- 在 Docker Compose 中独立运行 Outbox / Inbox Worker，使出站 Provider 凭据
  与入站 Worker、LangGraph workflow 保持隔离。

## v0.6.0 新增内容

- 引入可信身份边界：每个请求在接触任何资源前，都必须先通过可替换的
  `IdentityProvider`（本地开发使用确定性的 demo provider）完成认证；
- 通过官方 authentication / authorization handler 限制 LangGraph 的
  thread、run 和 assistant；thread 所有者会被写入并强制校验，未授权资源
  默认拒绝；
- 为退款、工单、工单事件、订单操作和操作事件增加
  `customer_id` / `tenant_id` / `created_by` 归属字段（additive migration
  `0005`）；legacy 数据被隔离到保留的 `legacy` 租户，任何真实身份都不可见；
- Repository 的所有读写都强制携带 `AccessScope`；单个资源查询对"不存在"
  和"无权访问"统一返回 404（防枚举）；
- 订单查询、退款、取消、退货、换货和物流调查全部验证订单归属；两条订单
  数据源（`orders.json` 与 `DemoOrderProvider`）共用同一套归属规则，且演示
  归属保持一致；
- 内部工单 API 增加保护：无凭据返回 401，权限不足返回 403，资源不可见
  返回 404；
- 实现 `customer` / `support_agent` / `supervisor` 三种角色的权限控制，
  新增仅 supervisor 可用的工单分配端点，并记录不可变的 `assigned` 审计
  事件（migration `0006`）；
- 原始 Token 或 Bearer 凭据不会写入 Graph state、PostgreSQL、日志或
  LangSmith trace。

## v0.5.1 新增内容

- 使用订单操作提取 fake，让 Graph 构建测试完全不依赖模型密钥；
- 让 PostgreSQL 自动操作集成测试与 v0.5 Provider 提交边界保持一致；
- 在不改变客户侧业务行为的前提下，验证 Python 3.11 和 Python 3.12 使用同一套 CI 测试。

## v0.5.0 新增内容

- 新增取消订单、退货、换货、物流问题和当前会话工单状态查询意图；
- 订单操作会先加载 Provider 快照，再由确定性 policy 判断结果；
- 自动和人工订单操作均要求一次明确确认；自动操作通过稳定幂等键提交 Provider；
- 人工订单操作创建或复用 `order_operation_review` 工单并关联回订单操作；
- 物流调查创建 `delivery_investigation` 工单，但不把用户陈述直接当作已证实事实；
- 工单状态查询只使用当前 LangGraph `thread_id`，不会接受用户指定其他会话；
- 当前 Provider 是进程内演示实现，真实 OMS / WMS / Carrier 集成仍属于后续工作。

## v0.4.0 新增内容

- 增加确定性的工单交接策略，明确工单类型、优先级和 reason codes
- 使用 PostgreSQL 持久化工单和不可变事件，并实现幂等与乐观锁
- 实现 PostgreSQL provider，并预留 Repository/Service 边界供后续 Webhook 或客户客服系统适配器接入
- 接入风险、大额退款、已确认真人请求和正式投诉的 LangGraph 工单节点
- 增加内部 FastAPI 接口，用于查询工单、读取事件和校验状态转换
- 增加单元、Graph 集成、API 契约及可选 PostgreSQL 往返测试

## v0.3.0 新增内容

- 增加保守的中英文 JSON 风险规则配置
- 明确区分 hard critical 硬性高危命中与上下文 risk signal
- 使用结构化输出完成五级严重程度和六类风险的 LLM 语义分类
- 高危和严重风险优先进入安全处理，同时保留原有确定性退款规则
- 当非高危风险与订单请求同时出现时，通过 interrupt 让用户选择优先处理内容
- 增加独立的风险节点、路由函数、状态字段、提示词和离线测试

## 功能

- 使用兼容 OpenAI 接口的聊天模型识别订单号
- 使用结构化输出区分退款申请、订单查询和投诉
- 识别明确的真人客服请求，并通过 interrupt 让用户确认
- 使用结构化输出区分人员行为投诉、其他正式投诉和一般不满
- 在语义分类前执行确定性的中英文风险规则匹配
- 使用结构化输出分类自伤、暴力、法律、监管、声誉和其他风险
- hard critical 命中不会被后续 LLM 降级
- 当非高危风险和订单请求同时出现时询问用户处理优先级
- 针对投诉生成简洁回复，但不会虚构订单或退款结果
- 使用确定性规则检查退款资格
- 创建自动退款申请前要求用户确认
- 金额大于或等于 100 时转人工审核
- 仅在用户明确指代上一订单时复用多轮订单上下文
- 使用 PostgreSQL 持久化，并保证同一订单不会重复创建退款申请
- 对符合条件的风险和大额退款触发确定性的人工工单
- 同一 thread 和工单类型只保留一个未解决工单，后续触发追加为事件
- 包含离线单元测试和图集成测试
- 演示订单使用相对日期，不会随着时间推移全部失效

## 工作流

```text
用户消息
    -> 检查确定性风险规则
       -> hard critical
          -> 返回安全提示或人工审核回复
       -> 其他情况进入语义风险分类
          -> high / critical
             -> 返回安全提示或人工审核回复
          -> none / low / medium
             -> 识别业务意图
                -> 投诉
                   -> 分类人员行为投诉 / 其他正式投诉 / 一般不满
                   -> 存在明确真人客服请求时要求用户确认
                   -> 普通投诉回复或非高危风险回复
                -> 订单查询 / 退款申请
                   -> 存在明确真人客服请求时要求用户确认
                   -> low / medium 风险
                      -> 询问是否现在处理订单
                         -> 继续处理订单
                         -> 继续处理风险问题
                   -> 无风险
                      -> 继续处理订单
                   -> 识别并查询订单
                   -> 订单查询：返回订单信息
                   -> 退款申请：检查确定性退款规则
                      -> 拒绝不符合条件的订单
                      -> 大额退款转客服人工处理
                      -> 请求用户确认
                         -> 在 PostgreSQL 中创建一条退款申请
                         -> 取消退款
    -> 完成人工工单交接
       -> 没有符合条件的触发：不查询工单数据库并结束
       -> 存在符合条件的触发：创建工单或幂等追加事件
```

## 风险处理

风险规则保存在 `src/agent/data/risk_rules.json`。每条规则包含 ID、匹配文本、语言、风险类别、严重程度和规则类型。matcher 会先规范化 Unicode、英文撇号、字母大小写和空白，再进行保守的字面短语匹配。

- `hard_critical` 表示含义明确且置信度高的危险表达。命中后会绕过语义分类器，LLM 不能将其降级。
- `risk_signals` 只提供上下文提示。signal 会交给语义分类器继续判断，本身不会直接决定最终风险级别或是否转人工。
- 未命中 hard critical 的消息由结构化模型输出分类为 `none`、`low`、`medium`、`high` 或 `critical`。
- `high` 和 `critical` 会结束业务流程，并返回相应的安全提示或人工审核回复。
- `low` 和 `medium` 可以继续识别业务意图。当消息同时包含订单查询或退款请求时，图会暂停并让用户选择要处理的问题。

规则 matcher 不调用 LLM，不包含图路由逻辑，也不直接决定最终是否转人工。

## 人工工单交接

每个完整结束的 Graph 轮次都会经过 `finalize_case_handoff`。该节点把现有
Graph 结构化事实转换为 `HandoffPolicyInput`，执行确定性的交接策略，并且
只有在策略要求创建工单时才调用 `CaseService`。普通订单查询、低风险消息和
一般的不满表达不会查询工单 Repository。

目前 Graph 已接入以下工单触发条件：

- hard critical 规则命中；
- semantic `medium`、`high` 或 `critical` 风险；
- 需要人工审核的大额退款；
- 用户确认后的明确真人客服请求；
- 结构化识别的人员行为投诉；
- 分类器识别为明确正式投诉的其他投诉。

普通表达不满仍只返回投诉回复，不创建工单。真人客服请求只有在用户确认
`human_handoff_confirmation` interrupt 后才会持久化。

工单持久化要求存在 LangGraph `thread_id` 和稳定的触发消息
`HumanMessage.id`。消息 ID 会参与生成幂等键，因此 Graph 不会随机补一个
ID。PostgreSQL 写入失败会明确导致本次 Run 失败，不会返回一个实际上没有
保存成功的人工交接结果。

### 内部工单 API

自定义 FastAPI 应用会在 LangGraph Agent Server API 旁边提供
`/internal/support-cases` 下的工单操作接口。该 API 需要有效 Bearer Token
（无凭据返回 401），并按角色执行权限控制（权限不足返回 403，资源不可见
返回 404）：

- `GET /internal/support-cases`：按照 `status`、`priority`、`case_type`、
  `thread_id` 或 `order_id` 筛选工单；
- `GET /internal/support-cases/{case_id}`：查询单个工单；
- `GET /internal/support-cases/{case_id}/events`：查询不可变的审计事件；
- `POST /internal/support-cases/{case_id}/status`：执行幂等的状态变更；
- `POST /internal/support-cases/{case_id}/assign`：把工单分配给客服
  （仅 supervisor），并追加不可变的 `assigned` 事件。

角色权限：customer 只能查看自己的工单；support_agent 只能读取和修改分配
给自己的工单；supervisor 管理整个租户队列并可以分配工单。请求示例、错误码、
状态规则和部署限制详见
[`docs/internal_case_api.md`](docs/internal_case_api.md)。

状态请求必须提供稳定的 `request_id`；审计操作人由已认证身份生成，调用方不能
自行传入。将工单改为 `on_hold` 时还必须提供 `on_hold_reason`。请求示例、错误码、状态规则和部署
限制详见 [`docs/internal_case_api.md`](docs/internal_case_api.md)。

### 内部 Provider 运维 API

v0.8 在 `/internal/provider-operations` 下提供五个控制面接口：队列概览、
Outbox / Inbox 详情，以及两类 redrive。读写都只允许具有准确 Provider 运维
权限的已认证 supervisor。租户只能来自认证身份；不存在与跨租户资源统一返回
相同的 404。

redrive 请求只接受稳定的 `request_id` 和固定 reason code。请求会同步提交审计
与新队列周期，但不会直接调用 Provider 或 Worker；独立 Worker 之后按普通路径
重新认领。响应不会包含 payload、客户/订单/源消息内容、Provider 连接或引用、
callback hash、密钥和原始诊断信息。完整路由契约、资格规则、迁移影响、错误码、
验证与回滚限制见
[`docs/v0.8_provider_operations.md`](docs/v0.8_provider_operations.md)。

`request_id` 按租户与队列种类分别唯一（Outbox 与 Inbox 是独立命名空间）。应用
迁移 `0008` 后，v0.7 Inbox Worker 不得再处理 cycle-aware/redrive 数据；迁移前
必须先排空旧 Worker，回滚路由时仍应保留 v0.8 Worker，除非经过专门的数据协调
门禁证明可安全回退。Provider Ops 本身不新增 Provider 凭据依赖，但 API lifespan
仍会初始化 v0.7 已有的连接 resolver。已认证调用方提交畸形 JSON 时获得脱敏
`422`，无凭据的合法请求继续使用共享 `401`。

### 恢复订单优先级 interrupt

当运行结果出现 `order_priority_confirmation` interrupt 时，应当恢复同一个 thread，不能发送一条新的用户消息。继续处理订单时发送：

```json
{
  "assistant_id": "agent",
  "command": {
    "resume": "handle_order"
  }
}
```

### 恢复真人客服确认 interrupt

当运行结果出现 `human_handoff_confirmation` interrupt 时，恢复同一个
thread 并传入 `confirm_handoff` 会创建 `general_support` 工单；传入
`continue_self_service` 会返回之前识别出的业务流程。

```json
{
  "assistant_id": "agent",
  "command": {
    "resume": "confirm_handoff"
  }
}
```

如果要继续处理风险问题，则发送：

```json
{
  "assistant_id": "agent",
  "command": {
    "resume": "continue_risk"
  }
}
```

恢复另一个退款确认 interrupt 时，应使用布尔值 `true` 或 `false`。

## 环境要求

- Python 3.11 或更高版本
- 使用 [uv](https://docs.astral.sh/uv/) 管理依赖
- PostgreSQL
- OpenAI 或兼容 OpenAI 接口的 API 密钥和模型

## 安装

克隆仓库并安装开发依赖：

```bash
git clone <你的仓库地址>
cd <仓库目录>
uv sync --extra dev
```

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

至少填写 `OPENAI_API_KEY`、`OPENAI_MODEL` 和 PostgreSQL 配置。使用 OpenAI 官方接口时，`OPENAI_BASE_URL` 可以留空。`CUSTOMER_SERVICE_CONTACT` 可以用来设置转人工审核时显示的联系方式。使用 v0.7+ 异步 Provider 流程时，还应分别配置出站命令使用的 `PROVIDER_CONNECTIONS_JSON` 与入站 HMAC 信任使用的 `PROVIDER_WEBHOOK_CONNECTIONS_JSON`；两套凭据有意隔离。

可以直接设置完整的 PostgreSQL 连接地址：

```dotenv
POSTGRES_URI=postgresql://user:password@localhost:5432/refund_agent
```

也可以分别填写 `.env.example` 中的 `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_HOST`、`POSTGRES_PORT` 和 `POSTGRES_DB`。

## 初始化数据库

创建数据库后执行所有尚未应用的版本化迁移：

```bash
uv run python scripts/apply_migrations.py
```

迁移版本和 SHA-256 校验值保存在
`case_management.schema_migrations` 中。迁移一旦应用就不应修改；数据库结构变化应新增编号更高的 SQL 文件。工单表位于独立的 `case_management` schema，不会修改 LangGraph 内部表。

## 本地运行

启动 LangGraph 开发服务器：

```bash
uv run --with "langgraph-cli[inmem]" langgraph dev --config langgraph.dev.json
```

`uv run --with` 会在项目自己的虚拟环境中运行 CLI，保证已安装的 `agent`
包可以被导入。服务器监听 `2024` 端口。

调用 API 前，先在 `.env` 中配置 demo 身份：

```dotenv
APP_ENV=development
IDENTITY_PROVIDER=demo
IDENTITY_DIRECTORY=none
DEMO_IDENTITY_TOKENS={"demo-customer-token":{"user_id":"customer-a","tenant_id":"tenant-demo","role":"customer"}}
```

之后每个请求都需要携带 `Authorization: Bearer demo-customer-token`。

本地命令会显式选择 `langgraph.dev.json`。仓库中的 `langgraph.json` 是生产安全
profile，本地 Studio 配置不能借此绕过 custom auth。

Provider Worker 是独立进程，不会在 LangGraph 或 FastAPI 内启动。应用迁移后，
可在不同终端启动异步 Provider 流程：

```bash
uv run python -m agent.integrations.worker_main
uv run python -m agent.integrations.inbox_worker_main
```

API 通过 `POST /webhooks/providers/{provider_connection_id}` 接收签名回调，
请求阶段只持久化验签后的 Inbox；后续领域更新由 Inbox Worker 完成。签名契约、
错误边界、收尾规则与运行限制见
[`docs/v0.7_step4_webhook_inbox.md`](docs/v0.7_step4_webhook_inbox.md)。

### 使用 Docker Compose 运行

Docker Compose 会同时启动 Agent Server、PostgreSQL、Redis，以及独立的
Outbox / Inbox Worker。先将 `.env.example` 复制为 `.env`，然后填写
`OPENAI_API_KEY`、`OPENAI_MODEL`、`LANGSMITH_API_KEY`、适合放入 URL 的
`POSTGRES_PASSWORD` 和 `PROVIDER_CONNECTIONS_JSON`；接收 Provider 回调前还需
配置 `PROVIDER_WEBHOOK_CONNECTIONS_JSON`。

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f langgraph-api
```

API 地址为 `http://127.0.0.1:8000`，可以执行以下命令检查：

```bash
curl http://127.0.0.1:8000/ok
```

Compose 会自动为 API 容器设置 `DATABASE_URI`、`POSTGRES_URI` 和 `REDIS_URI`。一次性的 `case-migrations` 服务会在 `langgraph-api` 和两个 Worker 启动前应用待执行迁移，即使 `postgres-data` 数据卷已经存在也会运行。迁移失败时三个应用进程都不会启动。两个 Worker 都不暴露端口：`outbox-worker` 仅接收数据库与出站 Provider 配置，`inbox-worker` 仅接收数据库 URI 和 Worker ID。

停止服务但保留数据库数据：

```bash
docker compose down
```

如果还要删除本地 PostgreSQL 数据，并在下次启动时重新初始化：

```bash
docker compose down -v
```

### 运行镜像来源

应用镜像固定使用官方 LangGraph Agent Server
`0.12.6-py3.11-wolfi` manifest，并通过固定的 Go 1.26.6 builder，从公开的
Datadog Agent 7.81.2 commit
`6dbfeceb7c8e1575803f209afaa62004293724d6` 重建 `serverless-init`。
builder 使用与最终 Wolfi runtime ABI 匹配的 Bookworm/glibc；构建会校验固定
toolchain、ELF interpreter 和共享库解析，并在最终 runtime 中直接执行 helper，
任一门禁失败都会终止镜像构建。
`/app/datadog-init` 与 `ddtrace-run` 的原始调用方式保持不变；源码、编译器和构建
工具不会进入最终 stage。Apache-2.0、NOTICE 与第三方许可记录位于
`/usr/share/licenses/datadog-init/`。

在加入应用层之前，构建会校验并只删除固定上游 rootfs 中已知有漏洞的 helper，
再将干净 rootfs 复制到 scratch stage 并重建上游容器配置。该显式 flatten 可避免
扫描器继续把已经不可访问的上游祖先层 binary 计入最终镜像；相应地，上游层级
来源由固定 digest 和文档化的配置/文件系统一致性证据表达，而不再保留原始 ancestry。

构建阶段还将 release graph 中的 `golang.org/x/net` 固定到首个安全修复版本
`v0.56.0`，并将 `google.golang.org/grpc` 固定到 `v1.82.1`；Go 会校验模块
checksum。这两个 override 也是已批准依赖图差异的一部分，后续变更必须重新测试
helper。

这是针对上游镜像的显式安全例外：上游 helper 使用修改过的依赖图构建，且没有
可验证的构建 attestation，因此公开 7.81.2 release graph 并非逐字节等价。
`Dockerfile` 固定了基础镜像、builder、源码 commit、Go build tags 和版本链接
参数；升级时必须重新构建、执行运行时 smoke test，并对最终镜像重新进行
Critical/High 扫描。最终运行时同时在 Agent Server 自身 constraints 下固定
`cryptography>=50,<51`。

## 演示订单

以下数据仅用于本地演示和测试：

| 订单号 | 场景 |
| --- | --- |
| `ORD-10001` | 符合自动退款条件 |
| `ORD-10002` | 符合条件，但需要人工审核 |
| `ORD-10003` | 已超过退款期限 |
| `ORD-10004` | 订单尚未送达 |
| `ORD-10005` | 已退款 |
| `ORD-10006` | 送达日期在未来，数据无效 |
| `ORD-10007` | 正好处于七天期限边界 |
| `ORD-10008` | 已确认但尚未履约，用于自动取消订单测试 |
| `ORD-10009` | 正在处理，用于取消订单人工审核测试 |
| `ORD-10010` | 已发货且物流刻意停滞，用于物流调查测试 |
| `ORD-10011` | 最近送达，用于退货或换货测试 |
| `ORD-10012` | 本地 Showcase Provider 首次失败、随后恢复的已确认订单 |

## v0.5 订单操作生命周期

v0.5 的订单操作子流程与原有退款流程保持独立：

```text
识别订单 -> 加载当前 Provider 快照 -> 提取一个规范化请求
-> 应用确定性规则 -> 请求明确确认
-> 幂等提交，或创建人工审核 / 物流调查工单
```

LLM 只能识别意图、原因、物流问题或换货规格，不能决定资格、工单优先级，
也不能决定是否允许修改订单。`submitted` 仅表示本地演示 Provider 已接受请求，
不表示真实仓库、物流商或支付系统已经完成后续处理。

`src/agent/data/orders.json` 仍是 v0.4 `search_order` 工具使用的简单订单查询
fixture。v0.5 使用进程内 `DemoOrderProvider` 提供更完整的快照和幂等提交。
二者共用演示订单号但职责不同，均不是生产 OMS、WMS、物流、支付或库存集成。

## v0.5 Docker/API 验收测试

以下验证会在 Compose PostgreSQL 中创建本地演示记录。重建后请先等待 API
服务显示为 healthy；如果刚启动就执行 curl 出现
`curl: (52) Empty reply from server`，通常只是 Uvicorn 尚未启动完成。

```powershell
docker compose up --build -d
docker compose ps
curl.exe http://127.0.0.1:8000/ok
```

打开 `http://127.0.0.1:8000/docs`，创建 `graph_id: "agent"` 的 assistant，
再创建一个 thread。每个场景使用
`POST /threads/{thread_id}/runs/wait`：

| 输入 | 首次预期结果 | 恢复后预期结果 |
| --- | --- | --- |
| `Please cancel ORD-10008.` | `order_operation_confirmation` | 用 `true` 恢复；`operation_status: submitted` |
| `Please cancel ORD-10009.` | `order_operation_confirmation` | 用 `true` 恢复；创建 P1 `order_operation_review` 工单，操作为 `manual_review` |
| `Tracking for ORD-10010 has not updated.` | 物流调查确认 | 用 `true` 恢复；创建 P1 `delivery_investigation` 工单，不创建订单操作记录 |
| 同一 thread 中发送 `What is my support request status?` | 无 interrupt | 仅列出当前 thread 的工单 |

当 run 被 interrupt 后，向同一个 thread 发送下一次请求：

```json
{
  "assistant_id": "YOUR_ASSISTANT_ID",
  "command": { "resume": true }
}
```

## 质量检查

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Graph 和内部 API 单元测试使用离线实现，不需要 API 密钥。只有在
`CASE_TEST_POSTGRES_URI` 指向可清理的测试数据库时，才会运行 PostgreSQL
Repository 与 API 往返集成测试：

```powershell
$env:CASE_TEST_POSTGRES_URI = "postgresql://test-user:test-password@127.0.0.1:55432/test-db"
uv run pytest -m postgres -p no:cacheprovider
```

不要让 `CASE_TEST_POSTGRES_URI` 指向生产数据库。PostgreSQL 测试使用唯一的
租户/测试标识，并且只能在可清理数据库中运行。GitHub Actions 会启动一次性的
PostgreSQL 16 服务，并在两个受支持的 Python 版本上执行这些测试。

## 项目结构

```text
.
|-- .github/workflows/ci.yml
|-- compose.yaml
|-- Dockerfile
|-- scripts/
|   `-- apply_migrations.py
|-- src/agent/
|   |-- cases/
|   |   |-- api.py
|   |   |-- api_errors.py
|   |   |-- api_models.py
|   |   |-- models.py
|   |   |-- policy.py
|   |   |-- postgres_repository.py
|   |   |-- repository.py
|   |   |-- runtime.py
|   |   `-- service.py
|   |-- sql/migrations/
|   |-- nodes/
|   |   |-- complaints.py
|   |   |-- cases.py
|   |   |-- intent.py
|   |   |-- orders.py
|   |   |-- refunds.py
|   |   `-- risk.py
|   |-- tools/
|   |-- data/
|   |   |-- orders.json
|   |   `-- risk_rules.json
|   |-- graph.py
|   |-- models.py
|   |-- order_context.py
|   |-- prompts.py
|   |-- risk_matcher.py
|   |-- routing.py
|   |-- schemas.py
|   |-- state.py
|   `-- webapp.py
|-- tests/
|   |-- integration_tests/
|   `-- unit_tests/
|-- .env.example
|-- langgraph.json
`-- pyproject.toml
```

`graph.py` 只负责组装工作流。提示词、模型工厂、状态、条件路由、确定性订单上下文和各业务节点分别放在独立模块中，便于单独审查和测试。

## 安全与生产环境说明

- 不要提交 `.env` 或任何真实密钥；仓库已经默认忽略 `.env`。
- 内置订单只是演示数据，不能保存真实客户信息。
- demo 身份提供者从 `DEMO_IDENTITY_TOKENS` 解析 Bearer Token，仅用于本地
  确定性开发。生产模式会拒绝 demo auth，并要求配置 OIDC 与只读 SCIM。
- 工单分配（`POST /{case_id}/assign`）要求目标人员同 tenant、active 且映射为
  `support_agent` 或 `supervisor`；missing、cross-tenant、inactive 和 wrong-role
  使用同一不枚举结果。
- 生产服务必须验证用户身份，并确认订单确实属于发起请求的用户。
- v0.9 已提供权限和身份审计不变量；真实上线仍需要托管 secrets、网关限流与
  日志脱敏、监控，并接入真实订单服务。
- 退款规则和人工审核金额阈值需要由相应的业务及法务人员确认。
- 风险和大额退款交接已经持久化为人工工单，但尚未连接外部客户客服队列适配器。
- 内部工单 API 自 v0.6 起已启用认证与角色保护。Compose 只把它绑定到
  `127.0.0.1`；在没有额外的网关控制（如限流）之前，不应将该接口暴露到
  外部网络。
- Provider 运维 API 仅允许 supervisor 且采用字段白名单。v0.9 生产契约要求
  内部网关与应用自行验证 Bearer Token，绝不信任 forwarded identity header。
- v0.9 不执行或批准真实生产部署；完整生产 envelope 与 runbook 见
  `docs/v0.9_identity_access.md`。
- v0.9 明确不包含运维 UI、retention cleanup、Worker heartbeat / Prometheus、
  分布式限流器或外部队列，以及 Provider 动态密钥轮换。
- 真人请求和正式投诉识别依赖 LLM 结构化输出，上线前仍需使用有代表性的多语言对话进行评测。
- 内置风险规则是有意保持保守的演示数据，并不是完整的安全或合规词库。
- 语义风险分类取决于所配置的模型，可能因措辞或语言不同而产生差异。投入生产环境前，应由专业的安全、法务和合规人员验证规则、提示词和回复内容。

## 许可证

本项目使用 MIT License，详见 `LICENSE`。
