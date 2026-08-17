"""Unit tests for scope-to-visibility translation."""

import pytest

from agent.auth.models import AccessScope
from agent.auth.visibility import (
    ForbiddenError,
    case_visibility,
    customer_owned_visibility,
)
from tests.fakes.identity import make_scope


def test_customer_case_visibility_filters_by_tenant_and_customer() -> None:
    scope = make_scope("customer", user_id="customer-a", tenant_id="tenant-demo")

    assert case_visibility(scope) == (
        "tenant_id = %s AND customer_id = %s",
        ("tenant-demo", "customer-a"),
    )


def test_support_agent_case_visibility_filters_by_assignment() -> None:
    scope = make_scope("support_agent", user_id="agent-7", tenant_id="tenant-demo")

    assert case_visibility(scope) == (
        "tenant_id = %s AND assigned_agent_id = %s",
        ("tenant-demo", "agent-7"),
    )


def test_supervisor_case_visibility_filters_by_tenant_only() -> None:
    scope = make_scope("supervisor", user_id="sup-1", tenant_id="tenant-demo")

    assert case_visibility(scope) == ("tenant_id = %s", ("tenant-demo",))


def test_customer_owned_visibility_filters_by_tenant_and_customer() -> None:
    scope = make_scope("customer", user_id="customer-a", tenant_id="tenant-demo")

    assert customer_owned_visibility(scope) == (
        "tenant_id = %s AND customer_id = %s",
        ("tenant-demo", "customer-a"),
    )


def test_customer_owned_visibility_rejects_non_customers() -> None:
    scope = make_scope("support_agent", user_id="agent-7", tenant_id="tenant-demo")

    with pytest.raises(ForbiddenError):
        customer_owned_visibility(scope)


def test_case_visibility_does_not_trust_role_without_matching_permission() -> None:
    scope = AccessScope(
        user_id="sup-1",
        tenant_id="tenant-demo",
        role="supervisor",
        permissions=frozenset({"cases:read:own"}),
    )

    with pytest.raises(ForbiddenError):
        case_visibility(scope)
