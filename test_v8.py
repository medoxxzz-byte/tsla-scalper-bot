"""
Test V8 Options Scalper - All Functions
"""
import os
os.environ.setdefault("ALPACA_KEY", "PKW3OHVLGGWGYCFMTCKDB435WA")
os.environ.setdefault("ALPACA_SECRET", "BeNQ9BiZ8AXDFhz8rFjCnHVAaGmGRahavnRBN7dj")
os.environ.setdefault("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

import options_scalper as v8

print("=" * 60)
print("  🧪 V8 OPTIONS SCALPER - FULL TEST")
print("=" * 60)

# ── Test 1: All functions exist ──
print("\n📋 Test 1: All functions exist")
funcs = [
    'detect_trend_multi', 'detect_pullback', 'compute_micro_sr',
    'compute_opening_range', 'is_safe_zone', 'get_tsla_bars',
    'get_tsla_snapshot', 'get_options_chain', 'find_atm_contract',
    'find_itm_contract', 'place_option_order', 'execute_atm_scalp',
    'execute_itm_pullback', 'monitor_atm_positions', 'monitor_itm_position',
    'force_close_all', 'engine_loop', 'start_scalper', 'stop_scalper',
    'get_scalper_status', 'load_pdh_pdl', 'get_nearest_psych_level',
    'build_option_symbol', 'check_risk', 'send_daily_summary',
    'get_account', 'get_positions', 'close_position', 'send_telegram'
]
for f in funcs:
    assert hasattr(v8, f), f'❌ Missing: {f}'
    print(f'  ✅ {f}')
print(f'  Total: {len(funcs)} functions verified')

# ── Test 2: Config constants ──
print("\n📋 Test 2: Config constants")
configs = {
    'SCALP_START_HOUR': 10, 'SCALP_END_HOUR': 12,
    'SCALP_END_MINUTE': 40, 'ATM_CONTRACTS': 2,
    'ATM_TP1_PCT': 0.05, 'ATM_TP2_PCT': 0.10,
    'ATM_SL_PCT': 0.25, 'ATM_REINFORCE_PCT': 0.15,
    'ITM_DELTA_MIN': 0.70, 'ITM_DELTA_MAX': 0.88,
    'ITM_TP_PCT': 0.40, 'ITM_SL_PCT': 0.16,
    'MAX_PORTFOLIO_LOSS': 7000.0,
    'MICRO_SR_LOOKBACK': 10, 'VWAP_DANGER_PCT': 0.002,
    'PSYCH_LEVEL_INTERVAL': 5.0,
}
for name, expected in configs.items():
    val = getattr(v8, name, None)
    assert val is not None, f'❌ Missing config: {name}'
    assert val == expected, f'❌ {name} = {val}, expected {expected}'
    print(f'  ✅ {name} = {val}')

# ── Test 3: Get bars (live API) ──
print("\n📋 Test 3: Get bars from Alpaca API")
bars_1m = v8.get_tsla_bars("1Min", 30)
bars_5m = v8.get_tsla_bars("5Min", 20)
bars_15m = v8.get_tsla_bars("15Min", 10)
print(f'  ✅ 1Min bars: {len(bars_1m)} candles')
print(f'  ✅ 5Min bars: {len(bars_5m)} candles')
print(f'  ✅ 15Min bars: {len(bars_15m)} candles')

if len(bars_5m) > 0:
    last = bars_5m[-1]
    print(f'  📊 Last 5Min: O={last["o"]:.2f} H={last["h"]:.2f} L={last["l"]:.2f} C={last["c"]:.2f} V={last["v"]}')

# ── Test 4: Snapshot ──
print("\n📋 Test 4: Get TSLA Snapshot")
snap = v8.get_tsla_snapshot()
if snap:
    price = snap.get("price", 0)
    vwap = snap.get("vwap", 0)
    print(f'  ✅ Price: ${price:.2f}')
    print(f'  ✅ VWAP: ${vwap:.2f}')
else:
    print('  ⚠️ Snapshot not available (market closed)')

# ── Test 5: PDH/PDL ──
print("\n📋 Test 5: Previous Day High/Low")
pdh_pdl = v8.load_pdh_pdl()
if pdh_pdl:
    print(f'  ✅ PDH: ${pdh_pdl.get("pdh", 0):.2f}')
    print(f'  ✅ PDL: ${pdh_pdl.get("pdl", 0):.2f}')
else:
    print('  ⚠️ No PDH/PDL data')

# ── Test 6: Micro S/R ──
print("\n📋 Test 6: Compute Micro S/R")
sr = v8.compute_micro_sr()
if sr:
    print(f'  ✅ Supports: {[f"${s:.2f}" for s in sr.get("supports", [])]}')
    print(f'  ✅ Resistances: {[f"${r:.2f}" for r in sr.get("resistances", [])]}')
else:
    print('  ⚠️ No S/R data (market may be closed)')

# ── Test 7: Psychological Levels ──
print("\n📋 Test 7: Psychological Levels")
test_prices = [442.3, 444.8, 447.5, 449.9, 452.1]
for p in test_prices:
    nearest, nearest_dist = v8.get_nearest_psych_level(p)
    dist = abs(p - nearest)
    pct = nearest_dist * 100
    print(f'  ${p:.1f} → nearest psych: ${nearest:.0f} (dist: ${dist:.1f} = {pct:.2f}%)')

# ── Test 8: Trend Detection Multi-TF ──
print("\n📋 Test 8: Detect Trend (Multi-Timeframe)")
trend_dir, trend_str, trend_details = v8.detect_trend_multi()
if trend_dir:
    print(f'  ✅ Direction: {trend_dir}')
    print(f'  ✅ Strength: {trend_str}')
    print(f'  ✅ 15m: {trend_details.get("15m", "N/A")}')
    print(f'  ✅ 5m: {trend_details.get("5m", "N/A")}')
    print(f'  ✅ 1m: {trend_details.get("1m", "N/A")}')
    print(f'  ✅ VWAP: {trend_details.get("vwap", "N/A")} ({trend_details.get("vwap_side", "")})')
else:
    print(f'  ⚠️ No trend detected — Details: {trend_details}')

# ── Test 9: Pullback Detection ──
print("\n📋 Test 9: Detect Pullback")
pb_found, pb_depth = v8.detect_pullback("BULL")
print(f'  ✅ Pullback (BULL): found={pb_found}, depth={pb_depth:.2%}')
pb2_found, pb2_depth = v8.detect_pullback("BEAR")
print(f'  ✅ Pullback (BEAR): found={pb2_found}, depth={pb2_depth:.2%}')

# ── Test 10: Safe Zone Check ──
print("\n📋 Test 10: Safe Zone Check")
test_prices = [440.0, 445.0, 447.5, 450.0, 455.0]
for p in test_prices:
    is_safe, reason = v8.is_safe_zone(p, "BULL")
    status = "✅ SAFE" if is_safe else "⚠️ BLOCKED"
    print(f'  ${p:.2f}: {status} — {reason}')

# ── Test 11: Options Chain ──
print("\n📋 Test 11: Options Chain")
today = v8._today_expiry()
print(f'  📅 Today expiry: {today}')
chain = v8.get_options_chain(today, "call")
if chain:
    print(f'  ✅ Found {len(chain)} call contracts')
    for c in chain[:3]:
        sym = c.get("symbol", "?")
        strike = c.get("strike_price", "?")
        print(f'    📄 {sym} | Strike: ${strike}')
else:
    print('  ⚠️ No options chain (market closed or no 0DTE today)')

# ── Test 12: Build Option Symbol ──
print("\n📋 Test 12: Build Option Symbol")
sym = v8.build_option_symbol("TSLA", "2026-05-19", "call", 445)
print(f'  ✅ Symbol: {sym}')
sym2 = v8.build_option_symbol("TSLA", "2026-05-19", "put", 440)
print(f'  ✅ Symbol: {sym2}')

# ── Test 13: Account Check ──
print("\n📋 Test 13: Account & Portfolio Protection")
acct = v8.get_account()
if acct:
    equity = float(acct.get("equity", 0))
    print(f'  ✅ Equity: ${equity:,.2f}')
    print(f'  ✅ Portfolio Start: ${v8.PORTFOLIO_START:,.2f}')
    print(f'  ✅ Max Loss Allowed: ${v8.MAX_PORTFOLIO_LOSS:,.2f}')
    print(f'  ✅ Kill Switch at: ${v8.PORTFOLIO_START - v8.MAX_PORTFOLIO_LOSS:,.2f}')

# ── Test 14: Risk Check ──
print("\n📋 Test 14: Risk Check")
risk = v8.check_risk()
print(f'  ✅ Risk status: {risk}')

# ── Test 15: Scalper Status ──
print("\n📋 Test 15: Scalper Status")
status = v8.get_scalper_status()
print(f'  ✅ Status: {status}')

print("\n" + "=" * 60)
print("  🎯 ALL 15 TESTS COMPLETE!")
print("=" * 60)
