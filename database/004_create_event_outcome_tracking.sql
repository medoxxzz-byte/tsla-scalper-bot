-- TM Sniper Phase B: append-only post-event observations and derived outcomes.
-- This migration never modifies raw research-event rows or their webhook receipts.

CREATE TABLE IF NOT EXISTS public.event_observations (
    observation_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES public.tm_research_events(id) ON DELETE RESTRICT,
    observation_key CHAR(64) NOT NULL UNIQUE,
    tracking_version TEXT NOT NULL,
    observation_type TEXT NOT NULL,
    observed_bar_open_time TIMESTAMPTZ NOT NULL,
    observed_bar_close_time TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    price_open NUMERIC(12, 4) NOT NULL,
    price_high NUMERIC(12, 4) NOT NULL,
    price_low NUMERIC(12, 4) NOT NULL,
    price_close NUMERIC(12, 4) NOT NULL,
    volume NUMERIC(20, 4) NULL,
    vwap NUMERIC(12, 4) NULL,
    cmf NUMERIC(12, 6) NULL,
    relative_volume NUMERIC(12, 4) NULL,

    cumulative_high NUMERIC(12, 4) NOT NULL,
    cumulative_low NUMERIC(12, 4) NOT NULL,
    upside_extension NUMERIC(12, 4) NOT NULL,
    downside_extension NUMERIC(12, 4) NOT NULL,
    mfe_dollars NUMERIC(12, 4) NULL,
    mae_dollars NUMERIC(12, 4) NULL,
    target_030_reached BOOLEAN NULL,
    target_060_reached BOOLEAN NULL,
    target_030_at TIMESTAMPTZ NULL,
    target_060_at TIMESTAMPTZ NULL,
    inside_zone BOOLEAN NULL,
    invalidation_met BOOLEAN NOT NULL DEFAULT FALSE,
    same_bar_ambiguity BOOLEAN NOT NULL DEFAULT FALSE,

    tracking_payload JSONB NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    reconciliation_mode TEXT NOT NULL DEFAULT 'direct'
        CHECK (reconciliation_mode IN ('direct', 'reconciled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT event_observations_bar_duration_check
        CHECK (observed_bar_close_time > observed_bar_open_time),
    CONSTRAINT event_observations_price_range_check
        CHECK (price_high >= price_low),
    CONSTRAINT event_observations_cumulative_range_check
        CHECK (cumulative_high >= cumulative_low),
    CONSTRAINT event_observations_target_time_check
        CHECK (
            (target_030_reached IS NOT TRUE OR target_030_at IS NOT NULL)
            AND (target_060_reached IS NOT TRUE OR target_060_at IS NOT NULL)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_observations_event_point
    ON public.event_observations (event_id, observation_type, observed_bar_close_time);

CREATE INDEX IF NOT EXISTS idx_event_observations_event_time
    ON public.event_observations (event_id, observed_bar_close_time ASC);

CREATE TABLE IF NOT EXISTS public.event_observation_receipts (
    receipt_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NULL REFERENCES public.tm_research_events(id) ON DELETE RESTRICT,
    observation_id BIGINT NULL REFERENCES public.event_observations(observation_id) ON DELETE RESTRICT,
    parent_event_key CHAR(64) NULL,
    observation_key CHAR(64) NULL,
    source TEXT NOT NULL CHECK (source IN ('v17', 'v18')),
    observation_type TEXT NULL,
    observed_bar_close_time TIMESTAMPTZ NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_hash CHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL,
    receipt_status TEXT NOT NULL
        CHECK (receipt_status IN ('new', 'duplicate', 'conflict', 'invalid', 'orphan')),
    conflict_fields JSONB NULL,
    validation_errors JSONB NULL,
    orphan_reason TEXT NULL,
    processor_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT event_observation_receipts_status_details_check CHECK (
        (receipt_status = 'conflict' AND conflict_fields IS NOT NULL AND validation_errors IS NULL)
        OR (receipt_status = 'invalid' AND validation_errors IS NOT NULL)
        OR (receipt_status = 'orphan' AND orphan_reason IS NOT NULL AND validation_errors IS NULL)
        OR (receipt_status IN ('new', 'duplicate') AND conflict_fields IS NULL AND validation_errors IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_event_observation_receipts_event_time
    ON public.event_observation_receipts (event_id, received_at ASC);

CREATE INDEX IF NOT EXISTS idx_event_observation_receipts_parent_key
    ON public.event_observation_receipts (parent_event_key, receipt_id ASC)
    WHERE parent_event_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_event_observation_receipts_observation_key
    ON public.event_observation_receipts (observation_key, receipt_id ASC)
    WHERE observation_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.event_observation_orphan_reconciliations (
    reconciliation_id BIGSERIAL PRIMARY KEY,
    orphan_receipt_id BIGINT NOT NULL UNIQUE
        REFERENCES public.event_observation_receipts(receipt_id) ON DELETE RESTRICT,
    event_id BIGINT NOT NULL REFERENCES public.tm_research_events(id) ON DELETE RESTRICT,
    observation_id BIGINT NOT NULL REFERENCES public.event_observations(observation_id) ON DELETE RESTRICT,
    reconciliation_method TEXT NOT NULL DEFAULT 'parent_arrival'
        CHECK (reconciliation_method IN ('parent_arrival', 'manual_repair')),
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.event_outcomes (
    outcome_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES public.tm_research_events(id) ON DELETE RESTRICT,
    outcome_version TEXT NOT NULL,
    final_observation_id BIGINT NOT NULL REFERENCES public.event_observations(observation_id) ON DELETE RESTRICT,
    status TEXT NOT NULL
        CHECK (status IN ('initial_extension', 'invalidated', 'neutral', 'non_directional_map', 'ambiguous', 'censored_at_session_close')),
    direction TEXT NULL CHECK (direction IN ('CALL', 'PUT')),
    mfe_dollars NUMERIC(12, 4) NULL,
    mae_dollars NUMERIC(12, 4) NULL,
    upside_extension NUMERIC(12, 4) NOT NULL,
    downside_extension NUMERIC(12, 4) NOT NULL,
    target_030_reached BOOLEAN NULL,
    target_060_reached BOOLEAN NULL,
    target_030_at TIMESTAMPTZ NULL,
    target_060_at TIMESTAMPTZ NULL,
    invalidated_at TIMESTAMPTZ NULL,
    same_bar_ambiguity BOOLEAN NOT NULL DEFAULT FALSE,
    final_observation_at TIMESTAMPTZ NOT NULL,
    final_price NUMERIC(12, 4) NOT NULL,
    classification_reason JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT event_outcomes_one_version_per_event UNIQUE (event_id, outcome_version)
);

CREATE INDEX IF NOT EXISTS idx_event_outcomes_version_status
    ON public.event_outcomes (outcome_version, status, computed_at DESC);

CREATE OR REPLACE VIEW public.event_tracking_summary AS
SELECT
    e.id AS event_id,
    e.event_key,
    e.source,
    e.action,
    e.direction,
    e.data_quality_tier,
    e.bar_close_time,
    o.outcome_version,
    o.status AS outcome_status,
    o.mfe_dollars,
    o.mae_dollars,
    o.upside_extension,
    o.downside_extension,
    o.target_030_reached,
    o.target_060_reached,
    o.target_030_at,
    o.target_060_at,
    o.invalidated_at,
    o.same_bar_ambiguity,
    o.final_observation_at,
    o.final_price
FROM public.tm_research_events e
LEFT JOIN public.event_outcomes o ON o.event_id = e.id
WHERE e.data_quality_tier = 'official';
