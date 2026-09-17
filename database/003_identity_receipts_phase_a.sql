-- TM Sniper Phase A: official event identity and immutable webhook receipts.
-- Safety contract:
--   * Existing Pilot rows remain raw and are only labelled by the new quality tier.
--   * Official events require Pine-provided identity fields in the application layer.
--   * Every accepted, duplicate, conflicting, or invalid webhook becomes a receipt.
--   * No receipt path updates the original event payload or indicator values.

ALTER TABLE public.tm_research_events
    ADD COLUMN IF NOT EXISTS schema_version TEXT NULL,
    ADD COLUMN IF NOT EXISTS signal_family TEXT NULL,
    ADD COLUMN IF NOT EXISTS symbol TEXT NULL,
    ADD COLUMN IF NOT EXISTS timeframe TEXT NULL,
    ADD COLUMN IF NOT EXISTS bar_open_time TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS bar_close_time TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS data_quality_tier TEXT NULL,
    ADD COLUMN IF NOT EXISTS payload_hash CHAR(64) NULL;

-- The raw payload of historical rows is intentionally untouched.  The label keeps
-- those rows out of the official research sample without pretending they possess
-- precise bar identity.
UPDATE public.tm_research_events
SET data_quality_tier = 'pilot_pre_identity_fix'
WHERE data_quality_tier IS NULL;

ALTER TABLE public.tm_research_events
    ALTER COLUMN data_quality_tier SET DEFAULT 'pilot_pre_identity_fix',
    ALTER COLUMN data_quality_tier SET NOT NULL;

ALTER TABLE public.tm_research_events
    ADD CONSTRAINT tm_research_events_data_quality_tier_check
    CHECK (data_quality_tier IN ('pilot_pre_identity_fix', 'official', 'test'));

ALTER TABLE public.tm_research_events
    ADD CONSTRAINT tm_research_events_official_identity_check
    CHECK (
        data_quality_tier = 'pilot_pre_identity_fix'
        OR (
            schema_version = 'event-v2'
            AND signal_family IS NOT NULL
            AND symbol IS NOT NULL
            AND timeframe IS NOT NULL
            AND bar_open_time IS NOT NULL
            AND bar_close_time IS NOT NULL
            AND bar_close_time > bar_open_time
            AND payload_hash IS NOT NULL
        )
    );

CREATE INDEX IF NOT EXISTS idx_tm_research_events_official_bar_close
    ON public.tm_research_events (data_quality_tier, bar_close_time DESC)
    WHERE data_quality_tier IN ('official', 'test');

CREATE INDEX IF NOT EXISTS idx_tm_research_events_identity_dimensions
    ON public.tm_research_events (source, signal_family, symbol, timeframe, bar_close_time DESC)
    WHERE data_quality_tier IN ('official', 'test');

CREATE TABLE IF NOT EXISTS public.webhook_receipts (
    receipt_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NULL REFERENCES public.tm_research_events(id) ON DELETE RESTRICT,
    claimed_event_key CHAR(64) NULL,
    source TEXT NOT NULL CHECK (source IN ('v17', 'v18')),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_hash CHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL,
    receipt_status TEXT NOT NULL CHECK (receipt_status IN ('new', 'duplicate', 'conflict', 'invalid')),
    conflict_fields JSONB NULL,
    validation_errors JSONB NULL,
    processor_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT webhook_receipts_status_details_check CHECK (
        (receipt_status = 'conflict' AND conflict_fields IS NOT NULL AND validation_errors IS NULL)
        OR (receipt_status = 'invalid' AND validation_errors IS NOT NULL)
        OR (receipt_status IN ('new', 'duplicate') AND conflict_fields IS NULL AND validation_errors IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_webhook_receipts_event_received
    ON public.webhook_receipts (event_id, received_at ASC);

CREATE INDEX IF NOT EXISTS idx_webhook_receipts_claimed_key
    ON public.webhook_receipts (claimed_event_key)
    WHERE claimed_event_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_webhook_receipts_status_received
    ON public.webhook_receipts (receipt_status, received_at DESC);

-- The existing time-of-day view remains analytical only.  Official events now use
-- the actual TradingView bar close; legacy Pilot rows keep their prior fallback.
DROP VIEW IF EXISTS public.tm_research_events_session_period;

CREATE VIEW public.tm_research_events_session_period AS
WITH new_events AS (
    SELECT e.*,
           CASE
               WHEN e.bar_close_time IS NOT NULL
                   THEN e.bar_close_time
               WHEN e.source_event_time ~ '^[0-9]{13}$'
                   THEN to_timestamp(e.source_event_time::numeric / 1000.0)
               WHEN e.source_event_time ~ '^[0-9]{10}$'
                   THEN to_timestamp(e.source_event_time::numeric)
               WHEN e.source_event_time ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
                   THEN e.source_event_time::timestamptz
               ELSE e.received_at
           END AS effective_event_time,
           CASE
               WHEN e.bar_close_time IS NOT NULL THEN 'bar_close_time'
               WHEN e.source_event_time ~ '^[0-9]{13}$'
                    OR e.source_event_time ~ '^[0-9]{10}$'
                    OR e.source_event_time ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
                   THEN 'source_event_time'
               ELSE 'received_at'
           END AS time_basis
    FROM public.tm_research_events e
    WHERE e.received_at >= TIMESTAMPTZ '2026-09-16 00:22:42+00'
)
SELECT n.*,
       n.effective_event_time AT TIME ZONE 'America/New_York' AS effective_event_time_et,
       CASE
           WHEN (n.effective_event_time AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
            AND (n.effective_event_time AT TIME ZONE 'America/New_York')::time < TIME '10:30'
               THEN 'opening'
           WHEN (n.effective_event_time AT TIME ZONE 'America/New_York')::time >= TIME '10:30'
            AND (n.effective_event_time AT TIME ZONE 'America/New_York')::time < TIME '15:30'
               THEN 'midday'
           WHEN (n.effective_event_time AT TIME ZONE 'America/New_York')::time >= TIME '15:30'
            AND (n.effective_event_time AT TIME ZONE 'America/New_York')::time < TIME '16:00'
               THEN 'closing'
           ELSE NULL
       END AS session_period
FROM new_events n;

CREATE VIEW public.tm_research_event_receipt_summary AS
SELECT
    e.id AS event_id,
    e.event_key,
    e.data_quality_tier,
    COUNT(r.receipt_id) FILTER (WHERE r.receipt_status = 'new') AS new_receipt_count,
    COUNT(r.receipt_id) FILTER (WHERE r.receipt_status = 'duplicate') AS duplicate_receipt_count,
    COUNT(r.receipt_id) FILTER (WHERE r.receipt_status = 'conflict') AS conflict_receipt_count,
    MIN(r.received_at) AS first_receipt_at,
    MAX(r.received_at) AS last_receipt_at
FROM public.tm_research_events e
LEFT JOIN public.webhook_receipts r ON r.event_id = e.id
GROUP BY e.id, e.event_key, e.data_quality_tier;
