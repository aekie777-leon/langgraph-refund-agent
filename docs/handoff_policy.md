# Support-case handoff policy

This document records the deterministic domain rules and current LangGraph
handoff integration implemented during v0.4 development. PostgreSQL persistence
is connected; an external support-platform adapter is not yet connected.

## Case types

| Case type | Purpose |
| --- | --- |
| `safety_review` | Self-harm and violence concerns |
| `business_escalation` | Legal, regulatory, reputation, and uncategorized risks |
| `refund_review` | Refunds that require manual review |
| `general_support` | A confirmed request for human support |
| `staff_conduct_complaint` | A complaint about staff conduct |
| `other_complaint` | Another explicit formal complaint |

When one message supplies multiple reasons, only one primary case is selected.
The type precedence is:

```text
safety_review
> staff_conduct_complaint
> business_escalation
> refund_review
> general_support
> other_complaint
```

The priority is selected independently as the most urgent priority supplied by
all reasons. All reason codes are retained.

## Priority policy

| Condition | Type | Priority |
| --- | --- | --- |
| Hard-critical self-harm or violence | `safety_review` | `p0` |
| High or critical semantic self-harm or violence | `safety_review` | `p0` |
| Medium semantic self-harm or violence | `safety_review` | `p2` |
| Hard, high, or critical legal/regulatory/reputation/other risk | `business_escalation` | `p1` |
| Medium legal/regulatory/reputation/other risk | `business_escalation` | `p2` |
| Refund manual review | `refund_review` | `p1` |
| Confirmed request for a human | `general_support` | `p2` |
| Critical staff conduct | `staff_conduct_complaint` | `p0` |
| High or medium staff conduct | `staff_conduct_complaint` | `p1` |
| Low staff conduct | `staff_conduct_complaint` | `p2` |
| Explicit other formal complaint | `other_complaint` | `p3` |
| Low risk, ordinary dissatisfaction, or no genuine risk | No case | — |

The policy consumes structured staff severity and a confirmed human-handoff
flag. The graph now obtains staff severity through a structured complaint
classifier and obtains the handoff flag only after an explicit confirmation
interrupt. Ordinary dissatisfaction does not set either case-creating fact.

## Status lifecycle

Cases use `open`, `in_progress`, `on_hold`, and `resolved`.

```text
open -> in_progress
in_progress -> on_hold | resolved
on_hold -> in_progress | resolved
resolved -> open
```

Repeating the current status is an idempotent no-op. Other transitions are
rejected. A later persistence phase must record an `on_hold` reason.

## Thread merge rule

A new trigger is appended to an existing case only when the `thread_id` and
`case_type` match and the existing case is `open`, `in_progress`, or `on_hold`.
A resolved case or a different case type requires a new case. When merging,
priority may be upgraded but must not be downgraded.

## Operational access

The internal API exposes case lookup, filtered listing, event history, and
idempotent status changes at `/internal/support-cases`. It reuses the same
`CaseService` and repository as the graph handoff node, so API operations cannot
bypass lifecycle validation or event recording. See `internal_case_api.md` for
the HTTP contract.

---

# 客服工单转人工策略

本文档记录 v0.4 开发期间实现的确定性领域规则和当前 LangGraph 工单交接。
PostgreSQL 持久化已经接入，但尚未连接外部客服平台适配器。

## 工单类型

| 工单类型 | 用途 |
| --- | --- |
| `safety_review` | 自伤和暴力风险 |
| `business_escalation` | 法律、监管、声誉及其他无法分类的风险 |
| `refund_review` | 需要人工审核的退款 |
| `general_support` | 用户确认要求真人客服 |
| `staff_conduct_complaint` | 针对工作人员行为的投诉 |
| `other_complaint` | 其他明确提出的正式投诉 |

一条消息出现多个原因时，只选择一个主工单。类型优先顺序为：

```text
safety_review
> staff_conduct_complaint
> business_escalation
> refund_review
> general_support
> other_complaint
```

priority 独立取所有原因中最高的优先级，并保留全部 reason codes。

## 优先级策略

| 条件 | 类型 | 优先级 |
| --- | --- | --- |
| hard critical 自伤或暴力 | `safety_review` | `p0` |
| semantic high/critical 自伤或暴力 | `safety_review` | `p0` |
| semantic medium 自伤或暴力 | `safety_review` | `p2` |
| hard/high/critical 法律、监管、声誉或其他风险 | `business_escalation` | `p1` |
| medium 法律、监管、声誉或其他风险 | `business_escalation` | `p2` |
| 大额退款人工审核 | `refund_review` | `p1` |
| 已确认的真人客服请求 | `general_support` | `p2` |
| critical 工作人员行为投诉 | `staff_conduct_complaint` | `p0` |
| high/medium 工作人员行为投诉 | `staff_conduct_complaint` | `p1` |
| low 工作人员行为投诉 | `staff_conduct_complaint` | `p2` |
| 其他明确的正式投诉 | `other_complaint` | `p3` |
| low 风险、普通表达不满或无真实风险 | 不创建 | — |

policy 只接收已经结构化的工作人员投诉严重程度和人工确认结果。Graph 现在
通过结构化投诉分类器获得人员投诉严重程度，并且只有在明确的确认 interrupt
完成后才设置真人交接标志。普通表达不满不会产生这两类建单事实。

## 状态生命周期

工单使用 `open`、`in_progress`、`on_hold` 和 `resolved`：

```text
open -> in_progress
in_progress -> on_hold | resolved
on_hold -> in_progress | resolved
resolved -> open
```

重复设置当前状态视为幂等操作。其他状态转换会被拒绝。后续持久化阶段必须为
`on_hold` 保存挂起原因。

## Thread 合并规则

只有 `thread_id`、`case_type` 相同，且已有工单状态为 `open`、
`in_progress` 或 `on_hold` 时，才把新触发事件追加到已有工单。已有工单为
`resolved` 或工单类型不同时，必须创建新工单。合并时只允许提升 priority，
不允许自动降低。

## 运营访问

内部 API 在 `/internal/support-cases` 下提供工单查询、筛选列表、事件历史和
幂等状态变更。它与 Graph 交接节点复用同一个 `CaseService` 和 Repository，
因此 API 操作不能绕过状态生命周期校验或事件记录。HTTP 契约详见
`internal_case_api.md`。
