# -*- coding: utf-8 -*-
"""
بوت تليجرام لتتبع السعرات الحرارية اليومية باستخدام Gemini (عبر SDK الموحّد google-genai)
================================================================
المكتبات المطلوبة (نفّذ هذا الأمر أولاً):

    pip install pyTelegramBotAPI google-genai Pillow

المتغيرات التي يجب عليك استبدالها قبل التشغيل (ابحث عن كلمة ضع_):
    1. TELEGRAM_BOT_TOKEN  -> احصل عليه من @BotFather على تليجرام
    2. GEMINI_API_KEY      -> احصل عليه من https://aistudio.google.com/app/apikey
"""

import sqlite3
import logging
import re
import html as html_lib
from datetime import datetime, timedelta
from io import BytesIO

import telebot
from telebot import types
from PIL import Image

from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError

# ============================================================
# 1) الإعدادات العامة - استبدل القيم التالية بقيمك الخاصة
# ============================================================
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
GEMINI_API_KEY = "GEMINI_API_KEY"

DB_PATH = "calories_bot.db"
MAX_IMAGE_SIZE = (800, 800)
# معرف حسابك على تليجرام (الأدمن) - له صلاحيات كاملة ولا يخضع لقيود الاشتراك/الحظر
ADMIN_ID = 1421302016
# ملاحظة: gemini-2.0-flash تم إيقافه نهائياً من جوجل في 1 يونيو 2026.
# gemini-2.5-flash هو البديل الحالي المتاح (وله موعد إيقاف مبدئي في أكتوبر 2026،
# فإذا احتجت خياراً أطول عمراً استخدم gemini-3.5-flash بدلاً منه).
MODEL_NAME = "gemini-2.5-flash"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# 2) البرومبت التوجيهي (System Instruction)
# ============================================================
SYSTEM_INSTRUCTION = """أنت خبير تغذية وذكاء اصطناعي فائق الدقة متصرف كمساعد شخصي لمراقبة السعرات الحرارية اليومية.

⚠️ قاعدة ذهبية هامة جداً (تصفير اليوم):
إذا كتب المستخدم عبارة "وقت النوم" أو "تصفير السعرات" أو "ابدأ يوماً جديداً"، فهذا يعني أنه أنهى يومه الحالي. في هذه الحالة فقط:
1. تجاهل حساب أي وجبات سابقة في الذاكرة.
2. اعتبر أن إجمالي السعرات السابقة أصبح الآن (0) سعرة حرارية.
3. رد عليه برسالة تشجيعية لطيفة باللغة العربية تخبره فيها أنه تم تصفير العداد بنجاح، وتتمنى له نوماً هنيئاً أو بداية يوم جديد موفقة، واذكر له أن العداد الآن جاهز استقبال وجبات الغد (0 سعرة).

في الحالات العادية (تحليل الوجبات):
1. راجع تاريخ المحادثة بالكامل من الذاكرة لتعرف الوجبات والسعرات التي أدخلها المستخدم "اليوم" (بعد آخر عملية تصفير).
2. قم بتحليل الصورة الجديدة أو النص المرسل بدقة عالية جداً. فكك الوجبة إلى مكوناتها الأساسية وقدر وزن كل مكون بالجرام تقريباً بناءً على ما تراه.
3. احسب السعرات الحرارية لكل مكون على حدة، ثم اجمعها لتعطي سعرات الوجبة الحالية.

طريقة صياغة الرد للوجبات (التزم بالتنسيق التالي حرفياً دون تعديل):

### 🍽️ تحليل الوجبة الحالية:
* [اسم المكون الأول] (الوزن التقريبي) ➡️ [عدد] سعرة حرارية.
* [اسم المكون الثاني] (الوزن التقريبي) ➡️ [عدد] سعرة حرارية.
🔥 **مجموع سعرات هذه الوجبة:** [عدد] سعرة حرارية.

---

### 📊 التقرير اليومي الإجمالي:
* **السعرات السابقة اليوم:** [قم بحساب السعرات من الوجبات السابقة المتواجدة في السياق، وإذا وجدت كلمة "وقت النوم" أو "تصفير" في المحادثة السابقة، اعتبر السعرات السابقة 0 فوراً] سعرة حرارية.
* 📈 **إجمالي ما تم تناوله اليوم حتى الآن:** [اجمع سعرات الوجبة الحالية + السعرات السابقة اليوم] سعرة حرارية.

---
💡 *ملاحظة:* [إذا كانت الصورة غير واضحة، ضع تقديراً ذكياً بناءً على خبرتك واسأل المستخدم سؤالاً واحداً قصيراً للتأكيد]."""

# ============================================================
# 3) تهيئة جيميني وتليجرام
# ============================================================
client = genai.Client(api_key=GEMINI_API_KEY)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")

# يتتبع الإجراء اللي ينتظره الأدمن بعد ضغط زر من لوحة التحكم
# مثال: admin_pending[ADMIN_ID] = "ban"  -> ننتظر منه إرسال user_id ليتم حظره
admin_pending: dict[int, str] = {}
# يخزن بيانات وسيطة لتسلسلات متعددة الخطوات (مثل: إرسال رسالة لمستخدم محدد -> ID ثم النص)
admin_pending_data: dict[int, dict] = {}

# ============================================================
# 4) طبقة قاعدة البيانات (SQLite) - الذاكرة التراكمية
# ============================================================

def init_db():
    """إنشاء الجداول إن لم تكن موجودة (ذاكرة المحادثة + إدارة المستخدمين)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_banned INTEGER NOT NULL DEFAULT 0,
            subscription_end TEXT,
            joined_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_message(user_id: int, role: str, content: str):
    """حفظ رسالة واحدة (من المستخدم أو من النموذج) في الذاكرة."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_memory (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(user_id: int):
    """
    إرجاع كامل سجل المحادثة لمستخدم معيّن بصيغة types.Content
    المتوافقة مع معامل history في client.chats.create().
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM chat_memory WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    history = []
    for role, content in rows:
        history.append(
            genai_types.Content(role=role, parts=[genai_types.Part.from_text(text=content)])
        )
    return history


# ============================================================
# 4.1) إدارة المستخدمين (تسجيل / حظر / اشتراكات بمدة محددة)
# ============================================================

def register_user(user_id: int, username: str | None, first_name: str | None) -> bool:
    """
    تسجيل المستخدم عند أول تفاعل، وتحديث اسمه إذا تغيّر لاحقاً.
    يرجع True إذا كان هذا أول ظهور للمستخدم (تسجيل جديد)، و False إذا كان مسجّلاً مسبقاً.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    exists = cur.fetchone()
    if exists:
        cur.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
            (username, first_name, user_id),
        )
        conn.commit()
        conn.close()
        return False
    else:
        cur.execute(
            "INSERT INTO users (user_id, username, first_name, is_banned, subscription_end, joined_at) "
            "VALUES (?, ?, ?, 0, NULL, ?)",
            (user_id, username, first_name, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return True


def notify_admin_new_user(user_id: int, username: str | None, first_name: str | None):
    """يرسل للأدمن إشعاراً فورياً عند انضمام مستخدم جديد، مع حالة اشتراكه."""
    status = get_subscription_status(user_id)
    sub_text = "✅ مشترك" if status["active"] else "❌ غير مشترك"
    text = (
        "🆕 <b>مستخدم جديد دخل البوت!</b>\n\n"
        f"🆔 <code>{user_id}</code>\n"
        f"👤 الاسم: {html_lib.escape(first_name or '—')}\n"
        f"🔗 المعرف: @{username if username else '—'}\n"
        f"💳 الحالة: {sub_text}"
    )
    try:
        bot.send_message(ADMIN_ID, text)
    except Exception as e:
        logger.warning("فشل إرسال إشعار مستخدم جديد للأدمن: %s", e)


def get_user(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_banned(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user and user["is_banned"])


def ban_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def unban_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_subscription_status(user_id: int) -> dict:
    """
    يرجع حالة الاشتراك: {"active": bool, "end": datetime|None, "remaining": timedelta|None}
    """
    user = get_user(user_id)
    if not user or not user["subscription_end"]:
        return {"active": False, "end": None, "remaining": None}

    end = datetime.fromisoformat(user["subscription_end"])
    now = datetime.now()
    if end > now:
        return {"active": True, "end": end, "remaining": end - now}
    return {"active": False, "end": end, "remaining": None}


def add_subscription_days(user_id: int, days: int) -> datetime:
    """
    يمدد الاشتراك: إذا كان فيه اشتراك فعّال يضيف الأيام فوق تاريخ انتهائه،
    وإذا ما فيه اشتراك (أو منتهي) يبدأ العدّ من الآن.
    يرجع تاريخ الانتهاء الجديد.
    """
    status = get_subscription_status(user_id)
    base = status["end"] if status["active"] else datetime.now()
    new_end = base + timedelta(days=days)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET subscription_end = ? WHERE user_id = ?",
        (new_end.isoformat(), user_id),
    )
    conn.commit()
    conn.close()
    return new_end


def set_subscription_days(user_id: int, days: int) -> datetime:
    """يحدد الاشتراك ليكون بالضبط N يوم من الآن (تجاهل أي رصيد سابق)."""
    new_end = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET subscription_end = ? WHERE user_id = ?",
        (new_end.isoformat(), user_id),
    )
    conn.commit()
    conn.close()
    return new_end


def cancel_subscription(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET subscription_end = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def list_all_users() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY joined_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_bot_stats() -> dict:
    users = list_all_users()
    total = len(users)
    banned = sum(1 for u in users if u["is_banned"])
    active_subs = sum(1 for u in users if get_subscription_status(u["user_id"])["active"])
    return {"total": total, "banned": banned, "active_subs": active_subs}


def format_remaining(remaining: timedelta) -> str:
    """تنسيق المدة المتبقية بصيغة عربية مختصرة (أيام وساعات)."""
    total_seconds = int(remaining.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, _ = divmod(rem, 3600)
    if days > 0:
        return f"{days} يوم و {hours} ساعة"
    return f"{hours} ساعة"


def check_access(message: types.Message) -> bool:
    """
    يُستدعى في بداية كل handler يتعامل مع المستخدم العادي.
    يسجل المستخدم، يتحقق من الحظر، ثم يتحقق من وجود اشتراك فعّال.
    الأدمن دائماً مسموح له بدون قيود.
    يرجع True إذا يُسمح بالمتابعة، و False إذا تم رفض الطلب (مع إرسال رسالة توضيحية).
    """
    user = message.from_user
    is_new = register_user(user.id, user.username, user.first_name)
    if is_new and user.id != ADMIN_ID:
        notify_admin_new_user(user.id, user.username, user.first_name)

    if is_admin(user.id):
        return True

    if is_banned(user.id):
        bot.reply_to(
            message,
            "🚫 تم حظرك من استخدام هذا البوت.\n"
            "إذا كنت تعتقد أن هذا خطأ، تواصل مع الإدارة.",
        )
        return False

    status = get_subscription_status(user.id)
    if not status["active"]:
        bot.reply_to(
            message,
            "⛔ <b>لا يوجد لديك اشتراك فعّال في هذا البوت.</b>\n\n"
            "هذا البوت مدفوع، يرجى التواصل مع الإدارة لتفعيل أو تجديد اشتراكك.",
        )
        return False

    return True


# ============================================================
# 5) أدوات مساعدة للصور
# ============================================================

def prepare_image(file_bytes: bytes) -> Image.Image:
    """
    فتح الصورة من البايتس، تحويلها إلى RGB (تفادي مشاكل PNG الشفافة)،
    وتصغيرها بحيث لا تتجاوز أبعادها MAX_IMAGE_SIZE مع الحفاظ على النسبة.
    """
    image = Image.open(BytesIO(file_bytes))
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail(MAX_IMAGE_SIZE, Image.LANCZOS)
    return image


# ============================================================
# 5.1) تحويل ردّ Gemini (Markdown) إلى HTML آمن لتيليجرام
# ============================================================

def gemini_text_to_telegram_html(text: str) -> str:
    """
    Gemini يرد بتنسيق Markdown عادي (**عريض**, ### عناوين, * نقاط).
    تيليجرام بوضع parse_mode='Markdown' القديم صارم جداً وينهار بسهولة
    مع هذا النوع من التنسيق، لذلك نحوّله يدوياً إلى HTML (أكثر استقراراً):
      1. نهرب أحرف HTML الخاصة (&, <, >) أولاً حتى لا تتعارض مع الوسوم
         اللي رح نضيفها بعدين.
      2. **نص** -> <b>نص</b>
      3. ### عنوان -> <b>عنوان</b>
      4. أسطر تبدأ بـ "* " تتحول إلى نقطة "• " عادية (لتفادي تعارضها مع <b>).
    """
    text = html_lib.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^\*\s+", "• ", text, flags=re.MULTILINE)
    return text


# ============================================================
# 6) المنطق الأساسي: التحدث مع Gemini
# ============================================================

def ask_gemini(user_id: int, text_content: str, image: Image.Image | None = None):
    """
    يبني الجلسة مع تاريخ المحادثة الكامل، يرسل الرسالة الجديدة (نص و/أو صورة)،
    يحفظ التبادل في قاعدة البيانات، ويعيد نص الرد.
    يرفع الاستثناءات للأعلى ليتم التعامل معها في الـ handlers.
    """
    history = get_history(user_id)
    chat = client.chats.create(
        model=MODEL_NAME,
        config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION),
        history=history,
    )

    # بناء محتوى الرسالة: نص فقط أو نص + صورة
    if image is not None:
        message_parts = [image, text_content] if text_content else [image]
    else:
        message_parts = [text_content]

    response = chat.send_message(message_parts)
    reply_text = response.text

    # حفظ رسالة المستخدم (نص فقط، لأن الصور لا تُخزَّن كنص في القاعدة)
    user_log = text_content if text_content else "[صورة وجبة]"
    save_message(user_id, "user", user_log)
    save_message(user_id, "model", reply_text)

    return reply_text


# ============================================================
# 7) أوامر البوت
# ============================================================

@bot.message_handler(commands=["start", "help"])
def handle_start(message: types.Message):
    user = message.from_user
    is_new = register_user(user.id, user.username, user.first_name)
    if is_new and user.id != ADMIN_ID:
        notify_admin_new_user(user.id, user.username, user.first_name)

    welcome = (
        "👋 أهلاً بك في بوت تتبع السعرات الحرارية!\n\n"
        "📸 أرسل لي صورة وجبتك أو صف لي ما أكلته نصياً، "
        "وسأقوم بتحليل السعرات الحرارية وتجميع تقريرك اليومي تلقائياً.\n\n"
        "🌙 عندما تنتهي من يومك، أرسل كلمة <b>وقت النوم</b> أو <b>تصفير السعرات</b> "
        "لإعادة ضبط العداد ليوم جديد."
    )

    if is_admin(user.id):
        welcome += "\n\n👑 أنت الأدمن، أرسل /admin لعرض لوحة التحكم."
    elif is_banned(user.id):
        welcome = "🚫 تم حظرك من استخدام هذا البوت."
    else:
        status = get_subscription_status(user.id)
        if status["active"]:
            welcome += (
                f"\n\n✅ اشتراكك فعّال، يتبقى لك: {format_remaining(status['remaining'])}."
            )
        else:
            welcome += (
                "\n\n⛔ لا يوجد لديك اشتراك فعّال حالياً. "
                "تواصل مع الإدارة لتفعيل اشتراكك."
            )

    bot.reply_to(message, welcome)


# ============================================================
# 7.1) أوامر الإدارة (الأدمن فقط - ID: 1421302016)
# ============================================================

def admin_guard(message: types.Message) -> bool:
    """يتحقق إن المرسل هو الأدمن، وإلا يرسل رسالة رفض ويرجع False."""
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "🚫 هذا الأمر مخصص للإدارة فقط.")
        return False
    return True


def parse_args(message: types.Message) -> list[str]:
    """يرجع أجزاء الأمر بعد اسم الأمر نفسه، مثال: '/ban 123' -> ['123']."""
    parts = message.text.strip().split()
    return parts[1:]


def build_admin_keyboard() -> types.InlineKeyboardMarkup:
    """يبني لوحة الأزرار التفاعلية للأدمن (زر واحد بكل صف، زي لوحة Minecraft by IBR)."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 الإحصائيات", callback_data="panel_stats"),
        types.InlineKeyboardButton("👥 قائمة المستخدمين", callback_data="panel_users"),
        types.InlineKeyboardButton("🚫 حظر مستخدم", callback_data="panel_ban"),
        types.InlineKeyboardButton("✅ فك حظر مستخدم", callback_data="panel_unban"),
        types.InlineKeyboardButton("⏱️ تمديد اشتراك", callback_data="panel_addtime"),
        types.InlineKeyboardButton("🗓️ تحديد اشتراك جديد", callback_data="panel_settime"),
        types.InlineKeyboardButton("🛑 إلغاء اشتراك", callback_data="panel_cancelsub"),
        types.InlineKeyboardButton("🔎 معلومات مستخدم", callback_data="panel_userinfo"),
        types.InlineKeyboardButton("📤 رسالة لمستخدم محدد", callback_data="panel_sendto"),
        types.InlineKeyboardButton("📢 إذاعة للجميع", callback_data="panel_broadcast"),
        types.InlineKeyboardButton("❌ إغلاق اللوحة", callback_data="panel_close"),
    )
    return kb


def build_stats_text() -> str:
    stats = get_bot_stats()
    return (
        "📊 <b>إحصائيات البوت</b>\n\n"
        f"👥 إجمالي المستخدمين: {stats['total']}\n"
        f"✅ اشتراكات فعّالة: {stats['active_subs']}\n"
        f"🚫 محظورون: {stats['banned']}"
    )


def build_users_list_text() -> str:
    users = list_all_users()
    if not users:
        return "لا يوجد مستخدمون مسجّلون بعد."

    lines = ["👥 <b>قائمة المستخدمين:</b>\n"]
    for u in users[:80]:  # حد أقصى لتفادي تجاوز حد طول رسائل تيليجرام
        status = get_subscription_status(u["user_id"])
        if u["is_banned"]:
            tag = "🚫"
        elif status["active"]:
            tag = "✅"
        else:
            tag = "⌛"
        name = html_lib.escape(u["first_name"] or "بدون اسم")
        lines.append(f"{tag} <code>{u['user_id']}</code> — {name}")

    if len(users) > 80:
        lines.append(f"\n... و{len(users) - 80} مستخدم آخر.")

    return "\n".join(lines)


def build_userinfo_text(target_id: int) -> str:
    user = get_user(target_id)
    if not user:
        return "⚠️ هذا المستخدم غير مسجّل لدى البوت."

    status = get_subscription_status(target_id)
    if status["active"]:
        sub_line = f"✅ فعّال — يتبقى {format_remaining(status['remaining'])} (ينتهي {status['end'].strftime('%Y-%m-%d %H:%M')})"
    elif status["end"]:
        sub_line = f"⌛ منتهي منذ {status['end'].strftime('%Y-%m-%d %H:%M')}"
    else:
        sub_line = "❌ لا يوجد اشتراك"

    return (
        f"🆔 <code>{user['user_id']}</code>\n"
        f"👤 الاسم: {html_lib.escape(user['first_name'] or '—')}\n"
        f"🔗 المعرف: @{user['username'] if user['username'] else '—'}\n"
        f"🚫 محظور: {'نعم' if user['is_banned'] else 'لا'}\n"
        f"💳 الاشتراك: {sub_line}\n"
        f"📅 انضم في: {user['joined_at'][:16]}"
    )


@bot.message_handler(commands=["admin"])
def handle_admin_panel(message: types.Message):
    if not admin_guard(message):
        return
    admin_pending.pop(message.from_user.id, None)
    bot.send_message(
        message.chat.id,
        f"👑 <b>أهلاً بك في لوحة تحكم الأدمن!</b>\n\nاختر إجراءً:",
        reply_markup=build_admin_keyboard(),
    )


# ------------------------------------------------------------
# معالج ضغطات أزرار لوحة الأدمن
# ------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data.startswith("panel_"))
def handle_admin_callbacks(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "🚫 هذا الأمر مخصص للإدارة فقط.", show_alert=True)
        return

    action = call.data
    bot.answer_callback_query(call.id)

    if action == "panel_close":
        bot.edit_message_text(
            "تم إغلاق اللوحة. أرسل /admin لفتحها مرة أخرى.",
            call.message.chat.id,
            call.message.message_id,
        )
        admin_pending.pop(ADMIN_ID, None)
        return

    if action == "panel_stats":
        bot.send_message(call.message.chat.id, build_stats_text())
        return

    if action == "panel_users":
        bot.send_message(call.message.chat.id, build_users_list_text())
        return

    if action == "panel_sendto":
        admin_pending[ADMIN_ID] = "panel_sendto_id"
        admin_pending_data[ADMIN_ID] = {}
        bot.send_message(call.message.chat.id, "أرسل الآن <code>user_id</code> المستلم:")
        return

    # الإجراءات التالية تحتاج إدخال نصي من الأدمن (user_id و/أو أيام)
    prompts = {
        "panel_ban": "أرسل الآن <code>user_id</code> المراد حظره:",
        "panel_unban": "أرسل الآن <code>user_id</code> المراد فك حظره:",
        "panel_addtime": "أرسل الآن <code>user_id عدد_الأيام</code> (مثال: <code>123456789 30</code>) لتمديد الاشتراك:",
        "panel_settime": "أرسل الآن <code>user_id عدد_الأيام</code> (مثال: <code>123456789 30</code>) لتحديد اشتراك جديد من الآن:",
        "panel_cancelsub": "أرسل الآن <code>user_id</code> المراد إلغاء اشتراكه:",
        "panel_userinfo": "أرسل الآن <code>user_id</code> لعرض تفاصيله:",
        "panel_broadcast": "📢 أرسل الآن نص الرسالة اللي تبي تذيعها لكل المستخدمين:",
    }
    if action in prompts:
        admin_pending[ADMIN_ID] = action
        bot.send_message(call.message.chat.id, prompts[action])


def handle_admin_pending_input(message: types.Message) -> bool:
    """
    يُستدعى أول شي داخل handle_text قبل أي منطق ثاني.
    إذا كان الأدمن بانتظار إدخال (بعد ضغط زر)، يعالج الإدخال هنا ويرجع True.
    إذا ما فيه إجراء معلّق، يرجع False ليكمل handle_text شغله العادي.
    """
    if message.from_user.id != ADMIN_ID:
        return False

    action = admin_pending.get(ADMIN_ID)
    if not action:
        return False

    admin_pending.pop(ADMIN_ID, None)  # نلغي الانتظار فور استلام الإدخال
    args = message.text.strip().split()

    if action == "panel_ban":
        if not args or not args[0].isdigit():
            bot.reply_to(message, "⚠️ صيغة غير صحيحة. أرسل /admin وحاول مرة أخرى.")
            return True
        target_id = int(args[0])
        ban_user(target_id)
        bot.reply_to(message, f"🚫 تم حظر المستخدم <code>{target_id}</code>.")
        return True

    if action == "panel_unban":
        if not args or not args[0].isdigit():
            bot.reply_to(message, "⚠️ صيغة غير صحيحة. أرسل /admin وحاول مرة أخرى.")
            return True
        target_id = int(args[0])
        unban_user(target_id)
        bot.reply_to(message, f"✅ تم فك الحظر عن المستخدم <code>{target_id}</code>.")
        return True

    if action == "panel_addtime":
        if len(args) != 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
            bot.reply_to(message, "⚠️ صيغة غير صحيحة. أرسل /admin وحاول مرة أخرى.")
            return True
        target_id, days = int(args[0]), int(args[1])
        new_end = add_subscription_days(target_id, days)
        bot.reply_to(
            message,
            f"✅ تم تمديد اشتراك <code>{target_id}</code> بمقدار {days} يوم.\n"
            f"📅 ينتهي الاشتراك الآن في: {new_end.strftime('%Y-%m-%d %H:%M')}",
        )
        return True

    if action == "panel_settime":
        if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
            bot.reply_to(message, "⚠️ صيغة غير صحيحة. أرسل /admin وحاول مرة أخرى.")
            return True
        target_id, days = int(args[0]), int(args[1])
        new_end = set_subscription_days(target_id, days)
        bot.reply_to(
            message,
            f"✅ تم تحديد اشتراك <code>{target_id}</code> لمدة {days} يوم من الآن.\n"
            f"📅 ينتهي في: {new_end.strftime('%Y-%m-%d %H:%M')}",
        )
        return True

    if action == "panel_cancelsub":
        if not args or not args[0].isdigit():
            bot.reply_to(message, "⚠️ صيغة غير صحيحة. أرسل /admin وحاول مرة أخرى.")
            return True
        target_id = int(args[0])
        cancel_subscription(target_id)
        bot.reply_to(message, f"🛑 تم إلغاء اشتراك المستخدم <code>{target_id}</code>.")
        return True

    if action == "panel_userinfo":
        if not args or not args[0].isdigit():
            bot.reply_to(message, "⚠️ صيغة غير صحيحة. أرسل /admin وحاول مرة أخرى.")
            return True
        target_id = int(args[0])
        bot.reply_to(message, build_userinfo_text(target_id))
        return True

    if action == "panel_sendto_id":
        if not args or not args[0].isdigit():
            bot.reply_to(message, "⚠️ صيغة غير صحيحة. أرسل /admin وحاول مرة أخرى.")
            admin_pending_data.pop(ADMIN_ID, None)
            return True
        target_id = int(args[0])
        admin_pending_data[ADMIN_ID] = {"target_id": target_id}
        admin_pending[ADMIN_ID] = "panel_sendto_msg"
        bot.reply_to(
            message,
            f"✏️ تمام، الآن أرسل نص الرسالة اللي تبي ترسلها للمستخدم <code>{target_id}</code>:",
        )
        return True

    if action == "panel_sendto_msg":
        data = admin_pending_data.pop(ADMIN_ID, {})
        target_id = data.get("target_id")
        if not target_id:
            bot.reply_to(message, "⚠️ حدث خطأ، حاول مرة أخرى من /admin.")
            return True
        try:
            bot.send_message(target_id, message.text)
            bot.reply_to(message, f"✅ تم إرسال الرسالة بنجاح للمستخدم <code>{target_id}</code>.")
        except Exception as e:
            logger.warning("فشل إرسال رسالة خاصة للمستخدم %s: %s", target_id, e)
            bot.reply_to(
                message,
                f"⚠️ فشل إرسال الرسالة للمستخدم <code>{target_id}</code> "
                "(على الأغلب حظر البوت أو لم يبدأ محادثة معه من قبل).",
            )
        return True

    if action == "panel_broadcast":
        broadcast_text = message.text  # نص الإذاعة كما هو (بدون تعديل)
        bot.reply_to(message, "📢 جاري إرسال الإذاعة لجميع المستخدمين...")

        users = list_all_users()
        sent, failed = 0, 0
        for u in users:
            uid = u["user_id"]
            if uid == ADMIN_ID:
                continue
            try:
                bot.send_message(uid, broadcast_text)
                sent += 1
            except Exception as e:
                failed += 1
                logger.warning("فشل إرسال الإذاعة للمستخدم %s: %s", uid, e)

        bot.send_message(
            message.chat.id,
            f"✅ تم إرسال الإذاعة بنجاح إلى {sent} مستخدم.\n"
            f"⚠️ فشل الإرسال لـ {failed} مستخدم (على الأغلب حظروا البوت).",
        )
        return True

    return False


# ============================================================
# 7.2) أوامر إدارة نصية بديلة (تشتغل لو فضّلت الأوامر بدل الأزرار)
# ============================================================

@bot.message_handler(commands=["ban"])
def handle_ban(message: types.Message):
    if not admin_guard(message):
        return
    args = parse_args(message)
    if not args or not args[0].isdigit():
        bot.reply_to(message, "الاستخدام: <code>/ban &lt;user_id&gt;</code>")
        return
    target_id = int(args[0])
    ban_user(target_id)
    bot.reply_to(message, f"🚫 تم حظر المستخدم <code>{target_id}</code>.")


@bot.message_handler(commands=["unban"])
def handle_unban(message: types.Message):
    if not admin_guard(message):
        return
    args = parse_args(message)
    if not args or not args[0].isdigit():
        bot.reply_to(message, "الاستخدام: <code>/unban &lt;user_id&gt;</code>")
        return
    target_id = int(args[0])
    unban_user(target_id)
    bot.reply_to(message, f"✅ تم فك الحظر عن المستخدم <code>{target_id}</code>.")


@bot.message_handler(commands=["addtime"])
def handle_addtime(message: types.Message):
    if not admin_guard(message):
        return
    args = parse_args(message)
    if len(args) != 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        bot.reply_to(message, "الاستخدام: <code>/addtime &lt;user_id&gt; &lt;عدد_الأيام&gt;</code>")
        return
    target_id, days = int(args[0]), int(args[1])
    new_end = add_subscription_days(target_id, days)
    bot.reply_to(
        message,
        f"✅ تم تمديد اشتراك <code>{target_id}</code> بمقدار {days} يوم.\n"
        f"📅 ينتهي الاشتراك الآن في: {new_end.strftime('%Y-%m-%d %H:%M')}",
    )


@bot.message_handler(commands=["settime"])
def handle_settime(message: types.Message):
    if not admin_guard(message):
        return
    args = parse_args(message)
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        bot.reply_to(message, "الاستخدام: <code>/settime &lt;user_id&gt; &lt;عدد_الأيام&gt;</code>")
        return
    target_id, days = int(args[0]), int(args[1])
    new_end = set_subscription_days(target_id, days)
    bot.reply_to(
        message,
        f"✅ تم تحديد اشتراك <code>{target_id}</code> لمدة {days} يوم من الآن.\n"
        f"📅 ينتهي في: {new_end.strftime('%Y-%m-%d %H:%M')}",
    )


@bot.message_handler(commands=["cancelsub"])
def handle_cancelsub(message: types.Message):
    if not admin_guard(message):
        return
    args = parse_args(message)
    if not args or not args[0].isdigit():
        bot.reply_to(message, "الاستخدام: <code>/cancelsub &lt;user_id&gt;</code>")
        return
    target_id = int(args[0])
    cancel_subscription(target_id)
    bot.reply_to(message, f"🛑 تم إلغاء اشتراك المستخدم <code>{target_id}</code>.")


@bot.message_handler(commands=["userinfo"])
def handle_userinfo(message: types.Message):
    if not admin_guard(message):
        return
    args = parse_args(message)
    if not args or not args[0].isdigit():
        bot.reply_to(message, "الاستخدام: <code>/userinfo &lt;user_id&gt;</code>")
        return
    target_id = int(args[0])
    bot.reply_to(message, build_userinfo_text(target_id))


@bot.message_handler(commands=["users"])
def handle_list_users(message: types.Message):
    if not admin_guard(message):
        return
    bot.reply_to(message, build_users_list_text())


@bot.message_handler(commands=["stats"])
def handle_stats(message: types.Message):
    if not admin_guard(message):
        return
    bot.reply_to(message, build_stats_text())


@bot.message_handler(commands=["sendto"])
def handle_sendto_command(message: types.Message):
    if not admin_guard(message):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.reply_to(message, "الاستخدام: <code>/sendto user_id نص_الرسالة</code>")
        return
    target_id = int(parts[1])
    text = parts[2]
    try:
        bot.send_message(target_id, text)
        bot.reply_to(message, f"✅ تم إرسال الرسالة بنجاح للمستخدم <code>{target_id}</code>.")
    except Exception as e:
        logger.warning("فشل إرسال رسالة خاصة للمستخدم %s: %s", target_id, e)
        bot.reply_to(
            message,
            f"⚠️ فشل إرسال الرسالة للمستخدم <code>{target_id}</code> "
            "(على الأغلب حظر البوت أو لم يبدأ محادثة معه من قبل).",
        )


@bot.message_handler(commands=["broadcast"])
def handle_broadcast_command(message: types.Message):
    if not admin_guard(message):
        return
    admin_pending[ADMIN_ID] = "panel_broadcast"
    bot.reply_to(message, "📢 أرسل الآن نص الرسالة اللي تبي تذيعها لكل المستخدمين:")


# ============================================================
# 8) استقبال الرسائل النصية
# ============================================================

@bot.message_handler(content_types=["text"])
def handle_text(message: types.Message):
    # إذا كان الأدمن بانتظار إدخال بعد ضغط زر من لوحة التحكم، عالجه هنا وتوقف
    if handle_admin_pending_input(message):
        return

    if not check_access(message):
        return

    user_id = message.from_user.id
    bot.send_chat_action(message.chat.id, "typing")

    try:
        reply = ask_gemini(user_id, message.text)
        bot.reply_to(message, gemini_text_to_telegram_html(reply))

    except APIError as e:
        if e.code == 429:
            logger.warning("تجاوز حدّ الحصة (429) لمستخدم %s", user_id)
            bot.reply_to(
                message,
                "⚠️ عذراً، تم استهلاك الحصة المجانية المتاحة حالياً من الخدمة. "
                "الرجاء المحاولة مرة أخرى بعد قليل.",
            )
        else:
            logger.error("خطأ من Gemini API (code=%s): %s", e.code, e)
            bot.reply_to(message, "⚠️ حدث خطأ أثناء الاتصال بخدمة الذكاء الاصطناعي. حاول مجدداً.")
    except Exception as e:
        logger.exception("خطأ غير متوقع: %s", e)
        bot.reply_to(message, "⚠️ حدث خطأ غير متوقع، الرجاء المحاولة مرة أخرى.")


# ============================================================
# 9) استقبال الصور
# ============================================================

@bot.message_handler(content_types=["photo"])
def handle_photo(message: types.Message):
    if not check_access(message):
        return

    user_id = message.from_user.id
    bot.send_chat_action(message.chat.id, "typing")

    try:
        # أخذ أعلى دقة متاحة للصورة المرسلة
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        file_bytes = bot.download_file(file_info.file_path)

        image = prepare_image(file_bytes)
        caption = message.caption or ""

        reply = ask_gemini(user_id, caption, image=image)
        bot.reply_to(message, gemini_text_to_telegram_html(reply))

    except APIError as e:
        if e.code == 429:
            logger.warning("تجاوز حدّ الحصة (429) لمستخدم %s", user_id)
            bot.reply_to(
                message,
                "⚠️ عذراً، تم استهلاك الحصة المجانية المتاحة حالياً من الخدمة. "
                "الرجاء المحاولة مرة أخرى بعد قليل.",
            )
        else:
            logger.error("خطأ من Gemini API (code=%s): %s", e.code, e)
            bot.reply_to(message, "⚠️ حدث خطأ أثناء الاتصال بخدمة الذكاء الاصطناعي. حاول مجدداً.")
    except Exception as e:
        logger.exception("خطأ غير متوقع: %s", e)
        bot.reply_to(message, "⚠️ حدث خطأ أثناء معالجة الصورة. حاول مرة أخرى.")


# ============================================================
# 10) نقطة التشغيل
# ============================================================

if __name__ == "__main__":
    init_db()
    logger.info("تم تهيئة قاعدة البيانات بنجاح.")
    logger.info("البوت يعمل الآن... اضغط Ctrl+C للإيقاف.")
    bot.infinity_polling(skip_pending=True)