-- v0.8 step 2: provider operations audit and redrive processing cycles.

-- Preserve the v0.7 free-form reason for legacy rows. New provider-operations
-- writes populate the fixed reason_code and never expose the legacy column.
ALTER TABLE integration.outbox_redrives
    ADD COLUMN reason_code TEXT CHECK (
        reason_code IS NULL OR reason_code IN (
            'dependency_or_configuration_restored',
            'transient_incident_resolved',
            'manual_retry_approved'
        )
    );

CREATE INDEX idx_outbox_redrives_tenant_command_history
    ON integration.outbox_redrives
        (tenant_id, command_id, created_at DESC, redrive_id DESC);

-- Existing Inbox messages and attempts are cycle 1. The lifetime attempt
-- counter remains unchanged; its value becomes cycle 1's attempt count.
ALTER TABLE integration.inbox_messages
    ADD COLUMN processing_cycle INTEGER NOT NULL DEFAULT 1 CHECK (
        processing_cycle >= 1
    ),
    ADD COLUMN attempts_in_cycle INTEGER NOT NULL DEFAULT 0;

UPDATE integration.inbox_messages
SET attempts_in_cycle = processing_attempts;

ALTER TABLE integration.inbox_messages
    ADD CONSTRAINT ck_inbox_messages_attempts_in_cycle CHECK (
        attempts_in_cycle BETWEEN 0 AND 5
    );

ALTER TABLE integration.inbox_processing_attempts
    ADD COLUMN processing_cycle INTEGER NOT NULL DEFAULT 1 CHECK (
        processing_cycle >= 1
    );

CREATE INDEX idx_inbox_processing_attempts_cycle_history
    ON integration.inbox_processing_attempts
        (inbox_id, processing_cycle DESC, attempt_number DESC, attempt_id DESC);

CREATE TABLE integration.inbox_redrives (
    redrive_id UUID PRIMARY KEY,
    inbox_id UUID NOT NULL
        REFERENCES integration.inbox_messages (inbox_id)
        ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL CHECK (btrim(tenant_id) <> ''),
    request_id TEXT NOT NULL CHECK (
        request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    ),
    requested_by TEXT NOT NULL CHECK (btrim(requested_by) <> ''),
    reason_code TEXT NOT NULL CHECK (
        reason_code IN (
            'dependency_or_configuration_restored',
            'transient_incident_resolved',
            'manual_retry_approved'
        )
    ),
    previous_cycle INTEGER NOT NULL CHECK (previous_cycle >= 1),
    new_cycle INTEGER NOT NULL CHECK (new_cycle >= 2),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_inbox_redrives_tenant_request UNIQUE (tenant_id, request_id),
    CONSTRAINT ck_inbox_redrives_cycle CHECK (new_cycle = previous_cycle + 1)
);

CREATE INDEX idx_inbox_redrives_tenant_inbox_history
    ON integration.inbox_redrives
        (tenant_id, inbox_id, created_at DESC, redrive_id DESC);

-- Provider-redrive case events contain only stable audit identifiers and the
-- fixed reason. Provider references and webhook/command payloads are excluded.
ALTER TABLE case_management.support_case_events
    ADD COLUMN provider_redrive_reason_code TEXT CHECK (
        provider_redrive_reason_code IS NULL OR provider_redrive_reason_code IN (
            'dependency_or_configuration_restored',
            'transient_incident_resolved',
            'manual_retry_approved'
        )
    );

ALTER TABLE case_management.support_case_events
    DROP CONSTRAINT IF EXISTS support_case_events_event_type_check;

ALTER TABLE case_management.support_case_events
    ADD CONSTRAINT support_case_events_event_type_check CHECK (
        event_type IN (
            'case_created', 'trigger_appended', 'status_changed',
            'assigned', 'provider_update', 'provider_redrive'
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
            AND provider_redrive_reason_code IS NULL
        )
        OR (
            event_type = 'status_changed'
            AND previous_status IS NOT NULL
            AND current_status IS NOT NULL
            AND actor IS NOT NULL
            AND btrim(actor) <> ''
            AND provider_redrive_reason_code IS NULL
        )
        OR (
            event_type = 'assigned'
            AND current_assigned_agent_id IS NOT NULL
            AND btrim(current_assigned_agent_id) <> ''
            AND actor IS NOT NULL
            AND btrim(actor) <> ''
            AND provider_redrive_reason_code IS NULL
        )
        OR (
            event_type = 'provider_update'
            AND provider_command_id IS NOT NULL
            AND provider_command_status IS NOT NULL
            AND actor IS NOT NULL
            AND btrim(actor) <> ''
            AND provider_redrive_reason_code IS NULL
        )
        OR (
            event_type = 'provider_redrive'
            AND provider_command_id IS NOT NULL
            AND provider_redrive_reason_code IS NOT NULL
            AND provider_command_status IS NULL
            AND provider_reference IS NULL
            AND previous_status IS NULL
            AND current_status IS NULL
            AND actor IS NOT NULL
            AND btrim(actor) <> ''
        )
    );

CREATE INDEX idx_support_case_events_provider_redrive_history
    ON case_management.support_case_events
        (tenant_id, case_id, created_at DESC, event_id DESC)
    WHERE event_type = 'provider_redrive';
