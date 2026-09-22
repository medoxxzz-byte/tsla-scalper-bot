#!/usr/bin/env bash
# Safe live Phase-B orphan test. Uses the deliberately muted V17 test action.
# It creates only test-tier data; it never emits a Telegram trading message.
set -euo pipefail

endpoint="https://tsla-scalper-bot.onrender.com/reversal_map"
marker="live-orphan-$(date -u +%Y%m%dT%H%M%SZ)-$$"
base_seconds=$(( ( $(date -u +%s) + 7200 ) / 300 * 300 ))
parent_open_ms=$(( base_seconds * 1000 ))
parent_close_ms=$(( parent_open_ms + 300000 ))
observed_open_ms="$parent_close_ms"
observed_close_ms=$(( parent_close_ms + 300000 ))

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

observation_payload="$workdir/orphan_observation.json"
parent_payload="$workdir/test_parent.json"
orphan_response="$workdir/orphan_response.json"
parent_response="$workdir/parent_response.json"

cat > "$observation_payload" <<JSON
{
  "tracking_version": "tracking-v1",
  "parent_schema_version": "event-v2",
  "parent_research_acceptance_test": true,
  "parent_symbol": "TSLA",
  "parent_timeframe": "5m",
  "parent_bar_open_time": "$parent_open_ms",
  "parent_bar_close_time": "$parent_close_ms",
  "parent_action": "STORAGE_ACCEPTANCE_TEST",
  "parent_support": 360.00,
  "parent_resistance": 361.00,
  "parent_range_low": null,
  "parent_range_high": null,
  "parent_zone_half_width": 0.10,
  "observation_type": "post_5m",
  "observed_bar_open_time": "$observed_open_ms",
  "observed_bar_close_time": "$observed_close_ms",
  "price_open": 360.12,
  "price_high": 360.52,
  "price_low": 360.05,
  "price_close": 360.48,
  "volume": 100000,
  "vwap": 360.20,
  "cmf": 0.12,
  "relative_volume": 1.10,
  "cumulative_high": 360.52,
  "cumulative_low": 360.05,
  "acceptance_test_marker": "$marker"
}
JSON

cat > "$parent_payload" <<JSON
{
  "schema_version": "event-v2",
  "symbol": "TSLA",
  "timeframe": "5m",
  "bar_open_time": "$parent_open_ms",
  "bar_close_time": "$parent_close_ms",
  "action": "STORAGE_ACCEPTANCE_TEST",
  "price": 360.12,
  "vwap": 359.90,
  "support": 360.00,
  "resistance": 361.00,
  "zone_half_width": 0.10,
  "cmf": 0.08,
  "obv_bull": true,
  "relative_volume": 1.10,
  "hist": 0.04,
  "momentum": 0.35,
  "adx": 25.0,
  "stoch_k": 55.0,
  "bull_score": 2,
  "bear_score": 0,
  "close_above_vwap": true,
  "_research_acceptance_test": true,
  "acceptance_test_marker": "$marker"
}
JSON

curl -fsS --max-time 60 -H 'Content-Type: application/json' \
  --data @"$observation_payload" "$endpoint" > "$orphan_response"

orphan_status="$(grep -o '"status":"[^"]*"' "$orphan_response" | head -n 1 | cut -d'"' -f4)"
parent_event_key="$(grep -o '"parent_event_key":"[a-f0-9]*"' "$orphan_response" | head -n 1 | cut -d'"' -f4)"
if [[ "$orphan_status" != "orphan" || ! "$parent_event_key" =~ ^[a-f0-9]{64}$ ]]; then
  echo "Unexpected orphan response:" >&2
  cat "$orphan_response" >&2
  exit 1
fi

curl -fsS --max-time 60 -H 'Content-Type: application/json' \
  --data @"$parent_payload" "$endpoint" > "$parent_response"

parent_storage_status="$(grep -o '"event_store":{[^}]*' "$parent_response" | grep -o '"status":"[^"]*"' | head -n 1 | cut -d'"' -f4)"
actual_event_key="$(grep -o '"event_key":"[a-f0-9]*"' "$parent_response" | head -n 1 | cut -d'"' -f4)"
if [[ "$parent_storage_status" != "new" || "$actual_event_key" != "$parent_event_key" ]]; then
  echo "Unexpected parent response:" >&2
  cat "$parent_response" >&2
  exit 1
fi

printf '{"marker":"%s","orphan_status":"%s","parent_storage_status":"%s","event_key":"%s","parent_open_time":"%s","parent_close_time":"%s","observed_close_time":"%s"}\n' \
  "$marker" "$orphan_status" "$parent_storage_status" "$actual_event_key" "$parent_open_ms" "$parent_close_ms" "$observed_close_ms"
