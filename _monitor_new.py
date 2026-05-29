def _manual_monitor_loop():
    """
    حلقة مراقبة صفقة ITM اليدوي.
    - TP: +$0.80 → بيع الكل تلقائياً
    - SL قبل التعزيز: -$0.65 → بيع تلقائي
    - تعزيز: عند -$0.35 → شراء 2 عقد إضافي
    - TP بعد التعزيز: +$0.80 من سعر التعزيز → بيع الكل
    - SL بعد التعزيز: -$0.40 من سعر التعزيز → بيع الكل
    """
    global _manual_state
    
    logger.info("[V9 Manual] Monitor started (TP=$0.80, Reinforce@-$0.35)")
    
    while _manual_state["monitor_active"]:
        try:
            pos = _manual_state["position"]
            if not pos or pos.get("status") != "open":
                _manual_state["monitor_active"] = False
                break
            
            symbol = pos["symbol"]
            quote = get_option_quote(symbol)
            
            if not quote or quote["mid"] <= 0:
                time.sleep(5)
                continue
            
            current_price = quote["mid"]
            entry_price = pos["entry_price"]
            pnl_dollar = round(current_price - entry_price, 2)
            
            # تحديث الحالة
            pos["current_price"] = current_price
            total_qty = 1 + _manual_state["reinforce_qty"]
            pos["pnl"] = round(pnl_dollar * 100 * total_qty, 2)
            _manual_state["last_price"] = current_price
            _manual_state["pnl_dollar"] = pnl_dollar
            
            direction = "CALL 📈" if pos.get("type") == "call" else "PUT 📉"
            reinforce_done = _manual_state["reinforce_done"]
            reinforce_price = _manual_state["reinforce_price"]
            
            # ── مرحلة ما بعد التعزيز ──
            if reinforce_done and reinforce_price:
                tp_after = round(reinforce_price + ITM_REINFORCE_TP, 2)
                sl_after = round(reinforce_price - ITM_REINFORCE_SL, 2)
                
                # TP بعد التعزيز
                if current_price >= tp_after:
                    logger.info(f"[V9 Manual] TP (after reinforce) hit! ${current_price:.2f} >= ${tp_after:.2f}")
                    close_manual_itm_all(reason="TP")
                    break
                
                # SL بعد التعزيز
                if current_price <= sl_after:
                    logger.info(f"[V9 Manual] SL (after reinforce) hit! ${current_price:.2f} <= ${sl_after:.2f}")
                    close_manual_itm_all(reason="SL")
                    break
            
            # ── مرحلة ما قبل التعزيز ──
            else:
                # TP تلقائي: +$0.80 من سعر الدخول
                if current_price >= pos["tp_price"]:
                    logger.info(f"[V9 Manual] TP hit! ${current_price:.2f} >= ${pos['tp_price']:.2f}")
                    close_manual_itm(reason="TP")
                    break
                
                # SL تلقائي: -$0.65 من سعر الدخول (قبل التعزيز)
                if current_price <= pos["sl_price"]:
                    logger.info(f"[V9 Manual] SL hit! ${current_price:.2f} <= ${pos['sl_price']:.2f}")
                    close_manual_itm(reason="SL")
                    break
                
                # تعزيز: عند -$0.35
                if not reinforce_done and pnl_dollar <= -ITM_REINFORCE_TRIGGER:
                    logger.info(f"[V9 Manual] Reinforce triggered at ${current_price:.2f} (pnl={pnl_dollar:+.2f})")
                    reinforce_order = place_option_order(
                        symbol=symbol, qty=ITM_REINFORCE_QTY, side="buy",
                        order_type="market", position_intent="buy_to_open"
                    )
                    if reinforce_order:
                        _manual_state["reinforce_done"] = True
                        _manual_state["reinforce_price"] = current_price
                        _manual_state["reinforce_qty"] = ITM_REINFORCE_QTY
                        tp_after = round(current_price + ITM_REINFORCE_TP, 2)
                        sl_after = round(current_price - ITM_REINFORCE_SL, 2)
                        msg = (
                            f"🔄 <b>V9 تعزيز — {direction}</b>\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"📋 {symbol}\n"
                            f"📥 دخول أصلي: ${entry_price:.2f}\n"
                            f"🔄 سعر التعزيز: ${current_price:.2f}\n"
                            f"📊 +{ITM_REINFORCE_QTY} عقود إضافية (المجموع: 3 عقود)\n"
                            f"🎯 TP جديد: ${tp_after:.2f} (+$0.80 من سعر التعزيز)\n"
                            f"🛑 SL جديد: ${sl_after:.2f} (-$0.40 من سعر التعزيز)\n"
                            f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                        )
                        send_telegram(msg)
                        logger.info(f"[V9 Manual] Reinforce done: {ITM_REINFORCE_QTY} contracts @ ${current_price:.2f}")
                    else:
                        logger.error("[V9 Manual] Reinforce order FAILED")
            
            # ── تنبيه 1: -$0.20 ──
            if not _manual_state["alert1_sent"] and pnl_dollar <= -ITM_ALERT1_DOLLARS:
                _manual_state["alert1_sent"] = True
                logger.warning(f"[V9 Manual] ALERT 1: -$0.20 | ${current_price:.2f}")
                msg = (
                    f"⚠️ <b>V9 تنبيه — {direction}</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {pos.get('symbol', '')}\n"
                    f"💵 السعر: ${current_price:.2f} | دخول: ${entry_price:.2f}\n"
                    f"📉 خسارة: <b>-$0.20</b> (-$20)\n"
                    f"🔄 التعزيز سيبدأ عند -$0.35\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
            
            # ── تنبيه 2: -$0.35 (عند التعزيز) ──
            if not _manual_state["alert2_sent"] and pnl_dollar <= -ITM_ALERT2_DOLLARS:
                _manual_state["alert2_sent"] = True
                logger.warning(f"[V9 Manual] ALERT 2: -$0.35 (reinforce zone) | ${current_price:.2f}")
                msg = (
                    f"🚨 <b>V9 منطقة التعزيز — {direction}</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 {pos.get('symbol', '')}\n"
                    f"💵 السعر: ${current_price:.2f} | دخول: ${entry_price:.2f}\n"
                    f"📉 خسارة: <b>-$0.35</b>\n"
                    f"🔄 جاري التعزيز بـ {ITM_REINFORCE_QTY} عقود...\n"
                    f"🕐 {_et_now().strftime('%I:%M %p')} ET"
                )
                send_telegram(msg)
            
            time.sleep(5)
            
        except Exception as e:
            logger.error(f"[V9 Manual Monitor] Error: {e}")
            time.sleep(5)
    
    logger.info("[V9 Manual] Monitor stopped")
