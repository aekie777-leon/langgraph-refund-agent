"""Unit tests for the application-scoped operation dependencies."""

from unittest.mock import Mock

import pytest

from agent.operations.runtime import (
    clear_operation_dependencies,
    configure_operation_dependencies,
    get_operation_service,
    get_order_provider,
)
from agent.operations.service import OperationService


@pytest.fixture(autouse=True)
def reset_operation_dependencies():
    clear_operation_dependencies()
    yield
    clear_operation_dependencies()


def test_configured_operation_dependencies_can_be_resolved() -> None:
    provider = Mock()
    service = Mock(spec=OperationService)

    configure_operation_dependencies(order_provider=provider, operation_service=service)

    assert get_order_provider() is provider
    assert get_operation_service() is service


def test_operation_dependencies_cannot_be_configured_twice() -> None:
    configure_operation_dependencies(order_provider=Mock(), operation_service=Mock())

    with pytest.raises(RuntimeError, match="already been configured"):
        configure_operation_dependencies(order_provider=Mock(), operation_service=Mock())


def test_unconfigured_operation_dependency_fails_clearly() -> None:
    with pytest.raises(RuntimeError, match="startup has not completed"):
        get_order_provider()
