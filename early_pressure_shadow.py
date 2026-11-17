"""Early Pressure Shadow v1.

Read-only analytical state engine. It never sends Telegram, places orders, or
changes any existing strategy. The engine accepts already-fetched bars and
metadata so tests can prove state transitions without network access.
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

RULE_VERSION = "ep-v1"
SCHEMA_VERSION = "shadow-v1"
BREAKOUT_BUFFER_PCT = 0.001  # fixed 0.10%, before any evaluation
EVALUATION_HORIZON_MINUTES = 30
MIN_CANDIDATE_SCORE = 2
MIN_CANDIDATE_VALID_EVIDENCE = 3
STRONG_SCORE = 4
STRONG_VALID_EVIDENCE = 4


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _bar_time(bar: Dict[str, Any]) -> Optional[str]:
    return bar.get("t") or bar.get("timestamp") or bar.get("time")


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _closed(bar: Dict[str, Any], timeframe_seconds: int, now: datetime) -> bool:
    dt = _parse_time(_bar_time(bar))
    return bool(dt and now >= dt and now >= dt + timedelta(seconds=timeframe_seconds))


def _ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _obv(values: Sequence[float], volumes: Sequence[float]) -> List[float]:
    if not values:
        return []
    result = [0.0]
    for idx in range(1, len(values)):
        if values[idx] > values[idx - 1]:
            result.append(result[-1] + volumes[idx])
        elif values[idx] < values[idx - 1]:
            result.append(result[-1] - volumes[idx])
        else:
            result.append(result[-1])
    return result


def _atr(bars: Sequence[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for idx in range(1, len(bars)):
        high = _num(bars[idx].get("h"))
        low = _num(bars[idx].get("l"))
        prev_close = _num(bars[idx - 1].get("c"))
        if None in (high, low, prev_close):
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period if len(trs) >= period else None


def _session_vwap(bars: Sequence[Dict[str, Any]]) -> Optional[float]:
    total_pv = 0.0
    total_volume = 0.0
    for bar in bars:
        high = _num(bar.get("h"))
        low = _num(bar.get("l"))
        close = _num(bar.get("c"))
        volume = _num(bar.get("v"))
        if None in (high, low, close, volume) or volume <= 0:
            continue
        total_pv += ((high + low + close) / 3.0) * volume
        total_volume += volume
    return total_pv / total_volume if total_volume else None


def compute_1m_features(bars: Sequence[Dict[str, Any]], *, vwap: Optional[float] = None) -> Dict[str, Any]:
    """Compute the frozen, repeatable 1M feature contract.

    Windows: Volume Ratio 20 prior bars, Momentum 12 bars, ATR 14 bars,
    OBV slope over the last 5 OBV steps, EMA9, optional VWAP.
    """
    closes = [_num(b.get("c")) for b in bars]
    volumes = [_num(b.get("v")) for b in bars]
    if any(value is None for value in closes + volumes):
        return {"valid": False, "missing": ["ohlcv"]}
    closes = [float(v) for v in closes]
    volumes = [float(v) for v in volumes]
    if len(closes) < 26:
        return {"valid": False, "missing": ["bars_26_required"]}

    obv_values = _obv(closes, volumes)
    obv_slope = obv_values[-1] - obv_values[-6] if len(obv_values) >= 6 else None
    avg_volume = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
    vol_ratio = volumes[-1] / avg_volume if avg_volume and avg_volume > 0 else None
    momentum = closes[-1] - closes[-13]
    atr14 = _atr(bars, 14)
    momentum_atr = momentum / atr14 if atr14 and atr14 > 0 else None
    ema9 = _ema(closes, 9)
    derived_vwap = vwap if vwap is not None else _session_vwap(bars)
    return {
        "valid": True,
        "price": closes[-1],
        "volume": volumes[-1],
        "avg_volume_20": avg_volume,
        "vol_ratio": vol_ratio,
        "ema9": ema9,
        "obv_slope": obv_slope,
        "momentum": momentum,
        "atr14": atr14,
        "momentum_atr": momentum_atr,
        "vwap": derived_vwap,
        "bar_time": _bar_time(bars[-1]),
        "windows": {"volume_ratio": 20, "momentum": 12, "atr": 14, "obv_slope": 5, "ema": 9},
    }


def score_direction(features: Dict[str, Any], direction: str) -> Dict[str, Any]:
    price = _num(features.get("price"))
    ema9 = _num(features.get("ema9"))
    vwap = _num(features.get("vwap"))
    obv = _num(features.get("obv_slope"))
    mom = _num(features.get("momentum_atr"))
    vol = _num(features.get("vol_ratio"))
    checks = []
    if direction == "CALL":
        checks = [
            ("price_above_ema9", price is not None and ema9 is not None and price > ema9),
            ("price_above_vwap", None if vwap is None else price is not None and price > vwap),
            ("obv_positive", None if obv is None else obv > 0),
            ("momentum_positive", None if mom is None else mom > 0),
            ("volume_active", None if vol is None else vol >= 1.0),
        ]
    else:
        checks = [
            ("price_below_ema9", price is not None and ema9 is not None and price < ema9),
            ("price_below_vwap", None if vwap is None else price is not None and price < vwap),
            ("obv_negative", None if obv is None else obv < 0),
            ("momentum_negative", None if mom is None else mom < 0),
            ("volume_active", None if vol is None else vol >= 1.0),
        ]
    supporting = [name for name, passed in checks if passed is True]
    opposing = [name for name, passed in checks if passed is False]
    missing = [name for name, passed in checks if passed is None]
    return {"direction": direction, "score": len(supporting), "valid_evidence": len(supporting) + len(opposing), "supporting": supporting, "opposing": opposing, "missing": missing}


def select_reference(levels: Iterable[Dict[str, Any]], price: float) -> Optional[Dict[str, Any]]:
    """Stable priority: ORB, GEX, then S/R/pivot; nearest within priority."""
    priority = {"ORB": 1, "GEX": 2, "Support/Resistance": 3, "S/R": 3, "Psychology": 4, "VWAP": 5}
    candidates = []
    for level in levels or []:
        level_price = _num(level.get("price"))
        if level_price is None:
            continue
        source = str(level.get("source") or "")
        name = str(level.get("name") or "")
        source_key = next((key for key in priority if key.lower() in source.lower() or key.lower() in name.lower()), None)
        rank = priority.get(source_key, 9)
        candidates.append((rank, abs(level_price - price) / level_price, level))
    if not candidates:
        return None
    return dict(sorted(candidates, key=lambda item: (item[0], item[1]))[0][2])


class EarlyPressureEngine:
    def __init__(self, log_path: Optional[str] = None):
        self.lock = threading.RLock()
        self.state = "Monitoring"
        self.direction = "NEUTRAL"
        self.cycle_id = 0
        self.reference = None
        self.last_bar_time = None
        self.last_observation_key = None
        self.history = deque(maxlen=50)
        self.last_snapshot = None
        self.weak_count = 0
        self.log_path = Path(log_path or os.environ.get("EARLY_PRESSURE_LOG_PATH", "data/early_pressure_shadow.jsonl"))

    def _persist(self, payload: Dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _transition(self, new_state: str, direction: str, reason: str, evidence: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        if new_state == self.state and direction == self.direction:
            return None
        previous = self.state
        self.state = new_state
        self.direction = direction
        event = {
            "event_type": "state_transition",
            "from_state": previous,
            "to_state": new_state,
            "direction": direction,
            "state_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cycle_id": self.cycle_id,
            "reason": reason,
            "supporting_evidence": evidence.get("supporting", []),
            "opposing_evidence": evidence.get("opposing", []),
            "not_confirmed": evidence.get("missing", []),
            "rule_version": RULE_VERSION,
        }
        self.history.append(event)
        self._persist(event)
        return event

    def update(self, *, bars_1m: Sequence[Dict[str, Any]], bars_3m: Sequence[Dict[str, Any]] = (), bars_5m: Sequence[Dict[str, Any]] = (), levels: Sequence[Dict[str, Any]] = (), now: Optional[datetime] = None, quality: Optional[Dict[str, Any]] = None, vwap: Optional[float] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        quality = dict(quality or {})
        if quality.get("status") in {"ERROR", "UNKNOWN_TIMESTAMP", "INCOMPLETE", "AGED", "SESSION_CLOSED", "WEEKEND"}:
            snapshot = {
                "schema_version": SCHEMA_VERSION,
                "rule_version": RULE_VERSION,
                "state": self.state,
                "direction": self.direction,
                "cycle_id": self.cycle_id,
                "duplicate_observation": False,
                "features": {"valid": False, "missing": ["data_quality"]},
                "supporting_evidence": [],
                "opposing_evidence": [],
                "not_confirmed": ["data_quality"],
                "reference": self.reference,
                "transition": None,
                "events": list(self.history)[-10:],
                "data_quality": quality,
            }
            self.last_snapshot = snapshot
            return snapshot
        features = compute_1m_features(bars_1m, vwap=vwap)
        latest_bar_time = features.get("bar_time")
        observation_key = (latest_bar_time, RULE_VERSION)
        duplicate = observation_key == self.last_observation_key
        if latest_bar_time and not duplicate:
            self.last_observation_key = observation_key

        result = score_direction(features, "CALL") if features.get("valid") else {"direction": "NEUTRAL", "score": 0, "valid_evidence": 0, "supporting": [], "opposing": [], "missing": features.get("missing", [])}
        put_result = score_direction(features, "PUT") if features.get("valid") else {"direction": "PUT", "score": 0, "valid_evidence": 0, "supporting": [], "opposing": [], "missing": features.get("missing", [])}
        chosen = result if result["score"] >= put_result["score"] else put_result
        direction = chosen["direction"] if chosen["score"] >= MIN_CANDIDATE_SCORE else "NEUTRAL"
        selected_evidence = chosen if direction != "NEUTRAL" else {"supporting": [], "opposing": [], "missing": result.get("missing", [])}

        transition = None
        active_direction = self.direction if self.state in {"Building", "Strengthening"} and self.direction != "NEUTRAL" else direction
        active_evidence = score_direction(features, active_direction) if features.get("valid") and active_direction != "NEUTRAL" else selected_evidence
        if not duplicate and features.get("valid") and direction != "NEUTRAL":
            if self.reference is None:
                ref = select_reference(levels, float(features["price"]))
                if ref:
                    self.reference = {**ref, "frozen_at": latest_bar_time, "rule_version": RULE_VERSION}
            if self.reference is not None:
                if self.state == "Monitoring":
                    previous_candidates = [item for item in self.history if item.get("event_type") == "candidate_observation" and item.get("direction") == direction]
                    strong = chosen["score"] >= STRONG_SCORE and chosen["valid_evidence"] >= STRONG_VALID_EVIDENCE
                    if strong or previous_candidates:
                        self.cycle_id = self.cycle_id or 1
                        transition = self._transition("Building", direction, "1M pressure candidate repeated or strong", selected_evidence, now)
                    else:
                        candidate = {"event_type": "candidate_observation", "direction": direction, "bar_time": latest_bar_time, "cycle_id": self.cycle_id, "supporting_evidence": selected_evidence.get("supporting", [])}
                        self.history.append(candidate)
                        self._persist(candidate)
                elif self.state == "Building":
                    stronger = chosen["score"] >= STRONG_SCORE or (chosen["score"] >= 3 and "volume_active" in chosen.get("supporting", []))
                    if stronger:
                        transition = self._transition("Strengthening", direction, "1M evidence strengthened", selected_evidence, now)
                elif self.state == "Strengthening":
                    pass

            if self.state in {"Building", "Strengthening"}:
                active_score = active_evidence.get("score", 0)
                opposing = set(active_evidence.get("opposing", []))
                anchor_loss = {"price_above_ema9", "price_below_ema9", "price_above_vwap", "price_below_vwap"} & opposing
                flow_loss = {"obv_positive", "obv_negative", "momentum_positive", "momentum_negative"} & opposing
                if active_score < MIN_CANDIDATE_SCORE:
                    self.weak_count += 1
                elif anchor_loss and flow_loss:
                    self.weak_count += 1
                else:
                    self.weak_count = 0
                if self.weak_count >= 2:
                    transition = self._transition("Failed", active_direction, "confirmed multi-bar pressure weakness", active_evidence, now) or transition
                    self.weak_count = 0

        # Confirmed is allowed directly from Building or Strengthening.
        close_5m = _num(bars_5m[-1].get("c")) if bars_5m else None
        closed_5m = bool(bars_5m and _closed(bars_5m[-1], 300, now))
        if self.reference and close_5m is not None and closed_5m and self.state in {"Building", "Strengthening"}:
            ref_price = _num(self.reference.get("price"))
            if ref_price is not None:
                crossed = (active_direction == "CALL" and close_5m > ref_price * (1 + BREAKOUT_BUFFER_PCT)) or (active_direction == "PUT" and close_5m < ref_price * (1 - BREAKOUT_BUFFER_PCT))
                if crossed:
                    transition = self._transition("Confirmed", active_direction, "5M close crossed frozen reference", active_evidence, now) or transition

        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "rule_version": RULE_VERSION,
            "state": self.state,
            "direction": self.direction,
            "cycle_id": self.cycle_id,
            "duplicate_observation": duplicate,
            "features": features,
            "supporting_evidence": selected_evidence.get("supporting", []),
            "opposing_evidence": selected_evidence.get("opposing", []),
            "not_confirmed": selected_evidence.get("missing", []),
            "reference": self.reference,
            "transition": transition,
            "events": list(self.history)[-10:],
            "data_quality": quality,
        }
        self.last_snapshot = snapshot
        if latest_bar_time and not duplicate:
            self._persist({"event_type": "observation", "bar_time": latest_bar_time, "snapshot": snapshot})
        return snapshot

    def snapshot(self) -> Dict[str, Any]:
        return self.last_snapshot or {"schema_version": SCHEMA_VERSION, "rule_version": RULE_VERSION, "state": "Monitoring", "direction": "NEUTRAL", "events": []}


_default_engine = EarlyPressureEngine()


def get_default_engine() -> EarlyPressureEngine:
    return _default_engine
