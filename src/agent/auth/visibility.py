"""Translate an access scope into repository visibility predicates."""

from agent.auth.models import AccessScope


class ForbiddenError(RuntimeError):
    """Report an authenticated caller that lacks permission for the action."""


def case_visibility(
    scope: AccessScope,
    *,
    prefix: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Return a SQL predicate and parameters scoping support cases."""
    column = f"{prefix}." if prefix else ""
    if "cases:read:all" in scope.permissions:
        return (f"{column}tenant_id = %s", (scope.tenant_id,))
    if "cases:read:assigned" in scope.permissions:
        return (
            f"{column}tenant_id = %s AND {column}assigned_agent_id = %s",
            (scope.tenant_id, scope.user_id),
        )
    if "cases:read:own" in scope.permissions:
        if scope.customer_id is None:
            raise ForbiddenError("own-case access requires a customer identity")
        return (
            f"{column}tenant_id = %s AND {column}customer_id = %s",
            (scope.tenant_id, scope.customer_id),
        )
    raise ForbiddenError("the caller cannot read support cases")


def customer_owned_visibility(
    scope: AccessScope,
    *,
    prefix: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Return a predicate for records owned by one customer."""
    column = f"{prefix}." if prefix else ""
    if scope.customer_id is None:
        raise ForbiddenError("only customers may access customer-owned records")
    return (
        f"{column}tenant_id = %s AND {column}customer_id = %s",
        (scope.tenant_id, scope.customer_id),
    )
