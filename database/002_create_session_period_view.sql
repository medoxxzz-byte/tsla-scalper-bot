-- TM Sniper research-event session-period classification.
-- This view does not alter tm_research_events and intentionally excludes pilot rows
-- received before the classification rollout.

CREATE OR REPLACE VIEW public.tm_research_events_session_period AS
WITH new_events AS (
    SELECT e.*,
           CASE
               WHEN e.source_event_time ~ '^[0-9]{13}$'
                   THEN to_timestamp(e.source_event_time::numeric / 1000.0)
               WHEN e.source_event_time ~ '^[0-9]{10}$'
                   THEN to_timestamp(e.source_event_time::numeric)
               WHEN e.source_event_time ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
                   THEN e.source_event_time::timestamptz
               ELSE e.received_at
           END AS effective_event_time,
           CASE
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

