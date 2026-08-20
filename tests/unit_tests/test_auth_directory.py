"""Unit tests for the narrow personnel-directory domain contract."""

import pytest
from pydantic import ValidationError

from agent.auth.directory import DirectoryUser


def test_directory_user_contains_only_assignment_eligibility_fields() -> None:
    user = DirectoryUser(
        user_id="agent-7",
        tenant_id="tenant-demo",
        active=True,
        roles=frozenset({"support_agent"}),
    )

    assert user.model_dump() == {
        "user_id": "agent-7",
        "tenant_id": "tenant-demo",
        "active": True,
        "roles": frozenset({"support_agent"}),
    }


def test_directory_user_preserves_identity_key_compatibility() -> None:
    with pytest.raises(ValidationError):
        DirectoryUser(
            user_id="agent:7",
            tenant_id="tenant-demo",
            active=True,
            roles=frozenset({"support_agent"}),
        )
