"""Contract tests for deterministic showcase model adapters."""

import pytest
from langchain_core.messages import HumanMessage

from agent.showcase.scenario_models import (
    ShowcaseComplaintClassifier,
    ShowcaseComplaintModel,
    ShowcaseOperationExtractor,
    ShowcaseOrderDetector,
    ShowcaseRiskClassifier,
    ShowcaseRouter,
)

pytestmark = pytest.mark.anyio


async def test_showcase_models_drive_a_cancellation_without_external_io() -> None:
    messages = [HumanMessage(content="Please cancel ORD-10008.")]

    order = await ShowcaseOrderDetector().ainvoke(messages)
    route = await ShowcaseRouter().ainvoke(messages)
    risk = await ShowcaseRiskClassifier().ainvoke(messages)
    operation = await ShowcaseOperationExtractor().ainvoke(messages)

    assert order.model_dump() == {"has_order_id": True, "order_id": "ORD-10008"}
    assert route.step == "cancellation_request"
    assert risk.risk_level == "none"
    assert operation.operation_type == "cancellation"
    assert operation.ambiguous is False


async def test_showcase_models_cover_chinese_formal_complaint() -> None:
    messages = [HumanMessage(content="我要正式投诉，客服辱骂了我")]

    route = await ShowcaseRouter().ainvoke(messages)
    complaint = await ShowcaseComplaintClassifier().ainvoke(messages)
    response = await ShowcaseComplaintModel().ainvoke(messages)

    assert route.step == "complaint"
    assert complaint.complaint_kind == "staff_conduct"
    assert complaint.staff_complaint_severity == "medium"
    assert "without promising" in str(response.content)


async def test_showcase_risk_result_is_structured_and_bounded() -> None:
    risk = await ShowcaseRiskClassifier().ainvoke(
        [HumanMessage(content="This is an immediate semantic danger.")]
    )

    assert risk.risk_level == "high"
    assert risk.categories == ["violence"]


async def test_showcase_operation_extractor_rejects_ambiguous_request() -> None:
    result = await ShowcaseOperationExtractor().ainvoke(
        [HumanMessage(content="Cancel and exchange ORD-10001")]
    )

    assert result.ambiguous is True
    assert result.operation_type is None
