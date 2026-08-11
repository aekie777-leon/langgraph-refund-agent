# LangGraph Refund Agent

A small human-in-the-loop refund assistant built with LangGraph. The agent finds an order, evaluates a deterministic refund policy, asks for confirmation before an automatic refund, and stores refund requests in PostgreSQL.

Version: `0.1.0`

## Features

- Structured order-number detection with an OpenAI-compatible chat model
- Deterministic refund-policy checks
- Human confirmation before an automatic refund is created
- Manual-review routing for refunds of 100 or more
- PostgreSQL persistence with idempotent request creation
- Offline unit and graph integration tests
- Bundled demonstration orders with dates relative to the current day

## Refund flow

```text
User message
    -> detect order number
    -> find order
    -> check refund policy
       -> reject ineligible order
       -> route large refund to customer service
       -> ask for user confirmation
          -> create one PostgreSQL refund request
          -> cancel
```

## Requirements

- Python 3.10 or newer
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

Fill in at least `OPENAI_API_KEY`, `OPENAI_MODEL`, and the PostgreSQL settings. `OPENAI_BASE_URL` can remain empty when the official OpenAI API is used.

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

The integration tests replace the model detector with an offline fake and do not connect to PostgreSQL. No API key or database is required to run the test suite.

## Project layout

```text
.
|-- .github/workflows/ci.yml
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

这是一个使用 LangGraph 构建、带人工确认环节的小型退款助手。它可以查询订单、执行确定性的退款规则、在自动退款前请求用户确认，并把退款申请保存到 PostgreSQL。

版本：`0.1.0`

## 功能

- 使用兼容 OpenAI 接口的聊天模型识别订单号
- 使用确定性规则检查退款资格
- 创建自动退款申请前要求用户确认
- 金额大于或等于 100 时转人工审核
- 使用 PostgreSQL 持久化，并保证同一订单不会重复创建退款申请
- 包含离线单元测试和图集成测试
- 演示订单使用相对日期，不会随着时间推移全部失效

## 退款流程

```text
用户消息
    -> 识别订单号
    -> 查询订单
    -> 检查退款规则
       -> 拒绝不符合条件的订单
       -> 大额退款转客服人工处理
       -> 请求用户确认
          -> 在 PostgreSQL 中创建一条退款申请
          -> 取消退款
```

## 环境要求

- Python 3.10 或更高版本
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

至少填写 `OPENAI_API_KEY`、`OPENAI_MODEL` 和 PostgreSQL 配置。使用 OpenAI 官方接口时，`OPENAI_BASE_URL` 可以留空。

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

集成测试会用离线实现替换模型检测器，而且不会连接 PostgreSQL，因此运行测试不需要 API 密钥或数据库。

## 项目结构

```text
.
|-- .github/workflows/ci.yml
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
