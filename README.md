# LangGraph Refund Agent

A small customer-service assistant built with LangGraph. It classifies refund requests, order inquiries, and complaints; keeps refund decisions deterministic; asks for confirmation before an automatic refund; and stores refund requests in PostgreSQL.

Version: `0.2.0`

## Features

- Structured order-number detection with an OpenAI-compatible chat model
- Structured intent routing for refunds, order inquiries, and complaints
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
    -> classify intent
       -> complaint
          -> return a concise customer-service response
       -> order inquiry
          -> detect and find the order
          -> return order status and product information
       -> refund request
          -> detect and find the order
          -> check deterministic refund policy
             -> reject ineligible order
             -> route large refund to customer service
             -> ask for user confirmation
                -> create one PostgreSQL refund request
                -> cancel
```

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
uv build
```

The integration tests replace the intent router, order detector, and complaint model with offline fakes and do not connect to PostgreSQL. No API key or database is required to run the test suite.

## Project layout

```text
.
|-- .github/workflows/ci.yml
|-- compose.yaml
|-- Dockerfile
|-- scripts/init_db.sql
|-- src/agent/
|   |-- data/orders.json
|   |-- tools/
|   `-- graph.py
|-- tests/
|-- .env.example
|-- langgraph.json
`-- pyproject.toml
```

## Security and production notes

- Never commit `.env` or real credentials. The repository ignores `.env` by default.
- The included order database is demonstration data, not customer data.
- A production service must authenticate users and verify that an order belongs to the requesting user.
- Add authorization, audit logging, monitoring, rate limits, encrypted secret management, and a real order service before production use.
- Review the refund policy and manual-review threshold with the responsible business and legal teams.

## License

Released under the MIT License. See `LICENSE`.

---

# LangGraph 退款 Agent

这是一个使用 LangGraph 构建的小型客服助手。它可以识别退款申请、订单查询和投诉，使用确定性规则判断退款资格，在自动退款前请求用户确认，并把退款申请保存到 PostgreSQL。

版本：`0.2.0`

## 功能

- 使用兼容 OpenAI 接口的聊天模型识别订单号
- 使用结构化输出区分退款申请、订单查询和投诉
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
    -> 识别意图
       -> 投诉
          -> 返回简洁的客服回复
       -> 订单查询
          -> 识别并查询订单
          -> 返回订单状态和商品信息
       -> 退款申请
          -> 识别并查询订单
          -> 检查确定性退款规则
             -> 拒绝不符合条件的订单
             -> 大额退款转客服人工处理
             -> 请求用户确认
                -> 在 PostgreSQL 中创建一条退款申请
                -> 取消退款
```

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
uv build
```

集成测试会用离线实现替换意图路由、订单号识别和投诉回复模型，而且不会连接 PostgreSQL，因此运行测试不需要 API 密钥或数据库。

## 项目结构

```text
.
|-- .github/workflows/ci.yml
|-- compose.yaml
|-- Dockerfile
|-- scripts/init_db.sql
|-- src/agent/
|   |-- data/orders.json
|   |-- tools/
|   `-- graph.py
|-- tests/
|-- .env.example
|-- langgraph.json
`-- pyproject.toml
```

## 安全与生产环境说明

- 不要提交 `.env` 或任何真实密钥；仓库已经默认忽略 `.env`。
- 内置订单只是演示数据，不能保存真实客户信息。
- 生产服务必须验证用户身份，并确认订单确实属于发起请求的用户。
- 上线前需要补充权限控制、审计日志、监控、限流和加密密钥管理，并接入真实订单服务。
- 退款规则和人工审核金额阈值需要由相应的业务及法务人员确认。

## 许可证

本项目使用 MIT License，详见 `LICENSE`。
