# LangGraph Refund Agent

A small, risk-aware customer-service assistant built with LangGraph. It classifies refund requests, order inquiries, and complaints; performs deterministic and semantic risk checks; keeps refund decisions deterministic; asks for confirmation before an automatic refund; and stores refund requests in PostgreSQL.

Version: `0.3.0`

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
                   -> normal complaint response or non-critical risk response
                -> order inquiry / refund request
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
```

## Risk handling

Risk rules live in `src/agent/data/risk_rules.json`. Each entry contains an ID, matching pattern, language, category, severity, and rule type. The matcher normalizes Unicode, apostrophes, letter case, and whitespace before applying conservative literal phrase matching.

- `hard_critical` rules represent explicit high-confidence danger. A match bypasses the semantic classifier and cannot be downgraded by the LLM.
- `risk_signals` provide context only. A signal is passed to the semantic classifier and does not independently decide the final severity or whether human handling is required.
- Messages without a hard-critical match are classified as `none`, `low`, `medium`, `high`, or `critical` using structured model output.
- A high or critical result ends the business flow with an appropriate safety or human-review response.
- A low or medium result may continue through business-intent routing. When an order inquiry or refund request is also present, the graph pauses and asks the user which concern to handle.

The rule matcher does not call an LLM, route the graph, or make a final human-review decision.

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

Create the database, then apply the included schema:

```bash
psql -d refund_agent -f scripts/init_db.sql
```

The schema places a unique constraint on `order_id`, preventing duplicate refund requests when concurrent calls target the same order.

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

Compose supplies `DATABASE_URI`, `POSTGRES_URI`, and `REDIS_URI` inside the API container. PostgreSQL is initialized from `scripts/init_db.sql` only when the `postgres-data` volume is created for the first time.

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

## Quality checks

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

The integration tests replace the intent router, order detector, semantic risk classifier, and response model with offline fakes and do not connect to PostgreSQL. No API key or database is required to run the test suite.

## Project layout

```text
.
|-- .github/workflows/ci.yml
|-- compose.yaml
|-- Dockerfile
|-- scripts/init_db.sql
|-- src/agent/
|   |-- nodes/
|   |   |-- complaints.py
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
|   `-- state.py
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
- `risk_requires_human_review` is currently a workflow state flag and response behavior; it is not connected to a real ticketing or human-queue system.
- The bundled risk rules are intentionally conservative demonstration data, not a complete safety or compliance vocabulary.
- Semantic risk classification depends on the configured model and may vary across wording or languages. Validate the policy, prompts, and responses with qualified safety, legal, and compliance reviewers before production use.

## License

Released under the MIT License. See `LICENSE`.

---

# LangGraph 退款 Agent

这是一个使用 LangGraph 构建的小型风险感知客服助手。它可以识别退款申请、订单查询和投诉，在 LLM 语义风险分类前执行确定性风险规则检测，使用确定性规则判断退款资格，在自动退款前请求用户确认，并把退款申请保存到 PostgreSQL。

版本：`0.3.0`

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
                   -> 普通投诉回复或非高危风险回复
                -> 订单查询 / 退款申请
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
```

## 风险处理

风险规则保存在 `src/agent/data/risk_rules.json`。每条规则包含 ID、匹配文本、语言、风险类别、严重程度和规则类型。matcher 会先规范化 Unicode、英文撇号、字母大小写和空白，再进行保守的字面短语匹配。

- `hard_critical` 表示含义明确且置信度高的危险表达。命中后会绕过语义分类器，LLM 不能将其降级。
- `risk_signals` 只提供上下文提示。signal 会交给语义分类器继续判断，本身不会直接决定最终风险级别或是否转人工。
- 未命中 hard critical 的消息由结构化模型输出分类为 `none`、`low`、`medium`、`high` 或 `critical`。
- `high` 和 `critical` 会结束业务流程，并返回相应的安全提示或人工审核回复。
- `low` 和 `medium` 可以继续识别业务意图。当消息同时包含订单查询或退款请求时，图会暂停并让用户选择要处理的问题。

规则 matcher 不调用 LLM，不包含图路由逻辑，也不直接决定最终是否转人工。

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

创建数据库后执行仓库中的建表脚本：

```bash
psql -d refund_agent -f scripts/init_db.sql
```

数据库会为 `order_id` 建立唯一约束，即使出现并发请求，同一订单也不会生成多条退款申请。

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

Compose 会自动为 API 容器设置 `DATABASE_URI`、`POSTGRES_URI` 和 `REDIS_URI`。`scripts/init_db.sql` 只会在第一次创建 `postgres-data` 数据卷时执行。

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

## 质量检查

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

集成测试会用离线实现替换意图路由、订单号识别、语义风险分类器和回复模型，而且不会连接 PostgreSQL，因此运行测试不需要 API 密钥或数据库。

## 项目结构

```text
.
|-- .github/workflows/ci.yml
|-- compose.yaml
|-- Dockerfile
|-- scripts/init_db.sql
|-- src/agent/
|   |-- nodes/
|   |   |-- complaints.py
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
|   `-- state.py
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
- `risk_requires_human_review` 目前只是工作流状态标记和回复行为，尚未连接真实工单或人工队列系统。
- 内置风险规则是有意保持保守的演示数据，并不是完整的安全或合规词库。
- 语义风险分类取决于所配置的模型，可能因措辞或语言不同而产生差异。投入生产环境前，应由专业的安全、法务和合规人员验证规则、提示词和回复内容。

## 许可证

本项目使用 MIT License，详见 `LICENSE`。
