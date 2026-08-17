# Internal support-case API

The v0.4 custom FastAPI application exposes a small operational API alongside
the LangGraph Agent Server routes. It is intended for an internal console or a
future customer-service adapter, not as a public customer API.

From v0.6 the API requires a valid bearer token (`401` without credentials)
and enforces role-based permissions (`403` for permission violations, `404`
for inaccessible resources).

Base path: `/internal/support-cases`

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /internal/support-cases` | List and filter cases |
| `GET /internal/support-cases/{case_id}` | Get one case |
| `GET /internal/support-cases/{case_id}/events` | Read immutable audit events |
| `POST /internal/support-cases/{case_id}/status` | Change status idempotently |
| `POST /internal/support-cases/{case_id}/assign` | Assign a case to a support agent (supervisor only) |

The list endpoint accepts `status`, `priority`, `case_type`, `thread_id`,
`order_id`, `limit` (1–100), and `offset`. Results are ordered by operational
priority (`p0` first), creation time, and case ID. Event pages accept `limit`
(1–200) and `offset`.

## Authentication and roles

Every request must include `Authorization: Bearer <token>`. The demo provider
resolves tokens from `DEMO_IDENTITY_TOKENS`; permissions are derived from the
role:

| Role | Can do |
| --- | --- |
| `customer` | Read their own cases |
| `support_agent` | Read and update cases assigned to them |
| `supervisor` | Read/update the whole tenant queue and assign cases |

Missing credentials return `401`; an authenticated caller without the required
permission returns `403`; a single-resource request for a case the caller
cannot see returns `404` (indistinguishable from a missing case).

## Status request

```json
{
  "target_status": "in_progress",
  "request_id": "console-20260815-0001"
}
```

`request_id` is the idempotency key for this operation. Retrying the same
request does not create a duplicate status event. A transition to `on_hold`
must include `on_hold_reason`; that field is rejected for every other target
status. The event actor is derived from the authenticated identity and cannot
be overridden by the request body.

```json
{
  "target_status": "on_hold",
  "request_id": "console-20260815-0002",
  "on_hold_reason": "waiting_customer"
}
```

Allowed `on_hold_reason` values are `waiting_customer`,
`waiting_external_system`, `waiting_internal_team`, `system_unavailable`,
`force_majeure`, and `other`.

## Assign request

```json
{
  "agent_id": "support-agent-42",
  "request_id": "console-20260817-0001"
}
```

`request_id` is the idempotency key. Retrying the same request does not create
a duplicate `assigned` event; assigning the same agent again is a
`status_unchanged` no-op. `agent_id` must be non-empty, at most 128
characters, free of `:`, and must not use the reserved values `system` or
`legacy`. The demo provider does not verify that the agent exists in a user
directory; production must resolve assignees against the real identity system.

The valid lifecycle remains:

```text
open -> in_progress
in_progress -> on_hold | resolved
on_hold -> in_progress | resolved
resolved -> open
```

## Responses and errors

List endpoints return `items`, `total`, `limit`, and `offset`. A successful
status operation returns its action, current case, and audit event. Expected
errors use a stable envelope:

```json
{
  "error": {
    "code": "invalid_case_status_transition",
    "message": "The requested support-case status transition is not allowed."
  }
}
```

| HTTP status | Meaning |
| --- | --- |
| `401` | Missing or invalid credentials |
| `403` | Authenticated but not permitted |
| `404` | The case does not exist or is not accessible |
| `409` | Invalid transition or a concurrent update conflict |
| `422` | Invalid UUID, query value, or request body |
| `503` | Case storage is unavailable |

Storage errors intentionally do not expose database connection details.

## Local test example

```powershell
$caseId = "00000000-0000-0000-0000-000000000000"
$token = "demo-supervisor-token"
curl.exe "http://127.0.0.1:8000/internal/support-cases?priority=p0&limit=20" -H "Authorization: Bearer $token"
curl.exe "http://127.0.0.1:8000/internal/support-cases/$caseId/events" -H "Authorization: Bearer $token"
curl.exe -X POST "http://127.0.0.1:8000/internal/support-cases/$caseId/assign" -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d '{\"agent_id\": \"support-agent-42\", \"request_id\": \"console-20260817-0001\"}'
```

The endpoints also appear in `/docs` under **Internal Support Cases**.

## Security boundary

From v0.6 the API is authenticated and role-protected. The supplied Compose
configuration binds the API to `127.0.0.1`. Do not expose these routes outside
a trusted local environment without additional gateway controls such as rate
limiting.

---

# 内部客服工单 API

v0.4 的自定义 FastAPI 应用会在 LangGraph Agent Server 路由旁边提供一组精简的
运营接口。它面向内部运营控制台或未来的客服系统适配器，不是公开的客户 API。

自 v0.6 起，该 API 需要有效的 Bearer Token（无凭据返回 401），并按角色执行
权限控制（权限不足返回 403，资源不可见返回 404）。

基础路径：`/internal/support-cases`

## 接口

| 方法和路径 | 用途 |
| --- | --- |
| `GET /internal/support-cases` | 查询并筛选工单 |
| `GET /internal/support-cases/{case_id}` | 查询单个工单 |
| `GET /internal/support-cases/{case_id}/events` | 读取不可变审计事件 |
| `POST /internal/support-cases/{case_id}/status` | 幂等修改状态 |
| `POST /internal/support-cases/{case_id}/assign` | 把工单分配给客服（仅 supervisor） |

列表接口支持 `status`、`priority`、`case_type`、`thread_id`、`order_id`、
`limit`（1–100）和 `offset`。结果先按运营优先级排序（`p0` 最前），再按创建
时间和工单 ID 排序。事件列表支持 `limit`（1–200）和 `offset`。

## 认证与角色

每个请求都必须携带 `Authorization: Bearer <token>`。demo 提供者从
`DEMO_IDENTITY_TOKENS` 解析 Token，权限由角色派生：

| 角色 | 可执行操作 |
| --- | --- |
| `customer` | 读取自己的工单 |
| `support_agent` | 读取和修改分配给自己（`assigned_agent_id`）的工单 |
| `supervisor` | 读取/修改整个租户队列，并可分配工单 |

缺少凭据返回 `401`；已认证但权限不足返回 `403`；对调用者不可见的单个资源
请求返回 `404`（与"工单不存在"无法区分）。

## 状态请求

```json
{
  "target_status": "in_progress",
  "request_id": "console-20260815-0001"
}
```

`request_id` 是本次操作的幂等键。使用相同 ID 重试不会重复创建状态事件。
修改为 `on_hold` 时必须提供 `on_hold_reason`，其他目标状态不允许携带该字段。
事件中的操作人由已认证身份生成，客户端不能在请求体中覆盖。

```json
{
  "target_status": "on_hold",
  "request_id": "console-20260815-0002",
  "on_hold_reason": "waiting_customer"
}
```

`on_hold_reason` 的合法值为 `waiting_customer`、
`waiting_external_system`、`waiting_internal_team`、`system_unavailable`、
`force_majeure` 和 `other`。

## 分配请求

```json
{
  "agent_id": "support-agent-42",
  "request_id": "console-20260817-0001"
}
```

`request_id` 是幂等键。使用相同 ID 重试不会重复创建 `assigned` 事件；再次
分配给同一客服是 `status_unchanged` 空操作。`agent_id` 必须非空、不超过
128 个字符、不含 `:`，且不能使用保留值 `system` 或 `legacy`。demo 提供者
不验证该客服是否真实存在于用户目录；生产环境必须对接真实身份系统解析
被分配人。

有效状态生命周期保持不变：

```text
open -> in_progress
in_progress -> on_hold | resolved
on_hold -> in_progress | resolved
resolved -> open
```

## 返回和错误

列表接口返回 `items`、`total`、`limit` 和 `offset`。状态修改成功时返回操作
类型、当前工单和审计事件。预期错误使用稳定结构：

```json
{
  "error": {
    "code": "invalid_case_status_transition",
    "message": "The requested support-case status transition is not allowed."
  }
}
```

| HTTP 状态 | 含义 |
| --- | --- |
| `401` | 缺少或无效的凭据 |
| `403` | 已认证但权限不足 |
| `404` | 工单不存在或不可访问 |
| `409` | 状态转换无效或发生并发更新冲突 |
| `422` | UUID、查询参数或请求体无效 |
| `503` | 工单存储暂时不可用 |

存储错误不会暴露数据库连接信息。

## 本地测试示例

```powershell
$caseId = "00000000-0000-0000-0000-000000000000"
$token = "demo-supervisor-token"
curl.exe "http://127.0.0.1:8000/internal/support-cases?priority=p0&limit=20" -H "Authorization: Bearer $token"
curl.exe "http://127.0.0.1:8000/internal/support-cases/$caseId/events" -H "Authorization: Bearer $token"
curl.exe -X POST "http://127.0.0.1:8000/internal/support-cases/$caseId/assign" -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d '{\"agent_id\": \"support-agent-42\", \"request_id\": \"console-20260817-0001\"}'
```

这些接口也会显示在 `/docs` 的 **Internal Support Cases** 分组中。

## 安全边界

自 v0.6 起该 API 已启用认证与角色保护。项目提供的 Compose 配置只绑定
`127.0.0.1`。在没有额外的网关控制（如限流）之前，不要把这些接口暴露到
可信本地环境之外。
