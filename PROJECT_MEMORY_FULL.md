# ذاكرة مشروع TSLA Scalper Bot — الشاملة
## من البداية حتى 19 مايو 2026

---

## معلومات المشروع الأساسية

| البند | التفاصيل |
|-------|----------|
| اسم البوت | ثاقب — TSLA Options Scalper |
| GitHub | https://github.com/medoxxzz-byte/tsla-scalper-bot |
| GitHub User | medoxxzz-byte |
| Render URL | https://tsla-scalper-bot.onrender.com |
| Telegram Bot | @Tsla_Rm_bot |
| Telegram Token | 8701854195:AAHVmtrdxwyPBjtXMC-bU1ZCOnUBNafmtzA |
| Telegram Chat ID | 975644160 |
| Alpaca API Key | PKW3OHVLGGWGYCFMTCKDB435WA |
| Alpaca Secret | BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH |
| Alpaca Base URL | https://paper-api.alpaca.markets |
| Alpaca Account | PA3AQO4OWMTR — رصيد ~$99,408 |
| Alpaca Options Level | **3** ✅ (أعلى مستوى) |
| FlashAlpha API Key | srd7RXM1awDGPt6XkSuPpxHVnJD2XQsUu8UeYUZJ |
| FlashAlpha Plan | Free (5 requests/day) |

---

## رحلة التطوير — من البداية

---

### 🔹 V1-V3.3 — البداية (مارس 2026)

**الفكرة الأولى:** بوت يستقبل إشارات TradingView عبر Webhook ويرسلها على Telegram.

**ما تم بناؤه:**
- Webhook server بسيط (Flask)
- استقبال إشارات Pine Script من TradingView
- إرسال تنبيهات على Telegram
- Deploy على Render

**المشاكل:**
- الرسائل غير منظمة
- لا يوجد تنفيذ فعلي للصفقات
- مجرد إشعارات

---

### 🔹 V4.0-V4.1 — Phase 2 (مارس 2026)

**الإضافات:**
- Afternoon session
- Telegram commands
- Market digest
- Royal portfolio tracking

**المشاكل:**
- Webhook 500 error
- مشاكل في format الرسائل

---

### 🔹 V5.0-V5.4 — Mosquito (أبريل 2026)

**الفلسفة:** تحويل البوت من مجرد تنبيهات إلى نظام تداول ذكي.

**الإضافات:**
- Alpaca integration (شراء/بيع أسهم TSLA)
- Multi-timeframe analysis
- Reversal detector
- 10 أسهم لكل صفقة

**المشاكل:**
- يشتري ولا يبيع (لا TP/SL)
- يشتري غالي ويبيع رخيص

---

### 🔹 V6.0 — ADX + GEX (9 مايو 2026)

**الإضافات:**
- ADX filter: لا تداول إذا ADX < 20 (سوق جانبي)
- Stop Loss تلقائي: -20%
- FlashAlpha GEX integration
- GEX morning worker: يرسل خريطة Gamma على Telegram 9:15 AM ET
- نافذة التداول: 9:30 AM - 3:30 PM ET

---

### 🔹 V7.0 — Reversal Map (مايو 2026)

**الإضافات:**
- Reversal Map (خريطة الانعكاسات)
- Trend Alignment Filter
- Enhanced Telegram messages

---

### 🔹 V7.1 — ثاقب (مايو 2026)

**الإصلاحات:**
- Stars rating: Neutral=-1, Opposing=-2, 5 نجوم فقط إذا كل شيء متوافق
- فلتر 15m إجباري: يحجب التنفيذ إذا 15m يعارض الإشارة
- Reversal volume filter
- Fibonacci direction fix

**الجديد:**
- رسائل Telegram معاد تصميمها (عملية، مباشرة، عربية)
- Choppy market warning: "هالمكان حرق أعصاب وفلوس"
- Friday trading block (0DTE = كازينو)
- Daily limits: 6 تداول + 3 انعكاسات
- Daily loss limit: -$150
- اسم البوت: ثاقب (Mosquito V7.1)

---

### 🔹 V7.1.1 — FlashAlpha GEX Direct (13 مايو 2026)

**الإضافات:**
- FlashAlpha GEX Direct API integration
- Free tier: 2 fetches/day (9:25 AM + 10:00 AM ET)

---

### 🔹 V8.0 — Options Scalper Engine (15 مايو 2026)

**التحول الكبير:** من تداول أسهم إلى تداول **أوبشن 0DTE** بالكامل.

#### الطبقة 1: ATM Scalp (سكالبينج سريع)

| البند | القيمة |
|-------|--------|
| العقود | 2 عقود ATM (0DTE) |
| شرط الدخول | EMA9 > EMA21 + فوق VWAP + ADX > 20 |
| TP1 | +5% (بيع العقد 1) |
| TP2 | +10% (بيع العقد 2) |
| Stop Loss | -25% |
| تعزيز | عند -15% إذا الترند قائم → 2 عقود إضافية |

#### الطبقة 2: ITM Pullback (الجوهرة)

| البند | القيمة |
|-------|--------|
| العقود | 1 عقد ITM (Delta 0.70-0.88) |
| الدخول | Limit Buy تحت البولباك 1-2% |
| TP | +40% |
| SL | -16% |
| Risk:Reward | 1:2.5 |

#### نافذة التداول:

| البند | القيمة |
|-------|--------|
| البداية | 10:10 AM ET |
| النهاية | 12:40 PM ET |
| بيع إجباري | 12:45 PM ET |
| الأيام | الاثنين - الخميس |
| الجمعة | لا تداول |

#### إدارة المخاطر:

| البند | القيمة |
|-------|--------|
| حد خسارة المحفظة | -$7,000 |
| وقف بعد خسارات متتالية | 2 خسارات → وقف 30 دقيقة |

---

### 🔹 V8.1 — Multi-timeframe Enhancements (15 مايو 2026)

**الإضافات:**
- Multi-timeframe analysis
- Micro Support/Resistance
- VWAP zone detection
- PDH/PDL (Previous Day High/Low)
- Psychological levels ($5 increments)
- Opening range (أول 30 دقيقة)

---

### 🔹 V8.1.1 — Friday Trading (15 مايو 2026)

**التعديلات:**
- تفعيل تداول الجمعة (TSLA عندها 0DTE يومي)
- توسيع نافذة التداول: 9:35 AM - 3:45 PM ET للاختبار

---

## 🔹🔹🔹 V9 — الخطان (18-19 مايو 2026) 🔹🔹🔹

### التغيير الجوهري:
تحويل النظام من خط واحد تلقائي إلى **خطين متوازيين**:
- **الخط 1:** ATM تلقائي (3 عقود — استراتيجية Runner)
- **الخط 2:** ITM يدوي (واجهة ويب للجوال)

---

### الخط 1 — ATM (تلقائي) — V9

| المعلمة | القديم (V8) | الجديد (V9) |
|---------|------------|------------|
| العقود | 2 | **3 عقود** |
| TP1 | +5% بيع 1 | **+5% بيع C1+C2** |
| TP2 | +10% بيع 1 | **+10% بيع C3 Runner** |
| Stop Loss | -25% | **-10%** |
| تعزيز | ✅ | ❌ محذوف |

**المنطق:**
- C1 + C2: بيع عند +5% (ربح مضمون سريع)
- C3 Runner: يبقى حتى +10% (ربح إضافي)
- SL -10% على أي عقد (أكثر انضباطاً)

---

### الخط 2 — ITM Manual (جديد كلياً) — V9

**الرابط:** `https://tsla-scalper-bot.onrender.com/manual`

| الميزة | التفاصيل |
|--------|---------|
| العقود | 1 عقد ITM |
| Delta | 0.70-0.88 (اختيار تلقائي) |
| الدخول | زر CALL أو PUT — Market Order فوري |
| TP تلقائي | **+$0.20** (بيع تلقائي) |
| SL تلقائي | **-$0.65** (بيع تلقائي) |
| تنبيه 1 | **-$0.30** (اهتزاز جوال + Telegram ⚠️) |
| تنبيه 2 | **-$0.50** (اهتزاز + وميض أحمر + Telegram 🚨) |
| زر يدوي | 🛑 STOP LOSS — بيع فوري |
| تحديث | كل 5 ثواني |
| تصميم | Mobile-first (للجوال) |

**تنبيهات Telegram:**
- عند -$0.30: `⚠️ V9 تنبيه 1 — CALL/PUT`
- عند -$0.50: `🚨 V9 تحذير — قرار مطلوب الآن!`
- عند TP: `✅ ربح +$0.20`
- عند SL: `❌ خسارة -$0.65`

---

### API Endpoints الكاملة (V9):

| Endpoint | الوصف |
|----------|-------|
| `GET /` | الواجهة الرئيسية |
| `GET /v8/status` | حالة الخط 1 (ATM) |
| `POST /v8/start` | تشغيل الخط 1 |
| `POST /v8/stop` | إيقاف الخط 1 |
| `GET /manual` | واجهة الخط 2 (ITM Manual) |
| `GET /manual/status` | حالة الخط 2 + سعر TSLA + العقود المقترحة |
| `POST /manual/buy` | شراء عقد (CALL أو PUT) |
| `POST /manual/sell` | بيع العقد الحالي |

---

### Versions Timeline:

| الإصدار | التعديل | التاريخ |
|---------|---------|---------|
| V1-V3.3 | Webhook + Telegram alerts | مارس 2026 |
| V4.0-V4.1 | Phase 2 + Alpaca basic | مارس 2026 |
| V5.0-V5.4 | Mosquito + أسهم | أبريل 2026 |
| V6.0 | ADX + GEX + SL | 9 مايو 2026 |
| V7.0 | Reversal Map | مايو 2026 |
| V7.1 | ثاقب + Stars + Friday block | مايو 2026 |
| V7.1.1 | FlashAlpha GEX Direct | 13 مايو 2026 |
| V8.0 | Options Scalper Engine (0DTE) | 15 مايو 2026 |
| V8.1 | Multi-timeframe + S/R + VWAP | 15 مايو 2026 |
| V8.1.1 | Friday trading + نافذة موسّعة | 15 مايو 2026 |
| **V9.0** | **ATM 3 عقود + ITM Manual كامل** | **18 مايو 2026** |
| **V9.1** | **إعادة تصميم واجهة /manual** | **18 مايو 2026** |
| **V9.2** | **تنبيهات Telegram -$0.30 و-$0.50** | **19 مايو 2026** |

---

## الملفات الحالية للمشروع

| الملف | الوصف |
|-------|-------|
| `app.py` | Flask server — كل الـ routes (V7 + V8 + V9) |
| `options_scalper.py` | محرك التداول الكامل (ATM + ITM Manual) |
| `templates/manual.html` | واجهة الخط 2 للجوال |
| `templates/index.html` | الواجهة الرئيسية |
| `smart_scalp_v5_6.pine` | Pine Script V5.7 (TradingView) |
| `requirements.txt` | المكتبات |
| `Procfile` | gunicorn config |
| `PROJECT_MEMORY.md` | ذاكرة V8 (القديمة) |
| `PROJECT_MEMORY_FULL.md` | هذا الملف — الذاكرة الشاملة |
| `MEMORY.md` | ملخص جلسة 18-19 مايو |

---

## أسلوب المتداول (مهم للرسائل والقرارات)

- **سكالبر أوبشنز سريع** — يدخل ويطلع بدقائق (0DTE)
- **مغامر** — يحب التجربة ويتحمّل المخاطر في Paper Trading
- **يبي البوت يشتغل من نفسه** — بدون تدخل في القرارات (الخط 1)
- **يتحكم يدوياً** في الخط 2 عبر الجوال
- **لغة عامية سعودية** مع مصطلحات تداول إنجليزية
- **لا يتداول يوم الجمعة** (0DTE = كازينو) — لكن V8.1.1 فعّله للاختبار
- **يتعلم GEX** — فهم 75% من أول شرح
- **فكرة ITM Pullback** — فكرته الشخصية من تجربته

---

## دروس التداول المهمة

1. **لا تدخل PUT إذا CheddarFlow 90%+ CALL** — ضد التيار
2. **لا تتداول ضد GEX + CheddarFlow مجتمعَين** — انتحار
3. **TP = +50% | SL = -30% | وأقفل التطبيق** — الانضباط
4. **إذا وصلت هدفك اليومي = أقفل فوراً** — الطمع عدوّك
5. **لا تعزّز على خسارة** (V9 حذف التعزيز نهائياً)
6. **GEX فوق Gamma Flip = CALL آمن** — القاعدة الذهبية
7. **حط أوامر ونام** — أفضل من المراقبة
8. **"أحسن صفقة هي اللي ما دخلتها"** — يوم الشوبي لا تدخل
9. **Larry Williams: "أخسر كثير بس أخسر صغير — وأربح قليل بس أربح كبير"**
10. **C3 Runner** — فكرة V9: اترك عقداً يركض مع الترند بعد تأمين الربح

---

## شرح GEX (مرجع سريع)

| المصطلح | المعنى |
|---------|--------|
| Gamma Flip | خط الفاصل: فوقه = صناع السوق معك، تحته = ضدك |
| Call Wall | السقف: صعب يخترقه، لا تشتري CALL قربه |
| Put Wall | الأرضية: صعب ينزل تحته |
| Max +Gamma | المغناطيس: السعر ينجذب له |
| القاعدة | بين Gamma Flip و Call Wall = منطقة اللعب الآمنة |

---

## نتائج التداول الحقيقي (المتداول)

| الأسبوع | الحساب | الملاحظة |
|---------|--------|----------|
| 12 مايو | ~$1,594 | بداية الأسبوع |
| 13 مايو | +$240 (+15%) → $1,834 | أفضل صفقة CALL +$432 |
| 14 مايو | لا تداول | ارتاح |
| 15 مايو | +$14.09 → **$1,848.71** | حط أوامر ونام — قرار ذكي |

---

## ملاحظات تقنية مهمة

- V8/V9 يشتغل كـ background thread مستقل — لا يتعارض مع V7
- يستخدم Alpaca Bars API (IEX feed) لبيانات السعر
- يحسب EMA/VWAP/ADX/MOM من البيانات مباشرة (بدون Pine Script)
- يشيك كل 30 ثانية (ATM) / كل 10 ثواني (ITM Manual)
- Render Free Tier — ينام بعد 15 دقيقة (keep_alive_worker يحل المشكلة)
- Symbol format: `TSLA260519C00403000` (TSLA + تاريخ + C/P + Strike×1000)
- `position_intent`: `buy_to_open` للشراء، `sell_to_close` للبيع
- **0DTE فقط** — لا يدعم expiry أطول (قرار مقصود)
- الأزرار تعمل خارج نافذة التداول (لا حجب — قرار مقصود)

---

## القرارات المتخذة (لا تعيد النقاش)

| القرار | السبب |
|--------|-------|
| لا تعزيز في V9 | حذف من الاستراتيجية نهائياً |
| الأزرار تعمل 24/7 | المتداول يريد التحكم الكامل |
| 0DTE فقط | الاستراتيجية مبنية على 0DTE |
| لا حجب خارج النافذة | قرار المتداول |
| SL -10% بدل -25% | انضباط أكثر في V9 |

---

## الاشتراكات والخدمات

| الخدمة | الحالة | الرابط |
|--------|--------|--------|
| FlashAlpha | ✅ مجاني | — |
| CheddarFlow | ✅ عنده حساب | https://dash.cheddarflow.com/options-order-flow |
| Alpaca Paper | ✅ مجاني | https://paper-api.alpaca.markets |
| Render | ✅ Free Tier | https://tsla-scalper-bot.onrender.com |
| Telegram Bot | ✅ شغّال | @Tsla_Rm_bot |

---

*آخر تحديث: 19 مايو 2026 — V9.2*
