"""
FlashAlpha GEX Integration — V6.0
يسحب مستويات Gamma Exposure لـ TSLA قبل التداول.

الاستخدام:
  - يُستدعى مرة واحدة صباحاً (قبل 9:30 AM ET)
  - يحدد: gamma_flip, أعلى مستوى GEX (مقاومة), أدنى مستوى GEX (دعم)
  - يرسل خريطة المستويات على Telegram
  - يوفر بيانات للبوت لتأكيد/رفض الصفقات

Free Tier: 5 requests/day — كافي لنا (1-2 قبل الافتتاح + 1-2 أثناء التداول)
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

try:
    import requests as http_requests
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "requests"], check=True)
    import requests as http_requests

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
FLASHALPHA_API_KEY = os.environ.get("FLASHALPHA_API_KEY", "")
FLASHALPHA_BASE_URL = "https://lab.flashalpha.com/v1"
SYMBOL = "TSLA"

# ── State ────────────────────────────────────────────────────────────────────
_gex_data = {
    "gamma_flip": None,
    "net_gex_label": None,      # "positive" or "negative"
    "call_wall": None,          # highest call GEX strike (resistance)
    "put_wall": None,           # highest put GEX strike (support)
    "top_strikes": [],          # top 5 GEX strikes
    "last_updated": None,
    "raw_response": None,
}


def get_et_now():
    """Get current Eastern Time."""
    return datetime.now(timezone.utc) - timedelta(hours=4)


def fetch_gex(expiration=None):
    """
    Fetch GEX data from FlashAlpha API.
    
    Args:
        expiration: Optional date string 'yyyy-MM-dd' for specific expiry.
                   If None and on Free plan, uses today's date (0DTE).
    
    Returns:
        dict with GEX data or None on failure.
    """
    if not FLASHALPHA_API_KEY:
        logger.warning("[FlashAlpha] No API key configured — skipping GEX fetch")
        return None

    # For Free plan: must specify single expiration
    # Default to today (0DTE) which is what we trade
    if expiration is None:
        today = get_et_now().strftime("%Y-%m-%d")
        expiration = today

    url = f"{FLASHALPHA_BASE_URL}/exposure/gex/{SYMBOL}"
    headers = {"X-Api-Key": FLASHALPHA_API_KEY}
    params = {"expiration": expiration}

    try:
        resp = http_requests.get(url, headers=headers, params=params, timeout=15)
        
        if resp.status_code == 200:
            data = resp.json()
            _process_gex_response(data)
            logger.info(f"[FlashAlpha] GEX fetched successfully | gamma_flip={_gex_data['gamma_flip']}")
            return _gex_data
        elif resp.status_code == 404:
            logger.warning(f"[FlashAlpha] No GEX data for {SYMBOL} exp={expiration}")
            return None
        elif resp.status_code == 429:
            logger.warning("[FlashAlpha] Rate limit reached (5/day on Free plan)")
            return None
        else:
            logger.error(f"[FlashAlpha] API error {resp.status_code}: {resp.text[:200]}")
            return None

    except Exception as e:
        logger.error(f"[FlashAlpha] Request failed: {e}")
        return None


def _process_gex_response(data):
    """Process raw API response into usable trading levels."""
    global _gex_data

    _gex_data["gamma_flip"] = data.get("gamma_flip")
    _gex_data["net_gex_label"] = data.get("net_gex_label", "unknown")
    _gex_data["last_updated"] = get_et_now().strftime("%Y-%m-%d %H:%M ET")
    _gex_data["raw_response"] = data

    strikes = data.get("strikes", [])
    if not strikes:
        return

    # Find Call Wall (highest call_gex strike)
    call_sorted = sorted(strikes, key=lambda s: s.get("call_gex", 0), reverse=True)
    if call_sorted:
        _gex_data["call_wall"] = call_sorted[0]["strike"]

    # Find Put Wall (highest put_gex strike)
    put_sorted = sorted(strikes, key=lambda s: s.get("put_gex", 0), reverse=True)
    if put_sorted:
        _gex_data["put_wall"] = put_sorted[0]["strike"]

    # Top 5 strikes by absolute net GEX
    top_sorted = sorted(strikes, key=lambda s: abs(s.get("net_gex", 0)), reverse=True)[:5]
    _gex_data["top_strikes"] = [
        {"strike": s["strike"], "net_gex": s["net_gex"]}
        for s in top_sorted
    ]


def get_gex_levels():
    """
    Get current GEX levels (cached from last fetch).
    
    Returns:
        dict with gamma_flip, call_wall, put_wall, net_gex_label
        or None if no data available.
    """
    if _gex_data["gamma_flip"] is None:
        return None
    return _gex_data


def check_gex_alignment(signal, current_price):
    """
    Check if a trade signal aligns with GEX levels.
    
    Args:
        signal: "CALL" or "PUT"
        current_price: Current TSLA price
    
    Returns:
        (aligned: bool, reason: str)
    """
    if _gex_data["gamma_flip"] is None:
        return True, "GEX data unavailable — no filter applied"

    gamma_flip = _gex_data["gamma_flip"]
    call_wall = _gex_data["call_wall"]
    put_wall = _gex_data["put_wall"]
    regime = _gex_data["net_gex_label"]

    reasons = []

    if signal == "CALL":
        # CALL near call_wall (resistance) = risky
        if call_wall and current_price >= call_wall * 0.995:
            reasons.append(f"⚠️ السعر عند Call Wall (${call_wall}) — مقاومة GEX")
        # CALL below gamma_flip in positive regime = good (mean-reverting bounce)
        if regime == "positive" and current_price < gamma_flip:
            reasons.append(f"✅ أسفل Gamma Flip (${gamma_flip}) + Positive GEX = ارتداد متوقع")
        # CALL above gamma_flip in negative regime = trending up
        if regime == "negative" and current_price > gamma_flip:
            reasons.append(f"✅ فوق Gamma Flip + Negative GEX = ترند صاعد")

    elif signal == "PUT":
        # PUT near put_wall (support) = risky
        if put_wall and current_price <= put_wall * 1.005:
            reasons.append(f"⚠️ السعر عند Put Wall (${put_wall}) — دعم GEX")
        # PUT above gamma_flip in positive regime = good (mean-reverting drop)
        if regime == "positive" and current_price > gamma_flip:
            reasons.append(f"✅ فوق Gamma Flip (${gamma_flip}) + Positive GEX = هبوط متوقع")
        # PUT below gamma_flip in negative regime = trending down
        if regime == "negative" and current_price < gamma_flip:
            reasons.append(f"✅ أسفل Gamma Flip + Negative GEX = ترند هابط")

    # Determine alignment
    has_warning = any("⚠️" in r for r in reasons)
    has_confirm = any("✅" in r for r in reasons)

    if has_warning and not has_confirm:
        return False, " | ".join(reasons)
    
    return True, " | ".join(reasons) if reasons else "GEX neutral"


def format_gex_telegram():
    """Format GEX data as Telegram message for morning map."""
    if _gex_data["gamma_flip"] is None:
        return None

    regime_emoji = "🟢" if _gex_data["net_gex_label"] == "positive" else "🔴"
    regime_text = "Mean-Reverting (Range)" if _gex_data["net_gex_label"] == "positive" else "Trending (Volatile)"

    msg = (
        f"📊 <b>خريطة GEX — TSLA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 <b>Gamma Flip:</b> ${_gex_data['gamma_flip']:.2f}\n"
        f"🟩 <b>Call Wall (مقاومة):</b> ${_gex_data['call_wall']:.2f}\n"
        f"🟥 <b>Put Wall (دعم):</b> ${_gex_data['put_wall']:.2f}\n"
        f"{regime_emoji} <b>النظام:</b> {regime_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if _gex_data["top_strikes"]:
        msg += "<b>أعلى 5 مستويات GEX:</b>\n"
        for s in _gex_data["top_strikes"]:
            gex_dir = "+" if s["net_gex"] > 0 else ""
            msg += f"  • ${s['strike']:.0f} → {gex_dir}{s['net_gex']/1e6:.1f}M\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {_gex_data['last_updated']}\n"
        f"📡 FlashAlpha Free Tier"
    )

    return msg
