"""OpenAI-compatible model factories for workflow nodes."""

import os
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from agent.schemas import OrderDetection, Route

load_dotenv()


def get_llm() -> ChatOpenAI:
    """Create the configured OpenAI-compatible chat model."""
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
    return get_llm().with_structured_output(OrderDetection)


def get_router() -> Any:
    """Create the structured-output model used for intent routing."""
    return get_llm().with_structured_output(Route)
