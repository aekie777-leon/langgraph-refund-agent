"""Define the persisted refund-request domain model."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

RefundStatus = Literal["pending", "approved", "rejected"]


class RefundRequest(BaseModel):
    """Represent one persisted refund request owned by a customer."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    refund_id: UUID
    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    status: RefundStatus
    customer_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    created_at: AwareDatetime
