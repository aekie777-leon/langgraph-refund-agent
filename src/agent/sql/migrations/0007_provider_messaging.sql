-- v0.7 step 2: provider messaging persistence (outbox / inbox / attempts / leases)
-- and the domain adjustments that support queued order operations and
-- provider updates on support cases.

CREATE SCHEMA IF NOT EXISTS integration;

-- =====================================================================
-- integration.outbox_messages
-- =====================================================================
CREATE TABLE integration.outbox_messages (
    command_id UUID PRIMARY KEY,
    schema_version SMALLINT NOT NULL CHECK (schema_version = 1),
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    customer_id TEXT NOT NULL CHECK (btrim(customer_id) <> ''),
    source_message_id TEXT NOT NULL CHECK (btrim(source_message_id) <> ''),
    provider_connection_id TEXT NOT NULL CHECK (btrim(provider_connection_id) <> ''),
    provider_capability TEXT NOT NULL CHECK (
        provider_capability IN ('order_query', 'inventory_query', 'order_operation')
    ),
    command_type TEXT NOT NULL CHECK (
        command_type IN (
            'cancel_order', 'return_order', 'exchange_order', 'delivery_investigation'
        )
    ),
    aggregate_type TEXT NOT NULL CHECK (
        aggregate_type IN ('order_operation', 'support_case')
    ),
    aggregate_id UUID NOT NULL,
    expected_order_version INTEGER CHECK (
        expected_order_version IS NULL OR expected_order_version >= 1
    ),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'processing', 'retry_scheduled', 'published', 'dead')
    ),
    delivery_cycle INTEGER NOT NULL DEFAULT 1 CHECK (delivery_cycle >= 1),
    attempts_in_cycle INTEGER NOT NULL DEFAULT 0 CHECK (
        attempts_in_cycle BETWEEN 0 AND 8
    ),
    available_at TIMESTAMPTZ NOT NULL,
    lease_id UUID,
    lease_owner TEXT CHECK (lease_owner IS NULL OR btrim(lease_owner) <> ''),
    lease_expires_at TIMESTAMPTZ,
    last_failure_kind TEXT CHECK (
        last_failure_kind IS NULL OR last_failure_kind IN (
            'network_error', 'timeout', 'http_retryable', 'http_client_error',
            'provider_rejection', 'validation_error'
        )
    ),
    last_error_code VARCHAR(500) CHECK (
        last_error_code IS NULL OR btrim(last_error_code) <> ''
    ),
    last_error_message VARCHAR(500) CHECK (
        last_error_message IS NULL OR btrim(last_error_message) <> ''
    ),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    dead_at TIMESTAMPTZ,
    CONSTRAINT ck_outbox_messages_timestamps CHECK (updated_at >= created_at),
    CONSTRAINT ck_outbox_messages_status_lease CHECK (
        (status = 'processing'
         AND lease_id IS NOT NULL
         AND lease_owner IS NOT NULL
         AND lease_expires_at IS NOT NULL)
        OR (status <> 'processing'
            AND lease_id IS NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_outbox_messages_published CHECK (
        (status = 'published' AND published_at IS NOT NULL)
        OR (status <> 'published' AND published_at IS NULL)
    ),
    CONSTRAINT ck_outbox_messages_dead CHECK (
        (status = 'dead' AND dead_at IS NOT NULL)
        OR (status <> 'dead' AND dead_at IS NULL)
    ),
    CONSTRAINT ck_outbox_messages_aggregate_version CHECK (
        (aggregate_type = 'order_operation' AND expected_order_version IS NOT NULL)
        OR (aggregate_type = 'support_case' AND expected_order_version IS NULL)
    ),
    CONSTRAINT ck_outbox_messages_command_aggregate CHECK (
        (command_type IN ('cancel_order', 'return_order', 'exchange_order')
         AND aggregate_type = 'order_operation')
        OR (command_type = 'delivery_investigation' AND aggregate_type = 'support_case')
    ),
    CONSTRAINT uq_outbox_messages_tenant_idempotency UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX idx_outbox_messages_due
    ON integration.outbox_messages (available_at, created_at, command_id)
    WHERE status IN ('pending', 'retry_scheduled');

CREATE INDEX idx_outbox_messages_expired_leases
    ON integration.outbox_messages (lease_expires_at)
    WHERE status = 'processing';

-- =====================================================================
-- integration.outbox_delivery_attempts
-- =====================================================================
CREATE TABLE integration.outbox_delivery_attempts (
    attempt_id UUID PRIMARY KEY,
    command_id UUID NOT NULL
        REFERENCES integration.outbox_messages (command_id)
        ON DELETE RESTRICT,
    delivery_cycle INTEGER NOT NULL CHECK (delivery_cycle >= 1),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    lease_id UUID NOT NULL,
    worker_id TEXT NOT NULL CHECK (btrim(worker_id) <> ''),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    outcome TEXT CHECK (
        outcome IS NULL OR outcome IN (
            'accepted', 'provider_rejected', 'retry_scheduled',
            'terminal_failure', 'lease_expired'
        )
    ),
    failure_kind TEXT CHECK (
        failure_kind IS NULL OR failure_kind IN (
            'network_error', 'timeout', 'http_retryable', 'http_client_error',
            'provider_rejection', 'validation_error'
        )
    ),
    http_status INTEGER CHECK (http_status IS NULL OR http_status >= 100),
    provider_operation_id TEXT CHECK (
        provider_operation_id IS NULL OR btrim(provider_operation_id) <> ''
    ),
    provider_reference TEXT CHECK (
        provider_reference IS NULL OR btrim(provider_reference) <> ''
    ),
    safe_error_code VARCHAR(500) CHECK (
        safe_error_code IS NULL OR btrim(safe_error_code) <> ''
    ),
    safe_error_message VARCHAR(500) CHECK (
        safe_error_message IS NULL OR btrim(safe_error_message) <> ''
    ),
    retry_after_seconds DOUBLE PRECISION CHECK (
        retry_after_seconds IS NULL
        OR (retry_after_seconds >= 0 AND retry_after_seconds < 'infinity')
    ),
    next_available_at TIMESTAMPTZ,
    CONSTRAINT uq_outbox_delivery_attempts_cycle_number
        UNIQUE (command_id, delivery_cycle, attempt_number),
    CONSTRAINT ck_outbox_delivery_attempts_finished CHECK (
        (outcome IS NULL AND finished_at IS NULL)
        OR (outcome IS NOT NULL AND finished_at IS NOT NULL)
    )
);

CREATE INDEX idx_outbox_delivery_attempts_command
    ON integration.outbox_delivery_attempts (command_id, started_at);

-- =====================================================================
-- integration.outbox_redrives
-- =====================================================================
CREATE TABLE integration.outbox_redrives (
    redrive_id UUID PRIMARY KEY,
    command_id UUID NOT NULL
        REFERENCES integration.outbox_messages (command_id)
        ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    request_id TEXT NOT NULL CHECK (btrim(request_id) <> ''),
    requested_by TEXT NOT NULL CHECK (btrim(requested_by) <> ''),
    reason VARCHAR(500) NOT NULL CHECK (btrim(reason) <> ''),
    previous_cycle INTEGER NOT NULL CHECK (previous_cycle >= 1),
    new_cycle INTEGER NOT NULL CHECK (new_cycle >= 2),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_outbox_redrives_tenant_request UNIQUE (tenant_id, request_id),
    CONSTRAINT ck_outbox_redrives_cycle CHECK (new_cycle = previous_cycle + 1)
);

-- =====================================================================
-- integration.inbox_messages
-- =====================================================================
CREATE TABLE integration.inbox_messages (
    inbox_id UUID PRIMARY KEY,
    provider_connection_id TEXT NOT NULL CHECK (btrim(provider_connection_id) <> ''),
    event_id TEXT NOT NULL CHECK (btrim(event_id) <> ''),
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    schema_version SMALLINT NOT NULL CHECK (schema_version = 1),
    event_type TEXT NOT NULL CHECK (event_type = 'provider_command_status_changed'),
    command_id UUID NOT NULL,
    aggregate_type TEXT NOT NULL CHECK (
        aggregate_type IN ('order_operation', 'support_case')
    ),
    aggregate_id UUID NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    raw_body_sha256 CHAR(64) NOT NULL CHECK (raw_body_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (
        status IN ('received', 'processing', 'processed', 'failed')
    ),
    processing_attempts INTEGER NOT NULL DEFAULT 0 CHECK (processing_attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL,
    lease_id UUID,
    lease_owner TEXT CHECK (lease_owner IS NULL OR btrim(lease_owner) <> ''),
    lease_expires_at TIMESTAMPTZ,
    last_error_code VARCHAR(500) CHECK (
        last_error_code IS NULL OR btrim(last_error_code) <> ''
    ),
    last_error_message VARCHAR(500) CHECK (
        last_error_message IS NULL OR btrim(last_error_message) <> ''
    ),
    received_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    CONSTRAINT uq_inbox_messages_connection_event
        UNIQUE (provider_connection_id, event_id),
    CONSTRAINT ck_inbox_messages_timestamps CHECK (updated_at >= received_at),
    CONSTRAINT ck_inbox_messages_status_lease CHECK (
        (status = 'processing'
         AND lease_id IS NOT NULL
         AND lease_owner IS NOT NULL
         AND lease_expires_at IS NOT NULL)
        OR (status <> 'processing'
            AND lease_id IS NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_inbox_messages_processed CHECK (
        (status = 'processed' AND processed_at IS NOT NULL)
        OR (status <> 'processed' AND processed_at IS NULL)
    ),
    CONSTRAINT ck_inbox_messages_failed CHECK (
        (status = 'failed' AND failed_at IS NOT NULL)
        OR (status <> 'failed' AND failed_at IS NULL)
    )
);

CREATE INDEX idx_inbox_messages_due
    ON integration.inbox_messages (available_at, received_at, inbox_id)
    WHERE status = 'received';

CREATE INDEX idx_inbox_messages_expired_leases
    ON integration.inbox_messages (lease_expires_at)
    WHERE status = 'processing';

-- =====================================================================
-- integration.inbox_processing_attempts
-- =====================================================================
CREATE TABLE integration.inbox_processing_attempts (
    attempt_id UUID PRIMARY KEY,
    inbox_id UUID NOT NULL
        REFERENCES integration.inbox_messages (inbox_id)
        ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    lease_id UUID NOT NULL,
    worker_id TEXT NOT NULL CHECK (btrim(worker_id) <> ''),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    outcome TEXT CHECK (
        outcome IS NULL OR outcome IN (
            'processed', 'retry_scheduled', 'terminal_failure', 'lease_expired'
        )
    ),
    safe_error_code VARCHAR(500) CHECK (
        safe_error_code IS NULL OR btrim(safe_error_code) <> ''
    ),
    safe_error_message VARCHAR(500) CHECK (
        safe_error_message IS NULL OR btrim(safe_error_message) <> ''
    ),
    CONSTRAINT uq_inbox_processing_attempts_number UNIQUE (inbox_id, attempt_number),
    CONSTRAINT ck_inbox_processing_attempts_finished CHECK (
        (outcome IS NULL AND finished_at IS NULL)
        OR (outcome IS NOT NULL AND finished_at IS NOT NULL)
    )
);

CREATE INDEX idx_inbox_processing_attempts_inbox
    ON integration.inbox_processing_attempts (inbox_id, started_at);

-- =====================================================================
-- Domain adjustments: queued order operations
-- =====================================================================
ALTER TABLE case_management.order_operations
    DROP CONSTRAINT IF EXISTS order_operations_status_check;

ALTER TABLE case_management.order_operations
    ADD CONSTRAINT order_operations_status_check CHECK (
        status IN (
            'pending_confirmation', 'queued', 'submitted', 'processing',
            'manual_review', 'completed', 'rejected', 'cancelled_by_customer'
        )
    );

-- The active-order index was created by 0004 inside the case_management
-- schema, which is not guaranteed to be in search_path: the DROP must be
-- schema-qualified or it silently misses the old index and the CREATE below
-- collides with it.
DROP INDEX IF EXISTS case_management.uq_order_operations_active_order;

CREATE UNIQUE INDEX uq_order_operations_active_order
    ON case_management.order_operations (tenant_id, order_id)
    WHERE status IN ('pending_confirmation', 'queued', 'submitted', 'processing', 'manual_review');

-- =====================================================================
-- Domain adjustments: provider_update case events and reason codes
-- =====================================================================
ALTER TABLE case_management.support_case_events
    ADD COLUMN provider_command_id UUID,
    ADD COLUMN provider_command_status TEXT CHECK (
        provider_command_status IS NULL
        OR provider_command_status IN ('accepted', 'processing', 'completed', 'rejected')
    ),
    ADD COLUMN provider_reference TEXT CHECK (
        provider_reference IS NULL OR btrim(provider_reference) <> ''
    );

ALTER TABLE case_management.support_cases
    DROP CONSTRAINT IF EXISTS ck_support_cases_reason_codes;

ALTER TABLE case_management.support_cases
    ADD CONSTRAINT ck_support_cases_reason_codes CHECK (
        cardinality(reason_codes) > 0
        AND reason_codes <@ ARRAY[
            'hard_critical_self_harm', 'hard_critical_violence',
            'hard_critical_legal', 'hard_critical_regulatory',
            'hard_critical_reputation', 'hard_critical_other',
            'semantic_critical_self_harm', 'semantic_critical_violence',
            'semantic_critical_legal', 'semantic_critical_regulatory',
            'semantic_critical_reputation', 'semantic_critical_other',
            'semantic_high_self_harm', 'semantic_high_violence',
            'semantic_high_legal', 'semantic_high_regulatory',
            'semantic_high_reputation', 'semantic_high_other',
            'semantic_medium_self_harm', 'semantic_medium_violence',
            'semantic_medium_legal', 'semantic_medium_regulatory',
            'semantic_medium_reputation', 'semantic_medium_other',
            'refund_manual_review', 'confirmed_human_request',
            'staff_conduct_critical', 'staff_conduct_high',
            'staff_conduct_medium', 'staff_conduct_low',
            'explicit_other_complaint',
            'order_state_invalid', 'cancellation_fulfillment_processing',
            'cancellation_state_invalid', 'operation_delivery_date_invalid',
            'return_eligibility_unknown', 'exchange_eligibility_unknown',
            'currency_threshold_unconfigured', 'return_manual_amount_review',
            'exchange_inventory_unknown', 'delivery_data_invalid',
            'delivery_tracking_stalled', 'delivery_overdue_investigation',
            'delivery_failed', 'delivery_marked_received_dispute',
            'delivery_damage_claim', 'delivery_other_issue',
            'provider_delivery_failed'
        ]::TEXT[]
    );

ALTER TABLE case_management.support_case_events
    DROP CONSTRAINT IF EXISTS ck_support_case_events_reason_codes;

ALTER TABLE case_management.support_case_events
    ADD CONSTRAINT ck_support_case_events_reason_codes CHECK (
        reason_codes <@ ARRAY[
            'hard_critical_self_harm', 'hard_critical_violence',
            'hard_critical_legal', 'hard_critical_regulatory',
            'hard_critical_reputation', 'hard_critical_other',
            'semantic_critical_self_harm', 'semantic_critical_violence',
            'semantic_critical_legal', 'semantic_critical_regulatory',
            'semantic_critical_reputation', 'semantic_critical_other',
            'semantic_high_self_harm', 'semantic_high_violence',
            'semantic_high_legal', 'semantic_high_regulatory',
            'semantic_high_reputation', 'semantic_high_other',
            'semantic_medium_self_harm', 'semantic_medium_violence',
            'semantic_medium_legal', 'semantic_medium_regulatory',
            'semantic_medium_reputation', 'semantic_medium_other',
            'refund_manual_review', 'confirmed_human_request',
            'staff_conduct_critical', 'staff_conduct_high',
            'staff_conduct_medium', 'staff_conduct_low',
            'explicit_other_complaint',
            'order_state_invalid', 'cancellation_fulfillment_processing',
            'cancellation_state_invalid', 'operation_delivery_date_invalid',
            'return_eligibility_unknown', 'exchange_eligibility_unknown',
            'currency_threshold_unconfigured', 'return_manual_amount_review',
            'exchange_inventory_unknown', 'delivery_data_invalid',
            'delivery_tracking_stalled', 'delivery_overdue_investigation',
            'delivery_failed', 'delivery_marked_received_dispute',
            'delivery_damage_claim', 'delivery_other_issue',
            'provider_delivery_failed'
        ]::TEXT[]
    );

ALTER TABLE case_management.support_case_events
    DROP CONSTRAINT IF EXISTS support_case_events_event_type_check;

ALTER TABLE case_management.support_case_events
    ADD CONSTRAINT support_case_events_event_type_check CHECK (
        event_type IN (
            'case_created', 'trigger_appended', 'status_changed',
            'assigned', 'provider_update'
        )
    );

ALTER TABLE case_management.support_case_events
    DROP CONSTRAINT IF EXISTS ck_support_case_events_by_type;

ALTER TABLE case_management.support_case_events
    ADD CONSTRAINT ck_support_case_events_by_type CHECK (
        (
            event_type IN ('case_created', 'trigger_appended')
            AND source_message_id IS NOT NULL
            AND btrim(source_message_id) <> ''
            AND cardinality(reason_codes) > 0
            AND btrim(triggering_message_excerpt) <> ''
            AND current_priority IS NOT NULL
            AND current_status IS NOT NULL
        )
        OR (
            event_type = 'status_changed'
            AND previous_status IS NOT NULL
            AND current_status IS NOT NULL
            AND actor IS NOT NULL
            AND btrim(actor) <> ''
        )
        OR (
            event_type = 'assigned'
            AND current_assigned_agent_id IS NOT NULL
            AND btrim(current_assigned_agent_id) <> ''
            AND actor IS NOT NULL
            AND btrim(actor) <> ''
        )
        OR (
            event_type = 'provider_update'
            AND provider_command_id IS NOT NULL
            AND provider_command_status IS NOT NULL
            AND actor IS NOT NULL
            AND btrim(actor) <> ''
        )
    );

-- =====================================================================
-- Delivery investigation uniqueness: replace the single active index
-- =====================================================================
-- Same schema-qualification requirement as the active-order index above:
-- 0002 created this index inside case_management.
DROP INDEX IF EXISTS case_management.uq_support_cases_active_thread_type;

CREATE UNIQUE INDEX uq_support_cases_active_thread_type
    ON case_management.support_cases (tenant_id, thread_id, case_type)
    WHERE status IN ('open', 'in_progress', 'on_hold')
      AND case_type <> 'delivery_investigation';

CREATE UNIQUE INDEX uq_support_cases_active_delivery_order
    ON case_management.support_cases (tenant_id, thread_id, case_type, order_id)
    WHERE status IN ('open', 'in_progress', 'on_hold')
      AND case_type = 'delivery_investigation'
      AND order_id IS NOT NULL;

-- delivery_investigation cases must always carry an order_id.
ALTER TABLE case_management.support_cases
    ADD CONSTRAINT ck_support_cases_delivery_order CHECK (
        case_type <> 'delivery_investigation'
        OR (order_id IS NOT NULL AND btrim(order_id) <> '')
    );
