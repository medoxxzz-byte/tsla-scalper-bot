# مراجعة قبول المرحلة B على Neon المؤقتة

**التاريخ:** 23 سبتمبر 2026
**الحالة:** اجتازت بيئة Neon المؤقتة الاختبارات. لا يزال تطبيق الترحيل على Neon الحية معلقاً على موافقة صريحة.

## النتيجة

أصبحت طبقة المتابعة المقترحة قادرة على حفظ نتيجة ما بعد الحدث بصورة **append-only**. لا تُحدَّث إشارة V17 أو V18 الأصلية، ولا تُعدَّل receipts الخاصة بها. كل شمعة متابعة تصل في سجل منفصل، ثم تُشتق نتيجة `outcome-v1` عند آخر نقطة قياس قابلة للاعتماد.

## حدود القياس المثبتة

| المصدر | نقاط المتابعة الكاملة | النتيجة النهائية |
|---|---|---|
| V18 | 3 و6 و12 دقيقة | `post_12m` |
| V17 | 5 و10 و15 دقيقة | `post_15m` |
| V18 قرب الإغلاق أو V17 متأخر | حتى 16:00 ET | `censored_at_session_close`، وليست نتيجة كاملة |

إذا اجتمع هدف أو إلغاء في الشمعة نفسها، تحفظ النتيجة بعلامة `same_bar_ambiguity=true`، ولا يدّعي النظام ترتيب الحركة داخل الشمعة.

## اختبار الحدث الأب المفقود

تم اختبار حالة وصول متابعة قبل الأب. لم تُربط بأقرب حدث ولم تُنشئ نتيجة. سجلت كـ`orphan` مع الحمولة والمفتاح المتوقعين. بعد إدخال الأب المطابق بالهوية الحتمية فقط، أنشأ النظام observation بأسلوب `reconciled` وسجل reconciliation مستقل. يبقى receipt الأصلي orphan، لذلك يظل تاريخ الاستلام شفافاً ولا يعاد تشكيله.

## اختبارات القبول المنفذة

| الاختبار | النتيجة |
|---|---|
| إدخال متابعة قانونية | نجح: observation واحد وreceipt `new` |
| إعادة الحمولة نفسها | نجح: receipt `duplicate` بلا observation جديد |
| تغيير OHLC/أعلى تراكمي بالهوية نفسها | نجح: receipt `conflict` مع الحقول المختلفة |
| وقت متابعة خاطئ | نجح: receipt `invalid` بلا observation |
| event الأب المفقود | نجح: receipt `orphan` فقط |
| وصول الأب لاحقاً | نجح: reconciliation موثق، بلا تخمين |
| `post_12m` لاتجاه V18 | نجح: outcome `initial_extension` مشتق |
| حدث إغلاق لا يكمل النافذة | نجح: outcome `censored_at_session_close`، بلا أسعار after-hours |
| عزل Telegram | نجح: payloads ذات `tracking_version` تذهب إلى التخزين فقط |
| انضباط نقاط Pine | نجح: V17 عند 5/10/15 وV18 عند 3/6/12 |

## القرار المطلوب قبل الإنتاج

الترحيل الحي سيضيف فقط جداول `event_observations` و`event_observation_receipts` و`event_observation_orphan_reconciliations` و`event_outcomes`، مع view تحليلي `event_tracking_summary`. لن يحذف أو يغير أي صف من Pilot أو official أو test في `tm_research_events` أو `webhook_receipts`.

بعد الموافقة، يكون ترتيب التنفيذ: تطبيق ترحيل Neon الحي، ثم تحديث النسخة المنشورة، ثم توسيع النسخ الاحتياطي، ثم اختبار حي آمن واحد، وأخيراً استبدال V17 وV18 على TradingView **بعد إغلاق السوق فقط**.

## المراجع

[1]: https://www.tradingview.com/pine-script-docs/concepts/alerts/ "TradingView Pine Script Alerts Documentation"
[2]: https://github.com/medoxxzz-byte/tsla-scalper-bot/blob/main/analysis/phase_b_outcome_tracking_contract.md "TM Sniper Phase B Outcome Tracking Contract"
[3]: https://github.com/medoxxzz-byte/tsla-scalper-bot/blob/main/database/004_create_event_outcome_tracking.sql "TM Sniper Phase B Outcome Tracking Migration"
