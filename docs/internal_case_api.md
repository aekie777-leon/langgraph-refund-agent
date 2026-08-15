# Internal support-case API

The v0.4 custom FastAPI application exposes a small operational API alongside
the LangGraph Agent Server routes. It is intended for an internal console or a
future customer-service adapter, not as a public customer API.

Base path: `/internal/support-cases`

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /internal/support-cases` | List and filter cases |
| `GET /internal/support-cases/{case_id}` | Get one case |
| `GET /internal/support-cases/{case_id}/events` | Read immutable audit events |
| `POST /internal/support-cases/{case_id}/status` | Change status idempotently |

The list endpoint accepts `status`, `priority`, `case_type`, `thread_id`,
`order_id`, `limit` (1–100), and `offset`. Results are ordered by operational
priority (`p0` first), creation time, and case ID. Event pages accept `limit`
(1–200) and `offset`.

## Status request

```json
{
  "target_status": "in_progress",
  "request_id": "console-20260815-0001",
  "actor": "support-agent-42"
}
```

`request_id` is the idempotency key for this operation. Retrying the same
request does not create a duplicate status event. A transition to `on_hold`
must include `on_hold_reason`; that field is rejected for every other target
status.

```json
{
  "target_status": "on_hold",
  "request_id": "console-20260815-0002",
  "actor": "support-agent-42",
  "on_hold_reason": "waiting_customer"
}
```

Allowed `on_hold_reason` values are `waiting_customer`,
`waiting_external_system`, `waiting_internal_team`, `system_unavailable`,
`force_majeure`, and `other`.

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
| `404` | The case does not exist |
| `409` | Invalid transition or a concurrent update conflict |
| `422` | Invalid UUID, query value, or request body |
| `503` | Case storage is unavailable |

Storage errors intentionally do not expose database connection details.

## Local test example

```powershell
$caseId = "00000000-0000-0000-0000-000000000000"
curl.exe "http://127.0.0.1:8000/internal/support-cases?priority=p0&limit=20"
curl.exe "http://127.0.0.1:8000/internal/support-cases/$caseId/events"
```

The endpoints also appear in `/docs` under **Internal Support Cases**.

## Security boundary

The v0.4 API does not yet implement application-level authentication or
role-based authorization. The supplied Compose configuration binds the API to
`127.0.0.1`. Do not expose these routes outside a trusted local environment
until authentication, authorization, audit identity validation, rate limiting,
and network controls are configured at the LangGraph deployment or gateway.

---

# 内部客服工单 API

v0.4 的自定义 FastAPI 应用会在 LangGraph Agent Server 路由旁边提供一组精简的
运营接口。它面向内部运营控制台或未来的客服系统适配器，不是公开的客户 API。

基础路径：`/internal/support-cases`

## 接口

| 方法和路径 | 用途 |
| --- | --- |
| `GET /internal/support-cases` | 查询并筛选工单 |
| `GET /internal/support-cases/{case_id}` | 查询单个工单 |
| `GET /internal/support-cases/{case_id}/events` | 读取不可变审计事件 |
| `POST /internal/support-cases/{case_id}/status` | 幂等修改状态 |

列表接口支持 `status`、`priority`、`case_type`、`thread_id`、`order_id`、
`limit`（1–100）和 `offset`。结果先按运营优先级排序（`p0` 最前），再按创建
时间和工单 ID 排序。事件列表支持 `limit`（1–200）和 `offset`。

## 状态请求

```json
{
  "target_status": "in_progress",
  "request_id": "console-20260815-0001",
  "actor": "support-agent-42"
}
```

`request_id` 是本次操作的幂等键。使用相同 ID 重试不会重复创建状态事件。
修改为 `on_hold` 时必须提供 `on_hold_reason`，其他目标状态不允许携带该字段。

```json
{
  "target_status": "on_hold",
  "request_id": "console-20260815-0002",
  "actor": "support-agent-42",
  "on_hold_reason": "waiting_customer"
}
```

`on_hold_reason` 的合法值为 `waiting_customer`、
`waiting_external_system`、`waiting_internal_team`、`system_unavailable`、
`force_majeure` 和 `other`。

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
| `404` | 工单不存在 |
| `409` | 状态转换无效或发生并发更新冲突 |
| `422` | UUID、查询参数或请求体无效 |
| `503` | 工单存储暂时不可用 |

存储错误不会暴露数据库连接信息。

## 本地测试示例

```powershell
$caseId = "00000000-0000-0000-0000-000000000000"
curl.exe "http://127.0.0.1:8000/internal/support-cases?priority=p0&limit=20"
curl.exe "http://127.0.0.1:8000/internal/support-cases/$caseId/events"
```

这些接口也会显示在 `/docs` 的 **Internal Support Cases** 分组中。

## 安全边界

v0.4 暂未实现应用层身份认证和基于角色的权限控制。项目提供的 Compose 配置只
绑定 `127.0.0.1`。在 LangGraph 部署层或网关完成身份认证、授权、操作人身份
校验、限流和网络控制之前，不要把这些接口暴露到可信本地环境之外。
