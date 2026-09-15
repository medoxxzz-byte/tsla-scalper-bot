-- TM Sniper research-event storage: version 1
-- Raw payload is retained so later research can add fields without losing history.

CREATE TABLE IF NOT EXISTS tm_research_events (
    id BIGSERIAL PRIMARY KEY,
    event_key CHAR(64) NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK (source IN ('v17', 'v18')),
    action TEXT NOT NULL,
    direction TEXT NULL CHECK (direction IN ('CALL', 'PUT')),
    event_date_et DATE NOT NULL,
    source_event_time TEXT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),

    price NUMERIC(12, 4) NULL,
    vwap NUMERIC(12, 4) NULL,
    support NUMERIC(12, 4) NULL,
    resistance NUMERIC(12, 4) NULL,
    range_low NUMERIC(12, 4) NULL,
    range_high NUMERIC(12, 4) NULL,
    cmf NUMERIC(12, 6) NULL,
    macd NUMERIC(12, 6) NULL,
    macd_signal NUMERIC(12, 6) NULL,
    macd_histogram NUMERIC(12, 6) NULL,
    rsi NUMERIC(12, 4) NULL,
    obv NUMERIC(20, 4) NULL,
    adx NUMERIC(12, 4) NULL,
    mom NUMERIC(12, 6) NULL,
    relative_volume NUMERIC(12, 4) NULL,
    atr NUMERIC(12, 6) NULL,
    volume NUMERIC(20, 4) NULL,
    five_minute_bias TEXT NULL,
    payload JSONB NOT NULL,

    outcome_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome_status IN ('pending', 'tracking', 'initial_extension',
                                  'continuation', 'late_failure', 'neutral', 'invalidated')),
    outcome JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tm_research_events_date_source
    ON tm_research_events (event_date_et DESC, source, action);

CREATE INDEX IF NOT EXISTS idx_tm_research_events_received_at
    ON tm_research_events (received_at DESC);

CREATE INDEX IF NOT EXISTS idx_tm_research_events_payload_gin
    ON tm_research_events USING GIN (payload);
