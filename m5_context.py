"""Transparent, non-executing M5 context scores for the dashboard.

Both versions are explanation layers only. They must never place orders, open a
Paper position, or replace the user-visible CALL/PUT direction gauge.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence


_MAX_CHECKS = 7


def _has_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _direction_from_potential(call_potential: Any, put_potential: Any) -> str:
    if not _has_number(call_potential) or not _has_number(put_potential):
        return "NEUTRAL"
    if call_potential >= 55 and call_potential > put_potential:
        return "CALL"
    if put_potential >= 55 and put_potential > call_potential:
        return "PUT"
    return "NEUTRAL"


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def normalized_obv_gauge(
    obv_values: Sequence[float], *, lookback: int = 20, slope_span: int = 5
) -> int:
    """Return an OBV gauge normalized against recent OBV slopes (0-100).

    V1 compared the current slope with its own absolute value, so any non-zero
    slope displayed as 0 or 100. V2 compares the current five-bar OBV slope to
    the median magnitude of rolling five-bar slopes over the latest 20 bars.
    ``tanh`` bounds outliers smoothly, preserving a meaningful neutral 50.
    """
    if len(obv_values) < slope_span + 2:
        return 50

    start = max(slope_span, len(obv_values) - lookback)
    slopes = [
        float(obv_values[index] - obv_values[index - slope_span])
        for index in range(start, len(obv_values))
    ]
    magnitudes = [abs(slope) for slope in slopes if slope != 0]
    baseline = _median(magnitudes)
    if baseline <= 0:
        return 50

    current_slope = slopes[-1]
    # ±42 around neutral leaves headroom and prevents routine slopes from
    # being rendered as false all-or-nothing 0/100 readings.
    gauge = 50 + 42 * math.tanh((current_slope / baseline) / 1.5)
    return int(round(max(0, min(100, gauge))))


def _classify_context(direction: str, checks: Dict[str, bool]) -> Dict[str, Any]:
    if direction == "NEUTRAL":
        return {
            "direction": "NEUTRAL",
            "score": 0,
            "max_score": _MAX_CHECKS,
            "gauge": 0,
            "status": "لا قرار",
            "summary": "الاتجاه غير محسوم؛ انتظر خروج العداد الكبير من الحياد.",
            "missing": ["اتجاه CALL أو PUT واضح"],
            "checks": {},
        }

    score = sum(bool(value) for value in checks.values())
    missing = [name for name, passed in checks.items() if not passed]

    if score <= 2:
        status = f"اتجاه {direction} ضعيف"
        summary = "لا تطارد الحركة؛ السياق لا يملك تأكيدات كافية بعد."
    elif score <= 4:
        status = f"مراقبة {direction}"
        summary = "يوجد ميل أولي، لكن انتظر تحسن العناصر الناقصة وإغلاق 5 دقائق."
    elif score <= 6:
        status = f"مرشح {direction}"
        summary = "السياق متماسك نسبياً؛ يبقى شرط المكان وإغلاق 5 دقائق."
    else:
        status = f"سياق {direction} متوافق"
        summary = "التوافق مرتفع؛ لا يزال المكان وإغلاق 5 دقائق شرطين منفصلين."

    return {
        "direction": direction,
        "score": score,
        "max_score": _MAX_CHECKS,
        "gauge": round(score / _MAX_CHECKS * 100),
        "status": status,
        "summary": summary,
        "missing": missing,
        "checks": checks,
    }


def _base_checks(
    *,
    direction: str,
    price: Any,
    ema9: Any,
    momentum_atr: Any,
    vol_ratio: Any,
    vol_reversal: Any,
    rsi: Any,
    macd_curr: Any,
    macd_prev: Any,
) -> Dict[str, bool]:
    is_call = direction == "CALL"
    return {
        "الاتجاه": True,
        "EMA9": _has_number(price) and _has_number(ema9) and (price > ema9 if is_call else price < ema9),
        "MOM": _has_number(momentum_atr) and (momentum_atr > 0 if is_call else momentum_atr < 0),
        "الحجم": (
            _has_number(vol_ratio)
            and _has_number(vol_reversal)
            and vol_ratio >= 0.80
            and vol_reversal >= 40
        ),
        "RSI": _has_number(rsi) and (52 <= rsi <= 68 if is_call else 32 <= rsi <= 48),
        "MACD": (
            _has_number(macd_curr)
            and _has_number(macd_prev)
            and (macd_curr > macd_prev if is_call else macd_curr < macd_prev)
        ),
    }


def build_m5_context(
    *,
    call_potential: Any,
    put_potential: Any,
    price: Any,
    ema9: Any,
    momentum_atr: Any,
    obv_slope: Any,
    vol_ratio: Any,
    vol_reversal: Any,
    rsi: Any,
    macd_curr: Any,
    macd_prev: Any,
) -> Dict[str, Any]:
    """Build the original V1 M5 context using the sign of the OBV slope."""
    direction = _direction_from_potential(call_potential, put_potential)
    if direction == "NEUTRAL":
        return _classify_context(direction, {})

    is_call = direction == "CALL"
    checks = _base_checks(
        direction=direction, price=price, ema9=ema9, momentum_atr=momentum_atr,
        vol_ratio=vol_ratio, vol_reversal=vol_reversal, rsi=rsi,
        macd_curr=macd_curr, macd_prev=macd_prev,
    )
    checks["OBV"] = _has_number(obv_slope) and (obv_slope > 0 if is_call else obv_slope < 0)
    return _classify_context(direction, checks)


def build_m5_context_v2(
    *,
    call_potential: Any,
    put_potential: Any,
    price: Any,
    ema9: Any,
    momentum_atr: Any,
    obv_gauge_v2: Any,
    vol_ratio: Any,
    vol_reversal: Any,
    rsi: Any,
    macd_curr: Any,
    macd_prev: Any,
) -> Dict[str, Any]:
    """Build experimental V2 M5 context using normalized OBV strength.

    V2 requires normalized OBV to be >=55 for CALL or <=45 for PUT. This keeps
    the original seven-check structure but avoids V1's all-or-nothing OBV read.
    """
    direction = _direction_from_potential(call_potential, put_potential)
    if direction == "NEUTRAL":
        return _classify_context(direction, {})

    is_call = direction == "CALL"
    checks = _base_checks(
        direction=direction, price=price, ema9=ema9, momentum_atr=momentum_atr,
        vol_ratio=vol_ratio, vol_reversal=vol_reversal, rsi=rsi,
        macd_curr=macd_curr, macd_prev=macd_prev,
    )
    checks["OBV"] = _has_number(obv_gauge_v2) and (obv_gauge_v2 >= 55 if is_call else obv_gauge_v2 <= 45)
    return _classify_context(direction, checks)
