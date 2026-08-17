ALTER TABLE case_management.support_cases
    DROP CONSTRAINT IF EXISTS support_cases_case_type_check;

ALTER TABLE case_management.support_cases
    ADD CONSTRAINT ck_support_cases_case_type CHECK (
        case_type IN (
            'safety_review',
            'business_escalation',
            'refund_review',
            'general_support',
            'staff_conduct_complaint',
            'other_complaint',
            'order_operation_review',
            'delivery_investigation'
        )
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
            'delivery_damage_claim', 'delivery_other_issue'
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
            'delivery_damage_claim', 'delivery_other_issue'
        ]::TEXT[]
    );

CREATE TABLE case_management.order_operations (
    operation_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (btrim(idempotency_key) <> ''),
    thread_id TEXT NOT NULL CHECK (btrim(thread_id) <> ''),
    source_message_id TEXT NOT NULL CHECK (btrim(source_message_id) <> ''),
    order_id VARCHAR(50) NOT NULL CHECK (btrim(order_id) <> ''),
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('cancellation', 'return', 'exchange')
    ),
    request_reason_code TEXT NOT NULL CHECK (
        request_reason_code IN (
            'ordered_by_mistake', 'no_longer_needed',
            'incorrect_item_or_quantity', 'delivery_too_slow', 'payment_issue',
            'changed_mind', 'wrong_item_received', 'damaged_item',
            'defective_item', 'missing_parts', 'not_as_described',
            'size_or_variant_issue', 'other'
        )
    ),
    policy_reason_codes TEXT[] NOT NULL CHECK (cardinality(policy_reason_codes) > 0),
    display_reason TEXT NOT NULL CHECK (btrim(display_reason) <> ''),
    replacement_variant_id TEXT,
    request_excerpt VARCHAR(500) NOT NULL CHECK (btrim(request_excerpt) <> ''),
    order_version INTEGER NOT NULL CHECK (order_version >= 1),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    currency CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    requires_manual_review BOOLEAN NOT NULL,
    review_case_type TEXT,
    review_priority TEXT,
    support_case_id UUID,
    provider_reference TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending_confirmation', 'submitted', 'processing',
            'manual_review', 'completed', 'rejected', 'cancelled_by_customer'
        )
    ),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT uq_order_operations_source_message UNIQUE (thread_id, source_message_id),
    CONSTRAINT fk_order_operations_support_case
        FOREIGN KEY (support_case_id)
        REFERENCES case_management.support_cases (case_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_order_operations_replacement CHECK (
        (operation_type = 'exchange' AND replacement_variant_id IS NOT NULL)
        OR (operation_type <> 'exchange' AND replacement_variant_id IS NULL)
    ),
    CONSTRAINT ck_order_operations_manual_review CHECK (
        (requires_manual_review = FALSE AND review_case_type IS NULL AND review_priority IS NULL)
        OR (
            requires_manual_review = TRUE
            AND review_case_type = 'order_operation_review'
            AND review_priority IN ('p1', 'p2')
        )
    ),
    CONSTRAINT ck_order_operations_support_case CHECK (
        support_case_id IS NULL OR requires_manual_review = TRUE
    ),
    CONSTRAINT ck_order_operations_timestamps CHECK (updated_at >= created_at)
);

CREATE TABLE case_management.order_operation_events (
    event_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (btrim(idempotency_key) <> ''),
    operation_id UUID NOT NULL
        REFERENCES case_management.order_operations (operation_id)
        ON DELETE RESTRICT,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'operation_created', 'confirmation_recorded', 'status_changed',
            'support_case_attached'
        )
    ),
    previous_status TEXT,
    current_status TEXT,
    provider_reference TEXT,
    support_case_id UUID
        REFERENCES case_management.support_cases (case_id)
        ON DELETE RESTRICT,
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_order_operation_events_status CHECK (
        (event_type = 'status_changed'
         AND previous_status IS NOT NULL
         AND current_status IS NOT NULL)
        OR (event_type <> 'status_changed'
            AND previous_status IS NULL
            AND current_status IS NULL)
    )
);

CREATE UNIQUE INDEX uq_order_operations_active_order
    ON case_management.order_operations (order_id)
    WHERE status IN ('pending_confirmation', 'submitted', 'processing', 'manual_review');

CREATE INDEX idx_order_operations_thread_created
    ON case_management.order_operations (thread_id, created_at, operation_id);

CREATE INDEX idx_order_operation_events_timeline
    ON case_management.order_operation_events (operation_id, created_at, event_id);
