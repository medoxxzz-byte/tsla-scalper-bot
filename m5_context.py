"""Transparent, non-executing M5 context score for the dashboard.

This score is a dashboard explanation layer only.  It must never place orders,
open a Paper position, or replace the user-visible CALL/PUT direction gauge.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


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
    """Return an explainable M5 context score for the current dominant direction.

    Seven equally visible checks are used: direction, EMA9 position, normalized
    momentum, OBV slope, volume participation, RSI zone, and MACD improvement.
    Volume is deliberately two-part: the live volume ratio must be at least
    0.80x *and* the existing reversal-volume gauge must be at least 40.
    The output remains a research/dashboard classification; it is not a signal
    and deliberately does not evaluate location (VWAP/support/decision zone),
    which must be verified separately before any human decision.
    """
    direction = _direction_from_potential(call_potential, put_potential)
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

    is_call = direction == "CALL"
    checks = {
        "الاتجاه": True,
        "EMA9": _has_number(price) and _has_number(ema9) and (price > ema9 if is_call else price < ema9),
        "MOM": _has_number(momentum_atr) and (momentum_atr > 0 if is_call else momentum_atr < 0),
        "OBV": _has_number(obv_slope) and (obv_slope > 0 if is_call else obv_slope < 0),
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
