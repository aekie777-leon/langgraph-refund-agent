CREATE INDEX IF NOT EXISTS idx_support_cases_order_id
    ON case_management.support_cases (order_id)
    WHERE order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_support_cases_created_at
    ON case_management.support_cases (created_at, case_id);
