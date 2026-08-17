# LangGraph Refund Agent

A small, risk-aware customer-service assistant built with LangGraph. It classifies order operations, refund requests, order inquiries, delivery issues, and complaints; performs deterministic and semantic risk checks; keeps business eligibility decisions deterministic; asks for confirmation before state-changing operations; and stores order operations, refund requests, support cases, and immutable events in PostgreSQL.

Version: `0.5.0`

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
`/internal/support-cases` alongside the LangGraph Agent Server API:

- `GET /internal/support-cases` lists cases with optional `status`, `priority`,
  `case_type`, `thread_id`, and `order_id` filters;
- `GET /internal/support-cases/{case_id}` returns one case;
- `GET /internal/support-cases/{case_id}/events` returns its immutable audit events;
- `POST /internal/support-cases/{case_id}/status` applies an idempotent status change.

The status request requires a stable `request_id` and an `actor`. Moving a case
to `on_hold` additionally requires `on_hold_reason`. See
[`docs/internal_case_api.md`](docs/internal_case_api.md) for request examples,
error codes, lifecycle rules, and deployment limitations.

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

Fill in at least `OPENAI_API_KEY`, `OPENAI_MODEL`, and the PostgreSQL settings. `OPENAI_BASE_URL` can remain empty when the official OpenAI API is used. `CUSTOMER_SERVICE_CONTACT` optionally controls the contact text shown for manual review.

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
uvx --from "langgraph-cli[inmem]" langgraph dev
```

The graph entry point is configured in `langgraph.json` as `agent.graph:create_graph` through the source file path.

### Run with Docker Compose

Docker Compose starts the Agent Server together with PostgreSQL and Redis. Copy `.env.example` to `.env`, then set `OPENAI_API_KEY`, `OPENAI_MODEL`, `LANGSMITH_API_KEY`, and a URL-safe `POSTGRES_PASSWORD`.

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f langgraph-api
```

The API is available at `http://127.0.0.1:8000`. Check it with:

```bash
curl http://127.0.0.1:8000/ok
```

Compose supplies `DATABASE_URI`, `POSTGRES_URI`, and `REDIS_URI` inside the API container. The one-shot `case-migrations` service applies pending migrations before `langgraph-api` starts, including when the `postgres-data` volume already exists. Migration failure prevents the API from starting.

Stop the services without deleting database data:

```bash
docker compose down
```

To also delete the local PostgreSQL data volume and initialize a fresh database on the next start:

```bash
docker compose down -v
```

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
$env:CASE_TEST_POSTGRES_URI = "postgresql://user:password@localhost:5432/refund_agent"
uv run pytest -m postgres tests/integration_tests/test_postgres_case_repository.py
```

Never point `CASE_TEST_POSTGRES_URI` at a production database. The tests create
and remove records whose thread IDs use a unique `case-integration-` prefix.
GitHub Actions provisions a disposable PostgreSQL 16 service and runs these
tests on both supported Python versions.

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
- A production service must authenticate users and verify that an order belongs to the requesting user.
- Add authorization, audit logging, monitoring, rate limits, encrypted secret management, and a real order service before production use.
- Review the refund policy and manual-review threshold with the responsible business and legal teams.
- Risk and manual-refund handoffs are persisted as support cases, but no external customer-service queue adapter is connected yet.
- The v0.4 internal support-case API has no application-level authentication.
  Compose binds it to `127.0.0.1`; do not expose it to an external network until
  authentication and authorization are configured at the Agent Server or gateway.
- Human-request and formal-complaint detection use LLM structured output and therefore require production evaluation against representative multilingual conversations.
- The bundled risk rules are intentionally conservative demonstration data, not a complete safety or compliance vocabulary.
- Semantic risk classification depends on the configured model and may vary across wording or languages. Validate the policy, prompts, and responses with qualified safety, legal, and compliance reviewers before production use.

## License

Released under the MIT License. See `LICENSE`.

---

# LangGraph 退款 Agent

这是一个使用 LangGraph 构建的小型风险感知客服助手。它可以识别订单操作、退款申请、订单查询、物流问题和投诉，在 LLM 语义风险分类前执行确定性风险规则检测，使用确定性规则判断业务资格，在会改变状态的操作前请求用户确认，并把订单操作、退款申请、人工工单和不可变事件保存到 PostgreSQL。

版本：`0.5.0`

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
`/internal/support-cases` 下的工单操作接口：

- `GET /internal/support-cases`：按照 `status`、`priority`、`case_type`、
  `thread_id` 或 `order_id` 筛选工单；
- `GET /internal/support-cases/{case_id}`：查询单个工单；
- `GET /internal/support-cases/{case_id}/events`：查询不可变的审计事件；
- `POST /internal/support-cases/{case_id}/status`：执行幂等的状态变更。

状态请求必须提供稳定的 `request_id` 和操作人 `actor`。将工单改为
`on_hold` 时还必须提供 `on_hold_reason`。请求示例、错误码、状态规则和部署
限制详见 [`docs/internal_case_api.md`](docs/internal_case_api.md)。

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

至少填写 `OPENAI_API_KEY`、`OPENAI_MODEL` 和 PostgreSQL 配置。使用 OpenAI 官方接口时，`OPENAI_BASE_URL` 可以留空。`CUSTOMER_SERVICE_CONTACT` 可以用来设置转人工审核时显示的联系方式。

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
uvx --from "langgraph-cli[inmem]" langgraph dev
```

图入口已在 `langgraph.json` 中配置。

### 使用 Docker Compose 运行

Docker Compose 会同时启动 Agent Server、PostgreSQL 和 Redis。先将 `.env.example` 复制为 `.env`，然后填写 `OPENAI_API_KEY`、`OPENAI_MODEL`、`LANGSMITH_API_KEY` 和一个适合放入 URL 的 `POSTGRES_PASSWORD`。

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f langgraph-api
```

API 地址为 `http://127.0.0.1:8000`，可以执行以下命令检查：

```bash
curl http://127.0.0.1:8000/ok
```

Compose 会自动为 API 容器设置 `DATABASE_URI`、`POSTGRES_URI` 和 `REDIS_URI`。一次性的 `case-migrations` 服务会在 `langgraph-api` 启动前应用待执行迁移，即使 `postgres-data` 数据卷已经存在也会运行。迁移失败时 API 不会启动。

停止服务但保留数据库数据：

```bash
docker compose down
```

如果还要删除本地 PostgreSQL 数据，并在下次启动时重新初始化：

```bash
docker compose down -v
```

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
$env:CASE_TEST_POSTGRES_URI = "postgresql://user:password@localhost:5432/refund_agent"
uv run pytest -m postgres tests/integration_tests/test_postgres_case_repository.py
```

不要让 `CASE_TEST_POSTGRES_URI` 指向生产数据库。测试只会创建并清理 thread ID 带有唯一 `case-integration-` 前缀的数据。GitHub Actions 会启动一次性的 PostgreSQL 16 服务，并在两个受支持的 Python 版本上执行这些测试。

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
- 生产服务必须验证用户身份，并确认订单确实属于发起请求的用户。
- 上线前需要补充权限控制、审计日志、监控、限流和加密密钥管理，并接入真实订单服务。
- 退款规则和人工审核金额阈值需要由相应的业务及法务人员确认。
- 风险和大额退款交接已经持久化为人工工单，但尚未连接外部客户客服队列适配器。
- v0.4 内部工单 API 暂未实现应用层鉴权。Compose 只把它绑定到
  `127.0.0.1`；在 Agent Server 或网关配置身份认证和权限控制之前，不应将
  该接口暴露到外部网络。
- 真人请求和正式投诉识别依赖 LLM 结构化输出，上线前仍需使用有代表性的多语言对话进行评测。
- 内置风险规则是有意保持保守的演示数据，并不是完整的安全或合规词库。
- 语义风险分类取决于所配置的模型，可能因措辞或语言不同而产生差异。投入生产环境前，应由专业的安全、法务和合规人员验证规则、提示词和回复内容。

## 许可证

本项目使用 MIT License，详见 `LICENSE`。
