CREATE TABLE IF NOT EXISTS helper_workers (
    id UUID PRIMARY KEY,
    label TEXT NOT NULL,
    token_hash BYTEA NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    seen_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS helper_orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    server_id INTEGER NOT NULL CHECK (server_id > 0),
    server_name TEXT NOT NULL,
    nickname TEXT NOT NULL,
    password_cipher BYTEA,
    keyword TEXT NOT NULL CHECK (char_length(keyword) BETWEEN 1 AND 50),
    min_price BIGINT NOT NULL CHECK (min_price BETWEEN 0 AND 100000000),
    max_price BIGINT NOT NULL CHECK (max_price BETWEEN min_price AND 100000000),
    mode TEXT NOT NULL CHECK (mode IN ('notify','buy')),
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds BETWEEN 5 AND 60),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','stopped','error')),
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
    partial_results BOOLEAN NOT NULL DEFAULT FALSE,
    worker_id UUID REFERENCES helper_workers(id),
    lease_until TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS helper_active_account
    ON helper_orders (server_id, lower(nickname)) WHERE status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS helper_worker_active
    ON helper_orders (worker_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS helper_orders_owner ON helper_orders (user_id, id DESC);

CREATE TABLE IF NOT EXISTS helper_lots (
    order_id BIGINT REFERENCES helper_orders(id) ON DELETE CASCADE,
    lot_id BIGINT,
    name TEXT NOT NULL,
    price BIGINT NOT NULL CHECK (price BETWEEN 0 AND 100000000),
    purchase TEXT CHECK (purchase IN ('pending','bought','declined','uncertain')),
    PRIMARY KEY (order_id, lot_id)
);

CREATE TABLE IF NOT EXISTS helper_alerts (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES helper_orders(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS helper_pending_alerts ON helper_alerts (id) WHERE delivered_at IS NULL;
