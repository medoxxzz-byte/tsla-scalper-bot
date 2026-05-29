import os, sys
sys.path.insert(0, '.')
from options_scalper import get_tsla_snapshot, get_tsla_bars, get_spy_direction, _rw_rsi, _rw_ema, _rw_obv

def calc_macd(closes):
    if len(closes) < 26:
        return None, None, None
    macd_series = []
    for i in range(26, len(closes)+1):
        ef = _rw_ema(closes[:i], 12)
        es = _rw_ema(closes[:i], 26)
        if ef and es:
            macd_series.append(ef - es)
    if len(macd_series) < 9:
        return None, None, None
    signal_series = []
    for i in range(9, len(macd_series)+1):
        signal_series.append(_rw_ema(macd_series[:i], 9))
    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    histogram = macd_line - signal_line if signal_line else None
    return macd_line, signal_line, histogram

def calc_atr(bars, period=14):
    if len(bars) < period+1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]['h'])
        l = float(bars[i]['l'])
        pc = float(bars[i-1]['c'])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period

def find_support_resistance(highs, lows, closes, current_price):
    """إيجاد مستويات الدعم والمقاومة من Swing Highs/Lows"""
    levels = []
    # Swing Highs
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            levels.append(('R', highs[i]))
    # Swing Lows
    for i in range(2, len(lows)-2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            levels.append(('S', lows[i]))
    
    # فلتر: أقرب 5 مستويات فوق وتحت السعر الحالي
    above = sorted([(t, p) for t, p in levels if p > current_price + 0.30], key=lambda x: x[1])[:4]
    below = sorted([(t, p) for t, p in levels if p < current_price - 0.30], key=lambda x: x[1], reverse=True)[:4]
    return above, below

snap = get_tsla_snapshot()
if not snap:
    print("ERROR: No snapshot")
    sys.exit(1)

price = snap['price']
vwap  = snap['vwap']
bid   = snap.get('bid', 0)
ask   = snap.get('ask', 0)

# 5M bars
bars_5m = get_tsla_bars('5Min', 80)
# 15M bars
bars_15m = get_tsla_bars('15Min', 40)
# 1D bars
bars_1d = get_tsla_bars('1Day', 20)

spy_dir, spy_chg = get_spy_direction()

print("=" * 55)
print(f"  🦟 TSLA — خارطة الانعكاسات | يوم الخميس")
print("=" * 55)
print(f"  السعر الحالي: ${price:.2f}")
print(f"  VWAP:         ${vwap:.2f}  ({'+' if price>vwap else ''}{price-vwap:.2f})")
print(f"  Bid/Ask:      ${bid:.2f} / ${ask:.2f}")
print(f"  SPY:          {spy_dir} ({spy_chg:+.3f}%)")
print()

if bars_5m and len(bars_5m) >= 20:
    closes_5m = [float(b['c']) for b in bars_5m]
    highs_5m  = [float(b['h']) for b in bars_5m]
    lows_5m   = [float(b['l']) for b in bars_5m]
    vols_5m   = [int(b['v']) for b in bars_5m]

    rsi_5m = _rw_rsi(closes_5m)
    macd_l, macd_s, macd_h = calc_macd(closes_5m)
    atr_5m = calc_atr(bars_5m)
    obv_5m = _rw_obv(closes_5m, vols_5m)
    obv_slope = obv_5m[-1] - obv_5m[-6] if len(obv_5m) >= 6 else None
    avg_vol = sum(vols_5m[-20:]) / 20

    # مستويات الدعم والمقاومة
    above, below = find_support_resistance(highs_5m, lows_5m, closes_5m, price)

    # نطاق اليوم
    high_today = max(highs_5m[-30:]) if len(highs_5m) >= 30 else max(highs_5m)
    low_today  = min(lows_5m[-30:]) if len(lows_5m) >= 30 else min(lows_5m)

    print("─" * 55)
    print("  📊 المؤشرات الفنية (5M)")
    print("─" * 55)
    print(f"  RSI (14):     {rsi_5m:.1f}  {'🔴 ذروة شراء' if rsi_5m>70 else '🟢 ذروة بيع' if rsi_5m<30 else '🟡 محايد'}")
    if macd_l is not None:
        print(f"  MACD Line:    {macd_l:.4f}")
        print(f"  MACD Signal:  {macd_s:.4f}")
        print(f"  MACD Hist:    {macd_h:.4f}  {'📈 صاعد' if macd_h and macd_h>0 else '📉 هابط'}")
    if atr_5m:
        print(f"  ATR (14):     ${atr_5m:.3f}  (تذبذب متوقع)")
    if obv_slope is not None:
        print(f"  OBV Slope:    {obv_slope:+,.0f}  {'📈 ضغط شراء' if obv_slope>0 else '📉 ضغط بيع'}")
    print(f"  Vol Ratio:    {vols_5m[-1]/avg_vol:.2f}x  (المتوسط: {avg_vol:,.0f})")
    print()

    print("─" * 55)
    print("  🗺️  خارطة الانعكاسات")
    print("─" * 55)
    print(f"  نطاق اليوم:  ${low_today:.2f}  ←→  ${high_today:.2f}")
    print()

    print("  🔴 مقاومات (فوق السعر):")
    if above:
        for t, lvl in above:
            dist = lvl - price
            print(f"     ${lvl:.2f}   (+${dist:.2f})")
    else:
        # fallback: أعلى 10 شمعات
        r1 = max(highs_5m[-10:])
        r2 = max(highs_5m[-20:])
        print(f"     ${r1:.2f}   (+${r1-price:.2f})  [أعلى 10 شمعات]")
        if abs(r2 - r1) > 0.50:
            print(f"     ${r2:.2f}   (+${r2-price:.2f})  [أعلى 20 شمعة]")

    print()
    print("  🟢 دعومات (تحت السعر):")
    if below:
        for t, lvl in below:
            dist = price - lvl
            print(f"     ${lvl:.2f}   (-${dist:.2f})")
    else:
        s1 = min(lows_5m[-10:])
        s2 = min(lows_5m[-20:])
        print(f"     ${s1:.2f}   (-${price-s1:.2f})  [أدنى 10 شمعات]")
        if abs(s2 - s1) > 0.50:
            print(f"     ${s2:.2f}   (-${price-s2:.2f})  [أدنى 20 شمعة]")

    print()
    print("─" * 55)
    print("  🎯 VWAP كمستوى محوري:")
    print(f"     VWAP: ${vwap:.2f}")
    if price > vwap:
        print(f"     ✅ السعر فوق VWAP (+${price-vwap:.2f}) — ميل صعودي")
    else:
        print(f"     ⚠️  السعر تحت VWAP (-${vwap-price:.2f}) — ميل هبوطي")

if bars_15m and len(bars_15m) >= 10:
    closes_15m = [float(b['c']) for b in bars_15m]
    highs_15m  = [float(b['h']) for b in bars_15m]
    lows_15m   = [float(b['l']) for b in bars_15m]
    rsi_15m = _rw_rsi(closes_15m)
    macd_l15, macd_s15, macd_h15 = calc_macd(closes_15m)
    print()
    print("─" * 55)
    print("  📊 15M (الاتجاه الأكبر)")
    print("─" * 55)
    print(f"  RSI (14):     {rsi_15m:.1f}  {'🔴 ذروة شراء' if rsi_15m>70 else '🟢 ذروة بيع' if rsi_15m<30 else '🟡 محايد'}")
    if macd_h15 is not None:
        print(f"  MACD Hist:    {macd_h15:.4f}  {'📈 صاعد' if macd_h15>0 else '📉 هابط'}")
    # مستوى مهم 15M
    r15 = max(highs_15m[-10:])
    s15 = min(lows_15m[-10:])
    print(f"  مقاومة 15M:  ${r15:.2f}  (+${r15-price:.2f})")
    print(f"  دعم 15M:     ${s15:.2f}  (-${price-s15:.2f})")

if bars_1d and len(bars_1d) >= 5:
    highs_1d = [float(b['h']) for b in bars_1d]
    lows_1d  = [float(b['l']) for b in bars_1d]
    closes_1d = [float(b['c']) for b in bars_1d]
    # أسبوعي
    week_high = max(highs_1d[-5:])
    week_low  = min(lows_1d[-5:])
    prev_close = closes_1d[-2] if len(closes_1d) >= 2 else closes_1d[-1]
    print()
    print("─" * 55)
    print("  📅 مستويات يومية مهمة")
    print("─" * 55)
    print(f"  إغلاق أمس:   ${prev_close:.2f}")
    print(f"  أعلى أسبوع:  ${week_high:.2f}  (+${week_high-price:.2f})")
    print(f"  أدنى أسبوع:  ${week_low:.2f}   (-${price-week_low:.2f})")

print()
print("─" * 55)
print("  ⚡ توصية Strategy D (Mosquito)")
print("─" * 55)
if bars_5m and len(bars_5m) >= 20:
    if price > vwap and rsi_5m and 45 <= rsi_5m <= 70:
        print("  ✅ الظروف مناسبة للـ CALL")
        print(f"     السعر فوق VWAP | RSI={rsi_5m:.1f}")
        print(f"     انتظر MACD Zero Cross (3M) صاعد")
    elif price < vwap and rsi_5m and 30 <= rsi_5m <= 55:
        print("  ✅ الظروف مناسبة للـ PUT")
        print(f"     السعر تحت VWAP | RSI={rsi_5m:.1f}")
        print(f"     انتظر MACD Zero Cross (3M) هابط")
    else:
        print(f"  ⏳ انتظر — RSI={rsi_5m:.1f if rsi_5m else 'N/A'}")
        print(f"     السعر {'فوق' if price>vwap else 'تحت'} VWAP")
print()
print("=" * 55)
print(f"  ⏰ السوق يفتح خلال دقائق — بالتوفيق! 🦟")
print("=" * 55)
