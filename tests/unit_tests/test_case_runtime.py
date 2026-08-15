"""Unit tests for the application-scoped case service lifecycle."""

from unittest.mock import Mock

import pytest

from agent.cases.runtime import (
    clear_case_service,
    configure_case_service,
    get_case_service,
)
from agent.cases.service import CaseService


@pytest.fixture(autouse=True)
def reset_case_service():
    clear_case_service()
    yield
    clear_case_service()


def test_configured_case_service_can_be_resolved() -> None:
    service = Mock(spec=CaseService)

    configure_case_service(service)

    assert get_case_service() is service


def test_case_service_cannot_be_configured_twice() -> None:
    configure_case_service(Mock(spec=CaseService))

    with pytest.raises(RuntimeError, match="already been configured"):
        configure_case_service(Mock(spec=CaseService))


def test_unconfigured_case_service_fails_clearly() -> None:
    with pytest.raises(RuntimeError, match="startup has not completed"):
        get_case_service()
