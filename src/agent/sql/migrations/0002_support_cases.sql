CREATE SCHEMA IF NOT EXISTS case_management;

CREATE TABLE IF NOT EXISTS case_management.support_cases (
    case_id UUID PRIMARY KEY,
    thread_id TEXT NOT NULL CHECK (btrim(thread_id) <> ''),
    source_message_id TEXT NOT NULL CHECK (btrim(source_message_id) <> ''),
    order_id VARCHAR(50),
    case_type TEXT NOT NULL CHECK (
        case_type IN (
            'safety_review',
            'business_escalation',
            'refund_review',
            'general_support',
            'staff_conduct_complaint',
            'other_complaint'
        )
    ),
    priority TEXT NOT NULL CHECK (priority IN ('p0', 'p1', 'p2', 'p3')),
    status TEXT NOT NULL CHECK (
        status IN ('open', 'in_progress', 'on_hold', 'resolved')
    ),
    risk_level TEXT CHECK (
        risk_level IS NULL
        OR risk_level IN ('none', 'low', 'medium', 'high', 'critical')
    ),
    risk_categories TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    reason_codes TEXT[] NOT NULL,
    display_reason TEXT NOT NULL CHECK (btrim(display_reason) <> ''),
    triggering_message_excerpt VARCHAR(500) NOT NULL
        CHECK (btrim(triggering_message_excerpt) <> ''),
    on_hold_reason TEXT CHECK (
        on_hold_reason IS NULL
        OR on_hold_reason IN (
            'waiting_customer',
            'waiting_external_system',
            'waiting_internal_team',
            'system_unavailable',
            'force_majeure',
            'other'
        )
    ),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT ck_support_cases_risk_categories CHECK (
        risk_categories <@ ARRAY[
            'self_harm',
            'violence',
            'legal',
            'regulatory',
            'reputation',
            'other'
        ]::TEXT[]
    ),
    CONSTRAINT ck_support_cases_reason_codes CHECK (
        cardinality(reason_codes) > 0
        AND reason_codes <@ ARRAY[
            'hard_critical_self_harm',
            'hard_critical_violence',
            'hard_critical_legal',
            'hard_critical_regulatory',
            'hard_critical_reputation',
            'hard_critical_other',
            'semantic_critical_self_harm',
            'semantic_critical_violence',
            'semantic_critical_legal',
            'semantic_critical_regulatory',
            'semantic_critical_reputation',
            'semantic_critical_other',
            'semantic_high_self_harm',
            'semantic_high_violence',
            'semantic_high_legal',
            'semantic_high_regulatory',
            'semantic_high_reputation',
            'semantic_high_other',
            'semantic_medium_self_harm',
            'semantic_medium_violence',
            'semantic_medium_legal',
            'semantic_medium_regulatory',
            'semantic_medium_reputation',
            'semantic_medium_other',
            'refund_manual_review',
            'confirmed_human_request',
            'staff_conduct_critical',
            'staff_conduct_high',
            'staff_conduct_medium',
            'staff_conduct_low',
            'explicit_other_complaint'
        ]::TEXT[]
    ),
    CONSTRAINT ck_support_cases_on_hold_reason CHECK (
        (status = 'on_hold' AND on_hold_reason IS NOT NULL)
        OR (status <> 'on_hold' AND on_hold_reason IS NULL)
    ),
    CONSTRAINT ck_support_cases_timestamps CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS case_management.support_case_events (
    event_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    case_id UUID NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('case_created', 'trigger_appended', 'status_changed')
    ),
    source_message_id TEXT,
    order_id VARCHAR(50),
    risk_level TEXT CHECK (
        risk_level IS NULL
        OR risk_level IN ('none', 'low', 'medium', 'high', 'critical')
    ),
    risk_categories TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    reason_codes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    triggering_message_excerpt VARCHAR(500) NOT NULL DEFAULT '',
    previous_priority TEXT CHECK (
        previous_priority IS NULL
        OR previous_priority IN ('p0', 'p1', 'p2', 'p3')
    ),
    current_priority TEXT CHECK (
        current_priority IS NULL
        OR current_priority IN ('p0', 'p1', 'p2', 'p3')
    ),
    previous_status TEXT CHECK (
        previous_status IS NULL
        OR previous_status IN ('open', 'in_progress', 'on_hold', 'resolved')
    ),
    current_status TEXT CHECK (
        current_status IS NULL
        OR current_status IN ('open', 'in_progress', 'on_hold', 'resolved')
    ),
    on_hold_reason TEXT CHECK (
        on_hold_reason IS NULL
        OR on_hold_reason IN (
            'waiting_customer',
            'waiting_external_system',
            'waiting_internal_team',
            'system_unavailable',
            'force_majeure',
            'other'
        )
    ),
    actor TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_support_case_events_case
        FOREIGN KEY (case_id)
        REFERENCES case_management.support_cases (case_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_support_case_events_idempotency UNIQUE (idempotency_key),
    CONSTRAINT ck_support_case_events_risk_categories CHECK (
        risk_categories <@ ARRAY[
            'self_harm',
            'violence',
            'legal',
            'regulatory',
            'reputation',
            'other'
        ]::TEXT[]
    ),
    CONSTRAINT ck_support_case_events_reason_codes CHECK (
        reason_codes <@ ARRAY[
            'hard_critical_self_harm',
            'hard_critical_violence',
            'hard_critical_legal',
            'hard_critical_regulatory',
            'hard_critical_reputation',
            'hard_critical_other',
            'semantic_critical_self_harm',
            'semantic_critical_violence',
            'semantic_critical_legal',
            'semantic_critical_regulatory',
            'semantic_critical_reputation',
            'semantic_critical_other',
            'semantic_high_self_harm',
            'semantic_high_violence',
            'semantic_high_legal',
            'semantic_high_regulatory',
            'semantic_high_reputation',
            'semantic_high_other',
            'semantic_medium_self_harm',
            'semantic_medium_violence',
            'semantic_medium_legal',
            'semantic_medium_regulatory',
            'semantic_medium_reputation',
            'semantic_medium_other',
            'refund_manual_review',
            'confirmed_human_request',
            'staff_conduct_critical',
            'staff_conduct_high',
            'staff_conduct_medium',
            'staff_conduct_low',
            'explicit_other_complaint'
        ]::TEXT[]
    ),
    CONSTRAINT ck_support_case_events_by_type CHECK (
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
    ),
    CONSTRAINT ck_support_case_events_on_hold_reason CHECK (
        event_type <> 'status_changed'
        OR (
            (current_status = 'on_hold' AND on_hold_reason IS NOT NULL)
            OR (current_status <> 'on_hold' AND on_hold_reason IS NULL)
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_support_cases_active_thread_type
    ON case_management.support_cases (thread_id, case_type)
    WHERE status IN ('open', 'in_progress', 'on_hold');

CREATE INDEX IF NOT EXISTS idx_support_cases_thread_status
    ON case_management.support_cases (thread_id, status);

CREATE INDEX IF NOT EXISTS idx_support_cases_queue
    ON case_management.support_cases (
        case_type,
        priority,
        status,
        created_at
    );

CREATE INDEX IF NOT EXISTS idx_support_case_events_case_timeline
    ON case_management.support_case_events (case_id, created_at, event_id);

CREATE INDEX IF NOT EXISTS idx_support_case_events_source_message
    ON case_management.support_case_events (source_message_id, case_id)
    WHERE source_message_id IS NOT NULL;
