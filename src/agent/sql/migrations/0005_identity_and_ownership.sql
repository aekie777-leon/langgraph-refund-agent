-- Add ownership columns to persisted domain tables and backfill legacy rows.

-- ===== case_management.support_cases =====
ALTER TABLE case_management.support_cases
    ADD COLUMN customer_id TEXT,
    ADD COLUMN tenant_id TEXT,
    ADD COLUMN created_by TEXT,
    ADD COLUMN assigned_agent_id TEXT;

UPDATE case_management.support_cases
   SET customer_id = 'legacy',
       tenant_id = 'legacy',
       created_by = 'system'
 WHERE customer_id IS NULL;

ALTER TABLE case_management.support_cases
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ADD CONSTRAINT ck_support_cases_ownership CHECK (
        btrim(customer_id) <> ''
        AND btrim(tenant_id) <> ''
        AND btrim(created_by) <> ''
    );

CREATE INDEX idx_support_cases_tenant_customer
    ON case_management.support_cases (tenant_id, customer_id, created_at);
CREATE INDEX idx_support_cases_tenant_assigned
    ON case_management.support_cases (tenant_id, assigned_agent_id, status)
    WHERE assigned_agent_id IS NOT NULL;

-- ===== case_management.support_case_events =====
ALTER TABLE case_management.support_case_events
    ADD COLUMN customer_id TEXT,
    ADD COLUMN tenant_id TEXT;

UPDATE case_management.support_case_events
   SET customer_id = 'legacy',
       tenant_id = 'legacy'
 WHERE customer_id IS NULL;

ALTER TABLE case_management.support_case_events
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ADD CONSTRAINT ck_support_case_events_ownership CHECK (
        btrim(customer_id) <> ''
        AND btrim(tenant_id) <> ''
    );

CREATE INDEX idx_support_case_events_tenant_customer
    ON case_management.support_case_events (tenant_id, customer_id, case_id);

-- ===== case_management.order_operations =====
ALTER TABLE case_management.order_operations
    ADD COLUMN customer_id TEXT,
    ADD COLUMN tenant_id TEXT,
    ADD COLUMN created_by TEXT;

UPDATE case_management.order_operations
   SET customer_id = 'legacy',
       tenant_id = 'legacy',
       created_by = 'system'
 WHERE customer_id IS NULL;

ALTER TABLE case_management.order_operations
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ADD CONSTRAINT ck_order_operations_ownership CHECK (
        btrim(customer_id) <> ''
        AND btrim(tenant_id) <> ''
        AND btrim(created_by) <> ''
    );

CREATE INDEX idx_order_operations_tenant_customer
    ON case_management.order_operations (tenant_id, customer_id, created_at);

-- ===== case_management.order_operation_events =====
ALTER TABLE case_management.order_operation_events
    ADD COLUMN customer_id TEXT,
    ADD COLUMN tenant_id TEXT;

UPDATE case_management.order_operation_events
   SET customer_id = 'legacy',
       tenant_id = 'legacy'
 WHERE customer_id IS NULL;

ALTER TABLE case_management.order_operation_events
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ADD CONSTRAINT ck_order_operation_events_ownership CHECK (
        btrim(customer_id) <> ''
        AND btrim(tenant_id) <> ''
    );

CREATE INDEX idx_order_operation_events_tenant_customer
    ON case_management.order_operation_events (tenant_id, customer_id, operation_id);

-- ===== public.refund_requests =====
ALTER TABLE refund_requests
    ADD COLUMN customer_id TEXT,
    ADD COLUMN tenant_id TEXT,
    ADD COLUMN created_by TEXT;

UPDATE refund_requests
   SET customer_id = 'legacy',
       tenant_id = 'legacy',
       created_by = 'system'
 WHERE customer_id IS NULL;

ALTER TABLE refund_requests
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ADD CONSTRAINT ck_refund_requests_ownership CHECK (
        btrim(customer_id) <> ''
        AND btrim(tenant_id) <> ''
        AND btrim(created_by) <> ''
    );

CREATE INDEX idx_refund_requests_tenant_customer
    ON refund_requests (tenant_id, customer_id, created_at);
