-- TM Sniper Dashboard: independent extended Paper trade ledger.
-- Non-destructive. These records are intentionally separate from V17/V18 research events.

CREATE TABLE IF NOT EXISTS extended_paper_trades (
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    trade_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'open', 'closed', 'entry_failed', 'exit_failed')),
    direction TEXT NOT NULL CHECK (direction IN ('CALL', 'PUT')),
    decision_version TEXT NOT NULL,
    execution_environment TEXT NOT NULL CHECK (execution_environment = 'alpaca_paper'),
    decision_at TIMESTAMPTZ NOT NULL,
    entered_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    option_symbol TEXT,
    option_expiry DATE,
    option_strike NUMERIC(12, 4),
    option_entry_mid NUMERIC(14, 4),
    option_exit_mid NUMERIC(14, 4),
    option_quantity INTEGER NOT NULL DEFAULT 1 CHECK (option_quantity = 1),
    alpaca_entry_order_id TEXT,
    alpaca_exit_order_id TEXT,
    tsla_decision_price NUMERIC(14, 4) NOT NULL,
    tsla_entry_price NUMERIC(14, 4),
    tsla_exit_price NUMERIC(14, 4),
    vwap_at_decision NUMERIC(14, 4) NOT NULL,
    invalidation_price NUMERIC(14, 4) NOT NULL,
    target_030_price NUMERIC(14, 4) NOT NULL,
    target_060_price NUMERIC(14, 4) NOT NULL,
    target_120_price NUMERIC(14, 4) NOT NULL,
    max_favorable_extension NUMERIC(14, 4) NOT NULL DEFAULT 0,
    max_adverse_extension NUMERIC(14, 4) NOT NULL DEFAULT 0,
    hit_030_at TIMESTAMPTZ,
    hit_060_at TIMESTAMPTZ,
    hit_120_at TIMESTAMPTZ,
    exit_reason TEXT,
    option_pnl_dollars NUMERIC(14, 4),
    decision_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (trade_date)
);

CREATE INDEX IF NOT EXISTS extended_paper_trades_status_idx
    ON extended_paper_trades (status, trade_date DESC);

CREATE TABLE IF NOT EXISTS extended_paper_trade_events (
    id BIGSERIAL PRIMARY KEY,
    trade_id BIGINT NOT NULL REFERENCES extended_paper_trades(id) ON DELETE RESTRICT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'decision_reserved', 'entry_opened', 'entry_failed',
        'target_030_hit', 'target_060_hit', 'target_120_hit',
        'exit_submitted', 'exit_closed', 'exit_failed', 'recovered_open_position'
    )),
    observed_at TIMESTAMPTZ NOT NULL,
    tsla_price NUMERIC(14, 4),
    option_mid NUMERIC(14, 4),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS extended_paper_trade_events_trade_idx
    ON extended_paper_trade_events (trade_id, observed_at);
