CREATE TABLE IF NOT EXISTS refund_requests (
    refund_id UUID PRIMARY KEY,
    order_id VARCHAR(50) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refund_requests_created_at
    ON refund_requests (created_at);
