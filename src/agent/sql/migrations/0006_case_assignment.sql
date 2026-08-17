-- Add support-case assignment metadata and the assigned event type.

ALTER TABLE case_management.support_case_events
    ADD COLUMN previous_assigned_agent_id TEXT,
    ADD COLUMN current_assigned_agent_id TEXT;

ALTER TABLE case_management.support_case_events
    DROP CONSTRAINT IF EXISTS support_case_events_event_type_check;

ALTER TABLE case_management.support_case_events
    ADD CONSTRAINT support_case_events_event_type_check CHECK (
        event_type IN (
            'case_created',
            'trigger_appended',
            'status_changed',
            'assigned'
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
    );
