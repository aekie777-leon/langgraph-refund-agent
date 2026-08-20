"""OpenAI-compatible model factories for workflow nodes."""

import os
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from agent.schemas import (
    FormalComplaintDetection,
    OperationRequestExtraction,
    OrderDetection,
    Route,
    SemanticRiskDetection,
)
from agent.showcase import showcase_enabled, validate_showcase_environment
from agent.showcase.scenario_models import (
    ShowcaseComplaintClassifier,
    ShowcaseComplaintModel,
    ShowcaseOperationExtractor,
    ShowcaseOrderDetector,
    ShowcaseRiskClassifier,
    ShowcaseRouter,
)

load_dotenv()


def get_llm() -> Any:
    """Create the configured OpenAI-compatible chat model."""
    validate_showcase_environment()
    if showcase_enabled():
        return ShowcaseComplaintModel()
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", api_key),
            ("OPENAI_MODEL", model),
        )
        if not value
    ]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {names}")

    client_options: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
    }
    if base_url := os.getenv("OPENAI_BASE_URL"):
        client_options["base_url"] = base_url

    return ChatOpenAI(**client_options)


def get_order_detector() -> Any:
    """Create the structured-output model used to detect order numbers."""
    validate_showcase_environment()
    if showcase_enabled():
        return ShowcaseOrderDetector()
    return get_llm().with_structured_output(OrderDetection)


def get_router() -> Any:
    """Create the structured-output model used for intent routing."""
    validate_showcase_environment()
    if showcase_enabled():
        return ShowcaseRouter()
    return get_llm().with_structured_output(Route)


def get_risk_classifier() -> Any:
    """Create the structured-output semantic risk classifier."""
    validate_showcase_environment()
    if showcase_enabled():
        return ShowcaseRiskClassifier()
    return get_llm().with_structured_output(SemanticRiskDetection)


def get_formal_complaint_classifier() -> Any:
    """Create the structured-output formal-complaint classifier."""
    validate_showcase_environment()
    if showcase_enabled():
        return ShowcaseComplaintClassifier()
    return get_llm().with_structured_output(FormalComplaintDetection)


def get_operation_request_extractor() -> Any:
    """Create the structured-output model for narrow operation extraction."""
    validate_showcase_environment()
    if showcase_enabled():
        return ShowcaseOperationExtractor()
    return get_llm().with_structured_output(OperationRequestExtraction)
