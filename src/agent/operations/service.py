"""Coordinate durable order-operation requests and their audit events."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from agent.auth.models import AccessScope
from agent.integrations.models import ProviderCommandEnvelope
from agent.integrations.provider_failure import (
    ProviderQueueFailureCoordinator,
    ProviderQueueFailureResult,
)
from agent.operations.models import (
    OperationDecision,
    OperationRecordStatus,
    OperationServiceAction,
    OperationServiceResult,
    OrderOperation,
    OrderOperationEvent,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.provider import OrderProvider
from agent.operations.repository import (
    ActiveOrderOperationConflictError,
    ConcurrentOperationUpdateError,
    DuplicateOperationIdempotencyError,
    DuplicateOperationSourceMessageError,
    OperationNotFoundError,
    OperationRepository,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], UUID]

_ACTIVE_STATUSES = frozenset(
    {"pending_confirmation", "queued", "submitted", "processing", "manual_review"}
)
_ALLOWED_STATUS_TRANSITIONS: dict[OperationRecordStatus, frozenset[OperationRecordStatus]] = {
    "pending_confirmation": frozenset(
        {
            "queued",
            "submitted",
            "manual_review",
            "rejected",
            "cancelled_by_customer",
        }
    ),
    "queued": frozenset({"submitted", "manual_review", "rejected"}),
    "submitted": frozenset({"processing", "completed", "rejected"}),
    "processing": frozenset({"completed", "rejected"}),
    "manual_review": frozenset({"processing", "completed", "rejected"}),
    "completed": frozenset(),
    "rejected": frozenset(),
    "cancelled_by_customer": frozenset(),
}


def _utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


class InvalidOperationStatusTransition(ValueError):
    """Report an unsupported order-operation state transition."""


class OperationService:
    """Apply confirmation, idempotency, and audit rules to order operations."""

    def __init__(
        self,
        repository: OperationRepository,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = uuid4,
        max_write_attempts: int = 3,
        provider_queue_failure_coordinator: ProviderQueueFailureCoordinator | None = None,
    ) -> None:
        """Initialize the service with persistence and deterministic test hooks."""
        if max_write_attempts < 1:
            raise ValueError("max_write_attempts must be at least 1")
        self._repository = repository
        self._clock = clock
        self._id_factory = id_factory
        self._max_write_attempts = max_write_attempts
        self._provider_queue_failure_coordinator = provider_queue_failure_coordinator

    async def move_to_provider_manual_review(
        self, scope: AccessScope, *, operation_id: UUID, request_id: str
    ) -> ProviderQueueFailureResult:
        """Atomically record confirmed provider-configuration fallback."""
        request_id, _ = self._validate_request_metadata(request_id, scope.identity)
        if self._provider_queue_failure_coordinator is None:
            raise RuntimeError("provider queue failure coordinator is unavailable")
        return await self._provider_queue_failure_coordinator.move_to_manual_review(
            scope, operation_id=operation_id, request_id=request_id
        )

    async def get_operation(self, scope: AccessScope, operation_id: UUID) -> OrderOperation:
        """Return one operation or raise when it does not exist."""
        operation = await self._repository.get_operation(scope, operation_id)
        if operation is None:
            raise OperationNotFoundError(str(operation_id))
        return operation

    async def create_pending_operation(
        self,
        scope: AccessScope,
        *,
        request: OrderOperationRequest,
        snapshot: OrderSnapshot,
        decision: OperationDecision,
        request_excerpt: str,
    ) -> OperationServiceResult:
        """Persist a confirmation-gated operation from an eligible policy result."""
        if scope.customer_id is None:
            raise ValueError("only customers may create order operations")
        excerpt = request_excerpt.strip()
        if not excerpt:
            raise ValueError("request_excerpt must not be empty")
        if request.order_id != snapshot.order_id:
            raise ValueError("request and snapshot must have the same order_id")
        if decision.operation_type != request.operation_type:
            raise ValueError("decision and request must have the same operation_type")
        if decision.outcome not in ("eligible", "manual_review"):
            raise ValueError("only eligible or manual_review decisions can be persisted")
        if not decision.requires_confirmation:
            raise ValueError("persisted operations must require confirmation")

        for _attempt in range(self._max_write_attempts):
            duplicate = await self._repository.find_by_source_message(
                scope,
                thread_id=request.thread_id,
                source_message_id=request.source_message_id,
            )
            if duplicate is not None:
                return OperationServiceResult(
                    action="duplicate_ignored",
                    operation=duplicate,
                )

            active = await self._repository.find_active_by_order_id(
                scope,
                request.order_id,
            )
            if active is not None:
                return OperationServiceResult(
                    action="duplicate_ignored",
                    operation=active,
                )

            operation, event = self._build_pending_operation(
                scope,
                request=request,
                snapshot=snapshot,
                decision=decision,
                request_excerpt=excerpt,
            )
            try:
                await self._repository.create_operation_with_events(
                    scope,
                    operation=operation,
                    events=(event,),
                )
            except (
                ActiveOrderOperationConflictError,
                DuplicateOperationIdempotencyError,
                DuplicateOperationSourceMessageError,
            ):
                continue
            return OperationServiceResult(
                action="created",
                operation=operation,
                events=(event,),
            )

        duplicate = await self._repository.find_by_source_message(
            scope,
            thread_id=request.thread_id,
            source_message_id=request.source_message_id,
        )
        if duplicate is not None:
            return OperationServiceResult(
                action="duplicate_ignored",
                operation=duplicate,
            )
        active = await self._repository.find_active_by_order_id(scope, request.order_id)
        if active is not None:
            return OperationServiceResult(
                action="duplicate_ignored",
                operation=active,
            )
        raise ConcurrentOperationUpdateError(
            "Could not create the order operation after concurrent write conflicts"
        )

    async def confirm_operation(
        self,
        scope: AccessScope,
        *,
        operation_id: UUID,
        request_id: str,
    ) -> OperationServiceResult:
        """Record customer confirmation for an operation that needs manual review."""
        operation = await self.get_operation(scope, operation_id)
        if not operation.requires_manual_review:
            raise ValueError(
                "automatic operations must use submit_confirmed_operation"
            )
        return await self._change_from_pending(
            scope,
            operation_id=operation_id,
            request_id=request_id,
            target_status=None,
            action="confirmed",
        )

    async def submit_confirmed_operation(
        self,
        scope: AccessScope,
        *,
        operation_id: UUID,
        request_id: str,
        provider: OrderProvider,
    ) -> OperationServiceResult:
        """Submit one confirmed automatic operation with provider idempotency."""
        request_id = request_id.strip()
        actor = scope.identity
        if not request_id:
            raise ValueError("request_id must not be empty")
        idempotency_key = f"operation:{operation_id}:submitted:{request_id}"
        prior = await self._repository.find_event_by_idempotency_key(
            scope,
            idempotency_key,
        )
        if prior is not None:
            return await self._unchanged_result(scope, prior.operation_id)

        last_conflict: RuntimeError | None = None
        for _attempt in range(self._max_write_attempts):
            current = await self.get_operation(scope, operation_id)
            if current.requires_manual_review:
                raise ValueError("manual-review operations must not be submitted to a provider")
            if current.status == "submitted" and current.provider_reference is not None:
                return OperationServiceResult(action="status_unchanged", operation=current)
            if current.status != "pending_confirmation":
                raise InvalidOperationStatusTransition(
                    f"cannot submit an operation in {current.status!r}"
                )

            request = OrderOperationRequest(
                thread_id=current.thread_id,
                source_message_id=current.source_message_id,
                order_id=current.order_id,
                operation_type=current.operation_type,
                reason=current.request_reason_code,
                replacement_variant_id=current.replacement_variant_id,
                customer_id=current.customer_id,
                tenant_id=current.tenant_id,
            )
            provider_operation = await provider.submit_operation(
                request=request,
                expected_order_version=current.order_version,
                idempotency_key=f"provider:operation:{operation_id}",
            )
            updated, status_event = self._build_status_update(
                current=current,
                target_status="submitted",
                idempotency_key=f"{idempotency_key}:status",
                actor=actor,
                provider_reference=(
                    provider_operation.provider_reference or provider_operation.operation_id
                ),
            )
            confirmation_event = OrderOperationEvent(
                event_id=self._id_factory(),
                idempotency_key=idempotency_key,
                operation_id=current.operation_id,
                event_type="confirmation_recorded",
                actor=actor,
                customer_id=current.customer_id,
                tenant_id=current.tenant_id,
                created_at=updated.updated_at,
            )
            try:
                await self._repository.update_operation_with_events(
                    scope,
                    operation=updated,
                    events=(confirmation_event, status_event),
                    expected_version=current.version,
                )
            except (ConcurrentOperationUpdateError, DuplicateOperationIdempotencyError) as error:
                last_conflict = error
                prior = await self._repository.find_event_by_idempotency_key(
                    scope,
                    idempotency_key,
                )
                if prior is not None:
                    return await self._unchanged_result(scope, prior.operation_id)
                continue
            return OperationServiceResult(
                action="submitted",
                operation=updated,
                events=(confirmation_event, status_event),
            )

        raise ConcurrentOperationUpdateError(
            "Could not submit the operation after concurrent write conflicts"
        ) from last_conflict

    async def queue_confirmed_operation(
        self,
        scope: AccessScope,
        *,
        operation_id: UUID,
        request_id: str,
        connection_id: str,
    ) -> OperationServiceResult:
        """Queue a confirmed automatic operation and its outbox command atomically."""
        request_id, _ = self._validate_request_metadata(request_id, scope.identity)
        normalized_connection_id = connection_id.strip()
        if not normalized_connection_id:
            raise ValueError("connection_id must not be empty")
        current = await self.get_operation(scope, operation_id)
        if current.requires_manual_review:
            raise ValueError("manual-review operations must not enter the provider outbox")
        if current.status == "queued":
            return OperationServiceResult(action="status_unchanged", operation=current)
        if current.status != "pending_confirmation":
            raise InvalidOperationStatusTransition(
                f"cannot queue an operation in {current.status!r}"
            )
        now = self._clock()
        queued, status_event = self._build_status_update(
            current=current,
            target_status="queued",
            idempotency_key=f"operation:{operation_id}:queued:{request_id}",
            actor=scope.identity,
            provider_reference=None,
        )
        confirmation_event = OrderOperationEvent(
            event_id=self._id_factory(),
            idempotency_key=f"operation:{operation_id}:confirmed:{request_id}",
            operation_id=current.operation_id,
            event_type="confirmation_recorded",
            actor=scope.identity,
            customer_id=current.customer_id,
            tenant_id=current.tenant_id,
            created_at=now,
        )
        command = ProviderCommandEnvelope.for_order_operation(
            operation=queued,
            connection_id=normalized_connection_id,
            command_id=self._id_factory(),
            created_at=now,
        )
        await self._repository.queue_operation_with_events_and_command(
            scope,
            operation=queued,
            events=(confirmation_event, status_event),
            command=command,
            expected_version=current.version,
        )
        return OperationServiceResult(
            action="queued",
            operation=queued,
            events=(confirmation_event, status_event),
        )

    async def cancel_pending_operation(
        self,
        scope: AccessScope,
        *,
        operation_id: UUID,
        request_id: str,
    ) -> OperationServiceResult:
        """Record that the customer declined a not-yet-submitted operation."""
        return await self._change_from_pending(
            scope,
            operation_id=operation_id,
            request_id=request_id,
            target_status="cancelled_by_customer",
            action="cancelled",
        )

    async def update_operation_status(
        self,
        scope: AccessScope,
        *,
        operation_id: UUID,
        target_status: OperationRecordStatus,
        request_id: str,
        actor: str,
        provider_reference: str | None = None,
    ) -> OperationServiceResult:
        """Update a submitted operation and append a status-change audit event."""
        request_id, actor = self._validate_request_metadata(request_id, actor)
        if target_status == "pending_confirmation":
            raise InvalidOperationStatusTransition(
                "a persisted operation cannot return to pending_confirmation"
            )
        idempotency_key = f"operation:{operation_id}:status:{request_id}"
        prior = await self._repository.find_event_by_idempotency_key(
            scope,
            idempotency_key,
        )
        if prior is not None:
            return await self._unchanged_result(scope, prior.operation_id)

        last_conflict: RuntimeError | None = None
        for _attempt in range(self._max_write_attempts):
            current = await self.get_operation(scope, operation_id)
            if current.status == target_status:
                return OperationServiceResult(action="status_unchanged", operation=current)
            self._validate_status_transition(current.status, target_status)
            updated, event = self._build_status_update(
                current=current,
                target_status=target_status,
                idempotency_key=idempotency_key,
                actor=actor,
                provider_reference=provider_reference,
            )
            try:
                await self._repository.update_operation_with_events(
                    scope,
                    operation=updated,
                    events=(event,),
                    expected_version=current.version,
                )
            except (ConcurrentOperationUpdateError, DuplicateOperationIdempotencyError) as error:
                last_conflict = error
                prior = await self._repository.find_event_by_idempotency_key(
                    scope,
                    idempotency_key,
                )
                if prior is not None:
                    return await self._unchanged_result(scope, prior.operation_id)
                continue
            return OperationServiceResult(
                action="status_changed",
                operation=updated,
                events=(event,),
            )
        raise ConcurrentOperationUpdateError(
            "Could not update the operation after concurrent write conflicts"
        ) from last_conflict

    async def attach_support_case(
        self,
        scope: AccessScope,
        *,
        operation_id: UUID,
        support_case_id: UUID,
        request_id: str,
    ) -> OperationServiceResult:
        """Link the manual-review operation to the support case created for it."""
        request_id = request_id.strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        idempotency_key = f"operation:{operation_id}:support-case:{request_id}"
        prior = await self._repository.find_event_by_idempotency_key(
            scope,
            idempotency_key,
        )
        if prior is not None:
            return await self._unchanged_result(scope, prior.operation_id)

        last_conflict: RuntimeError | None = None
        for _attempt in range(self._max_write_attempts):
            current = await self.get_operation(scope, operation_id)
            if not current.requires_manual_review or current.status != "manual_review":
                raise ValueError("only a manual-review operation may receive a support case")
            if current.support_case_id is not None:
                if current.support_case_id == support_case_id:
                    return OperationServiceResult(
                        action="status_unchanged",
                        operation=current,
                    )
                raise ValueError("a manual-review operation already has a support case")
            now = self._clock()
            updated = current.model_copy(
                update={
                    "support_case_id": support_case_id,
                    "updated_at": now,
                    "version": current.version + 1,
                }
            )
            event = OrderOperationEvent(
                event_id=self._id_factory(),
                idempotency_key=idempotency_key,
                operation_id=current.operation_id,
                event_type="support_case_attached",
                support_case_id=support_case_id,
                actor="system",
                customer_id=current.customer_id,
                tenant_id=current.tenant_id,
                created_at=now,
            )
            try:
                await self._repository.update_operation_with_events(
                    scope,
                    operation=updated,
                    events=(event,),
                    expected_version=current.version,
                )
            except (ConcurrentOperationUpdateError, DuplicateOperationIdempotencyError) as error:
                last_conflict = error
                prior = await self._repository.find_event_by_idempotency_key(
                    scope,
                    idempotency_key,
                )
                if prior is not None:
                    return await self._unchanged_result(scope, prior.operation_id)
                continue
            return OperationServiceResult(
                action="support_case_attached",
                operation=updated,
                events=(event,),
            )
        raise ConcurrentOperationUpdateError(
            "Could not attach the support case after concurrent write conflicts"
        ) from last_conflict

    def _build_pending_operation(
        self,
        scope: AccessScope,
        *,
        request: OrderOperationRequest,
        snapshot: OrderSnapshot,
        decision: OperationDecision,
        request_excerpt: str,
    ) -> tuple[OrderOperation, OrderOperationEvent]:
        """Build an immutable initial operation and its creation event."""
        assert scope.customer_id is not None
        now = self._clock()
        operation_id = self._id_factory()
        recommendation = decision.case_recommendation
        operation = OrderOperation(
            operation_id=operation_id,
            idempotency_key=(
                f"operation:{request.thread_id}:{request.source_message_id}:created"
            ),
            thread_id=request.thread_id,
            source_message_id=request.source_message_id,
            order_id=request.order_id,
            operation_type=request.operation_type,
            request_reason_code=request.reason,
            policy_reason_codes=decision.reason_codes,
            display_reason=decision.display_reason,
            replacement_variant_id=request.replacement_variant_id,
            request_excerpt=request_excerpt,
            order_version=snapshot.version,
            amount=snapshot.amount,
            currency=snapshot.currency,
            requires_manual_review=decision.outcome == "manual_review",
            review_case_type=recommendation.case_type if recommendation else None,
            review_priority=recommendation.priority if recommendation else None,
            status="pending_confirmation",
            created_at=now,
            updated_at=now,
            customer_id=scope.customer_id,
            tenant_id=scope.tenant_id,
            created_by=scope.identity,
        )
        event = OrderOperationEvent(
            event_id=self._id_factory(),
            idempotency_key=operation.idempotency_key,
            operation_id=operation_id,
            event_type="operation_created",
            actor="system",
            customer_id=scope.customer_id,
            tenant_id=scope.tenant_id,
            created_at=now,
        )
        return operation, event

    async def _change_from_pending(
        self,
        scope: AccessScope,
        *,
        operation_id: UUID,
        request_id: str,
        target_status: OperationRecordStatus | None,
        action: OperationServiceAction,
    ) -> OperationServiceResult:
        """Handle the only two customer decisions available before submission."""
        request_id = request_id.strip()
        actor = scope.identity
        if not request_id:
            raise ValueError("request_id must not be empty")
        idempotency_key = f"operation:{operation_id}:{action}:{request_id}"
        prior = await self._repository.find_event_by_idempotency_key(
            scope,
            idempotency_key,
        )
        if prior is not None:
            return await self._unchanged_result(scope, prior.operation_id)

        last_conflict: RuntimeError | None = None
        for _attempt in range(self._max_write_attempts):
            current = await self.get_operation(scope, operation_id)
            if current.status != "pending_confirmation":
                if action == "confirmed" and current.status in _ACTIVE_STATUSES:
                    return OperationServiceResult(
                        action="status_unchanged",
                        operation=current,
                    )
                if action == "cancelled" and current.status == "cancelled_by_customer":
                    return OperationServiceResult(
                        action="status_unchanged",
                        operation=current,
                    )
                raise InvalidOperationStatusTransition(
                    f"cannot {action} an operation in {current.status!r}"
                )

            next_status = target_status
            if next_status is None:
                next_status = (
                    "manual_review" if current.requires_manual_review else "submitted"
                )
            updated, status_event = self._build_status_update(
                current=current,
                target_status=next_status,
                idempotency_key=f"{idempotency_key}:status",
                actor=actor,
                provider_reference=None,
            )
            events: tuple[OrderOperationEvent, ...]
            if action == "confirmed":
                decision_event = OrderOperationEvent(
                    event_id=self._id_factory(),
                    idempotency_key=idempotency_key,
                    operation_id=current.operation_id,
                    event_type="confirmation_recorded",
                    actor=actor,
                    customer_id=current.customer_id,
                    tenant_id=current.tenant_id,
                    created_at=updated.updated_at,
                )
                events = (decision_event, status_event)
            else:
                status_event = status_event.model_copy(
                    update={"idempotency_key": idempotency_key}
                )
                events = (status_event,)
            try:
                await self._repository.update_operation_with_events(
                    scope,
                    operation=updated,
                    events=events,
                    expected_version=current.version,
                )
            except (ConcurrentOperationUpdateError, DuplicateOperationIdempotencyError) as error:
                last_conflict = error
                prior = await self._repository.find_event_by_idempotency_key(
                    scope,
                    idempotency_key,
                )
                if prior is not None:
                    return await self._unchanged_result(scope, prior.operation_id)
                continue
            return OperationServiceResult(
                action=action,
                operation=updated,
                events=events,
            )
        raise ConcurrentOperationUpdateError(
            f"Could not {action} the operation after concurrent write conflicts"
        ) from last_conflict

    def _build_status_update(
        self,
        *,
        current: OrderOperation,
        target_status: OperationRecordStatus,
        idempotency_key: str,
        actor: str,
        provider_reference: str | None,
    ) -> tuple[OrderOperation, OrderOperationEvent]:
        """Build a versioned aggregate update and its immutable status event."""
        now = self._clock()
        updated = current.model_copy(
            update={
                "status": target_status,
                "provider_reference": provider_reference or current.provider_reference,
                "updated_at": now,
                "version": current.version + 1,
            }
        )
        event = OrderOperationEvent(
            event_id=self._id_factory(),
            idempotency_key=idempotency_key,
            operation_id=current.operation_id,
            event_type="status_changed",
            previous_status=current.status,
            current_status=target_status,
            provider_reference=updated.provider_reference,
            actor=actor,
            customer_id=current.customer_id,
            tenant_id=current.tenant_id,
            created_at=now,
        )
        return updated, event

    async def _unchanged_result(
        self,
        scope: AccessScope,
        operation_id: UUID,
    ) -> OperationServiceResult:
        """Return the aggregate for an already-recorded idempotent request."""
        return OperationServiceResult(
            action="status_unchanged",
            operation=await self.get_operation(scope, operation_id),
        )

    @staticmethod
    def _validate_request_metadata(request_id: str, actor: str) -> tuple[str, str]:
        """Reject blank idempotency and actor metadata before a write."""
        normalized_request_id = request_id.strip()
        normalized_actor = actor.strip()
        if not normalized_request_id:
            raise ValueError("request_id must not be empty")
        if not normalized_actor:
            raise ValueError("actor must not be empty")
        return normalized_request_id, normalized_actor

    @staticmethod
    def _validate_status_transition(
        current: OperationRecordStatus,
        target: OperationRecordStatus,
    ) -> None:
        """Reject status changes outside the lifecycle owned by this service."""
        if target not in _ALLOWED_STATUS_TRANSITIONS[current]:
            raise InvalidOperationStatusTransition(
                f"cannot transition an operation from {current!r} to {target!r}"
            )
