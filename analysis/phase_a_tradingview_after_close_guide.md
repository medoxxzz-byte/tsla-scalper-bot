# دليل ما بعد الإغلاق: تحديث V17 وV18 إلى هوية `event-v2`

**نفذ هذا الدليل بعد إغلاق السوق الأمريكي فقط.** هذا تحديث لحفظ البحث، وليس تحديثاً لمنطق الإشارة أو رسالة Telegram أو مواعيد V17/V18.

## قبل البدء

احتفظ بالسكريبتين الحاليين كنسخة رجوع داخل Pine Editor عبر **Save As** أو نسخة مكررة. لا تحذف GEX ولا أي تنبيه آخر خارج V17 وV18. المطلوب هو استبدال نص V17 وV18 ثم إعادة إنشاء تنبيه webhook لكل واحد لأن TradingView يحتفظ بلقطة من السكربت في وقت إنشاء التنبيه.

## تحديث V17 على فريم 5 دقائق

افتح شارت TSLA على فريم 5 دقائق. افتح Pine Editor، ثم افتح السكربت **TM Reversal Map V17**. استبدل النص كاملاً بالنص من ملف `tm_reversal_map_v17.pine`، ثم اضغط **Save** و **Add to chart** أو **Update on chart**. لا تغير أي Input.

بعد نجاح الحفظ، افتح قائمة Alerts. احذف فقط التنبيه القديم المرتبط بـV17 أو عطله. أنشئ تنبيهاً جديداً بالشروط التالية:

| الحقل | القيمة |
|---|---|
| Condition | `TM Reversal Map V17` ثم `Any alert() function call` |
| Trigger | `Once Per Bar Close` |
| Webhook URL | `https://tsla-scalper-bot.onrender.com/reversal_map` |
| Message | اتركه كما يضبطه `alert()` في Pine؛ لا تكتب JSON يدوياً |

احفظ التنبيه. سيظل V17 على فريم خمس دقائق وبالخرائط والمواعيد نفسها. الاختلاف الوحيد أن الحمولة أصبحت ترفق هوية الشمعة العلمية.

## تحديث V18 على فريم دقيقة

افتح شارت TSLA على فريم دقيقة. في Pine Editor، افتح **TM Reversal Experiments V18**. استبدل النص كاملاً بالنص من ملف `tm_reversal_experiments_v18.pine`، ثم اضغط **Save** و **Add to chart** أو **Update on chart**. لا تعدل نوافذ 10:05–10:35 أو 15:30–15:55، ولا تغير Inputs.

احذف أو عطل فقط التنبيه القديم المرتبط بـV18 ثم أنشئ تنبيهاً جديداً:

| الحقل | القيمة |
|---|---|
| Condition | `TM Reversal Experiments V18` ثم `Any alert() function call` |
| Trigger | `Once Per Bar Close` |
| Webhook URL | `https://tsla-scalper-bot.onrender.com/reversal_experiments` |
| Message | اتركه للـ`alert()` داخل Pine؛ لا تكتب JSON يدوياً |

## فحص النجاح

لا نحتاج إلى فتح صفقة أو انتظار نتيجة سعرية. يكفي أن يظهر السكربتان بلا أخطاء وأن يكون لكل سكربت تنبيه واحد فقط يرسل إلى endpoint الصحيح. تم بالفعل اختبار Render بحمولة مطابقة لصيغة Pine، وتحقق أن خدمة التخزين ترد بـ`identity_version: event-v2`.

في أول يوم تداول لاحق، يبدأ أول event طبيعي يحمل `event-v2` طبقة `official`. لا تدخل الاختبارات الموسومة `test` ولا صفوف Pilot القديمة في التحليل. إذا وصلت رسالة Telegram قبل تحديث Pine، ستصل للمستخدم كالمعتاد، لكن لا تعتبر حدثاً رسمياً في الدراسة.

## المراجع

[1]: https://github.com/medoxxzz-byte/tsla-scalper-bot/blob/main/tm_reversal_map_v17.pine "TM Reversal Map V17 with event-v2 identity"
[2]: https://github.com/medoxxzz-byte/tsla-scalper-bot/blob/main/tm_reversal_experiments_v18.pine "TM Reversal Experiments V18 with event-v2 identity"
