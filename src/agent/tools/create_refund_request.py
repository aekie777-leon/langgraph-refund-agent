"""PostgreSQL-backed refund request creation tool."""

import datetime as dt
import os
import uuid
from typing import Any

import psycopg
from dotenv import load_dotenv
from langchain_core.tools import tool
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, Field

load_dotenv()


def _database_uri() -> str:
    """Build a safe PostgreSQL connection string from the environment."""
    if uri := os.getenv("POSTGRES_URI"):
        return uri

    environment = {
        "POSTGRES_USER": os.getenv("POSTGRES_USER"),
        "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD"),
        "POSTGRES_HOST": os.getenv("POSTGRES_HOST"),
        "POSTGRES_PORT": os.getenv("POSTGRES_PORT", "5432"),
        "POSTGRES_DB": os.getenv("POSTGRES_DB"),
    }
    missing = [name for name, value in environment.items() if not value]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing PostgreSQL configuration: {names}")

    return make_conninfo(
        user=environment["POSTGRES_USER"],
        password=environment["POSTGRES_PASSWORD"],
        host=environment["POSTGRES_HOST"],
        port=environment["POSTGRES_PORT"],
        dbname=environment["POSTGRES_DB"],
    )


def _existing_request(order_id: str) -> tuple[Any, str] | None:
    """Return an existing refund request for an order, if present."""
    with psycopg.connect(_database_uri()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT refund_id, status
                FROM refund_requests
                WHERE order_id = %s
                """,
                (order_id,),
            )
            return cursor.fetchone()


def _save_refund_request(refund_data: dict[str, Any]) -> bool:
    """Insert a request atomically and return whether it was created."""
    with psycopg.connect(_database_uri()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO refund_requests
                    (refund_id, order_id, status, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING
                RETURNING refund_id
                """,
                (
                    refund_data["refund_id"],
                    refund_data["order_id"],
                    refund_data["status"],
                    refund_data["created_at"],
                ),
            )
            return cursor.fetchone() is not None


class CreateRefundRequest(BaseModel):
    """Validate refund request creation input."""

    order_id: str = Field(pattern=r"^ORD-\d{5}$", description="Order number")


@tool(args_schema=CreateRefundRequest)
def create_refund_request(order_id: str) -> dict[str, Any]:
    """Create one pending refund request after policy and user approval."""
    refund_data = {
        "refund_id": str(uuid.uuid4()),
        "order_id": order_id,
        "status": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc),
    }

    try:
        created = _save_refund_request(refund_data)
        if created:
            return {"success": True, **refund_data}

        existing = _existing_request(order_id)
    except psycopg.Error as error:
        raise RuntimeError("Failed to save refund request.") from error

    result: dict[str, Any] = {
        "success": False,
        "status": "already_exists",
        "order_id": order_id,
        "message": "A refund request already exists for this order.",
    }
    if existing is not None:
        refund_id, status = existing
        result.update(
            refund_id=str(refund_id),
            refund_status=status,
        )
    return result
