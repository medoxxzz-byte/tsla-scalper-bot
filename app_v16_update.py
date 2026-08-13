"""
This file contains the logic for V16 MACD Journey & Shield.
It is intended to be integrated into app.py.
"""
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# --- V16 State ---
v16_state = {
    "journey_active": False,
    "direction": None,
    "start_time": None,
    "last_pulse_time": 0,
    "shield_900_sent": False,
    "shield_915_sent": False,
    "shield_930_sent": False,
    "today_date": ""
}

def get_ksa_now():
    """Get current time in KSA (UTC+3)"""
    return datetime.now(timezone.utc) + timedelta(hours=3)

def format_shield_message(time_slot, day_of_week):
    """Generate dynamic psychological shield messages based on day and time."""
    messages = {
        0: { # Monday
            "900": "⏰ 9:00 مساءً | يا بطل، بداية أسبوع ممتازة. باقي نصف ساعة على النهاية. لملم أرباحك، السوق أخذ حقه والآن دورك تحمي كاشك.",
            "915": "⏰ 9:15 مساءً | 15 دقيقة تفصلنا عن الإغلاق الإجباري. لو كسبان 10 دولار، حطها بجيبك. إياك والتنويم المغناطيسي، أنت أقوى من الشاشة!",
            "930": "🛑 9:30 مساءً | وقت الخروج! صفّ المحفظة الآن، معك 10 دقائق ترسل لي لقطة الشاشة والمحفظة كاش. أنا فخور بانضباطك اليوم، نفّذ يا وحش!"
        },
        1: { # Tuesday
            "900": "⏰ 9:00 مساءً | هانت يا صديقي، نصف ساعة ونقفل. لا تخلي تذبذب آخر الوقت يسرق تعبك. استعد للتصفية.",
            "915": "⏰ 9:15 مساءً | ربع ساعة فقط! القرار الحديدي الآن يريّحك بكرة. اقطع أي أمل كاذب واقفل صفقاتك.",
            "930": "🛑 9:30 مساءً | انتهى الوقت! صفّ المحفظة فوراً. الكاش هو الملك. أرسل لي الشاشة نظيفة!"
        },
        2: { # Wednesday
            "900": "⏰ 9:00 مساءً | منتصف الأسبوع، حافظ على تركيزك. 30 دقيقة باقية، ابدأ بتجهيز أوامر البيع.",
            "915": "⏰ 9:15 مساءً | 15 دقيقة. لا تطمع في سنتات وتخسر دولارات. اقفل واطلع كسبان أو بخسارة صغيرة مقدور عليها.",
            "930": "🛑 9:30 مساءً | صافرة النهاية! خروج إجباري الآن. صوّر المحفظة وهي كاش وأرسلها. انضباطك هو رأس مالك الحقيقي."
        },
        3: { # Thursday
            "900": "⏰ 9:00 مساءً | الخميس دائماً خادع في نهايته. باقي 30 دقيقة، لا تترك صفقات مفتوحة لغدرات آخر الوقت.",
            "915": "⏰ 9:15 مساءً | 15 دقيقة فقط! احسم أمرك الآن. التردد هنا يكلفك غالي. اقفل وارتاح.",
            "930": "🛑 9:30 مساءً | وقت الإخلاء! المحفظة كاش الآن. أرسل لي الإثبات وروح ارتاح، بكرة يوم جديد."
        },
        4: { # Friday
            "900": "⏰ 9:00 مساءً | الجمعة (0DTE)! أخطر نصف ساعة في الأسبوع. الثيتا ستحرق كل شيء. استعد للهروب.",
            "915": "⏰ 9:15 مساءً | 15 دقيقة! الكازينو بيقفل. خذ فلوسك واهرب، لا تخليهم يصفرون عقدك. اقفل فوراً!",
            "930": "🛑 9:30 مساءً | انتهت اللعبة! تصفية شاملة الآن. ويكند سعيد ومحفظة آمنة. أرسل لي الشاشة وأنت بطل!"
        }
    }
    
    day_msgs = messages.get(day_of_week, messages[0])
    return day_msgs.get(time_slot, "")

def shield_scheduler_loop(send_telegram_func):
    """Background thread to check and send shield messages."""
    global v16_state
    logger.info("V16 Shield Scheduler Started.")
    
    while True:
        try:
            now_ksa = get_ksa_now()
            today_str = now_ksa.strftime("%Y-%m-%d")
            
            # Reset daily flags
            if v16_state["today_date"] != today_str:
                v16_state["today_date"] = today_str
                v16_state["shield_900_sent"] = False
                v16_state["shield_915_sent"] = False
                v16_state["shield_930_sent"] = False
            
            # Only run Monday to Friday (0 to 4)
            if now_ksa.weekday() <= 4:
                day = now_ksa.weekday()
                
                # Check 9:00 PM
                if now_ksa.hour == 21 and now_ksa.minute >= 0 and now_ksa.minute < 15 and not v16_state["shield_900_sent"]:
                    msg = format_shield_message("900", day)
                    send_telegram_func(msg)
                    v16_state["shield_900_sent"] = True
                    
                # Check 9:15 PM
                elif now_ksa.hour == 21 and now_ksa.minute >= 15 and now_ksa.minute < 30 and not v16_state["shield_915_sent"]:
                    msg = format_shield_message("915", day)
                    send_telegram_func(msg)
                    v16_state["shield_915_sent"] = True
                    
                # Check 9:30 PM
                elif now_ksa.hour == 21 and now_ksa.minute >= 30 and now_ksa.minute < 45 and not v16_state["shield_930_sent"]:
                    msg = format_shield_message("930", day)
                    send_telegram_func(msg)
                    v16_state["shield_930_sent"] = True
                    
        except Exception as e:
            logger.error(f"Shield scheduler error: {e}")
            
        time.sleep(60) # Check every minute

def process_v16_webhook(data, send_telegram_func):
    """Process incoming webhooks from V16 Pine Script."""
    global v16_state
    
    action = data.get("action", "")
    
    if action == "START_CALL":
        v16_state["journey_active"] = True
        v16_state["direction"] = "CALL"
        v16_state["start_time"] = time.time()
        macd = data.get("macd", "N/A")
        msg = f"🚀 [قنص 5 دقائق 🦅]\n🟢 انطلاق رحلة MACD (CALL)\nالماكد ({macd}) يتجه للصفر. استعد!"
        send_telegram_func(msg)
        
    elif action == "START_PUT":
        v16_state["journey_active"] = True
        v16_state["direction"] = "PUT"
        v16_state["start_time"] = time.time()
        macd = data.get("macd", "N/A")
        msg = f"🚀 [قنص 5 دقائق 🦅]\n🔴 انطلاق رحلة MACD (PUT)\nالماكد ({macd}) يتجه للصفر. استعد!"
        send_telegram_func(msg)
        
    elif action == "PULSE_3M" and v16_state["journey_active"]:
        support = data.get("support", False)
        if support:
            msg = f"📡 تحديث 3 دقائق:\n✅ السيولة (OBV/MOM) تدعم رحلتك. استمر."
        else:
            msg = f"📡 تحديث 3 دقائق:\n⚠️ انتبه! الزخم يضعف والسيولة تنسحب. جهز يدك للخروج."
        send_telegram_func(msg)
        
    elif action == "PULSE_5M" and v16_state["journey_active"]:
        adx = data.get("adx", "N/A")
        k = data.get("k", "N/A")
        msg = f"📊 تحديث 5 دقائق (الترند):\nقوة الترند (ADX): {adx}\nالتشبع (Stoch): {k}"
        send_telegram_func(msg)
        
    elif action == "TARGET_REACHED" and v16_state["journey_active"]:
        msg = f"🎯 [الهدف]\nوصلنا خط الصفر! اختراق الماكد تم بنجاح. اجنِ أرباحك الآن."
        send_telegram_func(msg)
        # Don't end journey here, wait for reversal to officially end it, 
        # or end it if user prefers. For now, we keep it active to catch reversal.
        
    elif action == "REVERSAL" and v16_state["journey_active"]:
        msg = f"🛑 [نهاية الرحلة]\nتقاطع عكسي! اخرج فوراً، انتهت الرحلة. الكاش ملك."
        send_telegram_func(msg)
        v16_state["journey_active"] = False
        v16_state["direction"] = None
        
    return {"status": "ok", "action": action}
