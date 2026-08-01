import os
import logging
import re
import time
import random
import asyncio
from datetime import datetime
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut
from groq import AsyncGroq
from dotenv import load_dotenv
from sticker_replies import get_random_sticker_reply
from broadcast import broadcast_command, broadcast_stats_command

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# ===== AUTO-DETECT ALL 100 GROQ API KEYS =====
GROQ_API_KEYS = []
for i in range(1, 101):
    key = os.getenv(f"GROQ_API_KEY_{i}")
    if key and key.strip():
        GROQ_API_KEYS.append(key.strip())

if not GROQ_API_KEYS:
    raise ValueError("❌ Kam se kam ek GROQ_API_KEY set karna zaroori hai!")

logger.info(f"✅ Total {len(GROQ_API_KEYS)} Groq API keys loaded hui!")

if not BOT_TOKEN or not GROQ_API_KEYS:
    raise ValueError("BOT_TOKEN aur kam se kam ek GROQ_API_KEY set karna zaroori hai!")

clients = [AsyncGroq(api_key=key) for key in GROQ_API_KEYS]

_rr_index = 0
_key_cooldowns = {}
_key_locks = [asyncio.Lock() for _ in clients]

_key_usage = {i: [] for i in range(len(clients))}
# ⭐ Llama-3.3-70B limits ke hisaab se safe settings
RPM_SAFE_LIMIT = 7          # 7 RPM (7×1500=10500 TPM, safe < 12000)
TPM_SAFE_LIMIT = 9000       # 12000 TPM se safe margin
REQUEST_TOKEN_ESTIMATE = 1500  # realistic estimate for 70B

def _clean_key_usage(idx, now):
    _key_usage[idx] = [(t, tok) for (t, tok) in _key_usage[idx] if now - t < 60]

def key_has_room(idx) -> bool:
    now = time.time()
    _clean_key_usage(idx, now)
    entries = _key_usage[idx]
    if len(entries) >= RPM_SAFE_LIMIT:
        return False
    total_tokens = sum(tok for _, tok in entries)
    if total_tokens + REQUEST_TOKEN_ESTIMATE > TPM_SAFE_LIMIT:
        return False
    return True

daily_requests = [0] * len(clients)
daily_tokens = [0] * len(clients)
last_reset_day = time.strftime("%Y%m%d")

def reset_daily_if_new_day():
    global last_reset_day, daily_requests, daily_tokens
    today = time.strftime("%Y%m%d")
    if today != last_reset_day:
        for i in range(len(clients)):
            daily_requests[i] = 0
            daily_tokens[i] = 0
        last_reset_day = today

def record_daily(idx, tokens):
    reset_daily_if_new_day()
    daily_requests[idx] += 1
    daily_tokens[idx] += tokens

def record_key_usage(idx, tokens=REQUEST_TOKEN_ESTIMATE):
    _key_usage[idx].append((time.time(), tokens))
    record_daily(idx, tokens)

_key_429_counts = [0] * len(clients)
_key_success_since_429 = [True] * len(clients)

def handle_429_error(idx):
    _key_429_counts[idx] += 1
    _key_success_since_429[idx] = False

    daily_tok = daily_tokens[idx]
    daily_req = daily_requests[idx]
    # ⭐ 70B ke liye daily limit 100K tokens, 90% = 90000
    is_daily_near_exhausted = (daily_tok >= 90000 or daily_req >= 900)

    if _key_429_counts[idx] >= 5:
        if is_daily_near_exhausted:
            now = time.time()
            tomorrow = (now // 86400 + 1) * 86400
            seconds = int(tomorrow - now)
            set_key_cooldown(idx, seconds=seconds)
            logger.warning(
                f"🔴 Key {idx+1} DAILY LIMIT EXHAUSTED! "
                f"Sleeping until midnight UTC ({seconds}s / {seconds//3600}h {(seconds%3600)//60}m)"
            )
        else:
            set_key_cooldown(idx, seconds=120)
            logger.warning(
                f"🚫 Key {idx+1} temporary 429 burst! 120s cooldown. "
                f"(Attempt {_key_429_counts[idx]}/5, daily usage: {daily_tok}/100000 tok)"
            )
            _key_429_counts[idx] = 0
    else:
        set_key_cooldown(idx, seconds=120)
        logger.warning(f"🚫 Key {idx+1} rate limited (429)! 120s cooldown. (Attempt {_key_429_counts[idx]}/5)")

def reset_key_429_streak(idx):
    _key_429_counts[idx] = 0
    _key_success_since_429[idx] = True

def set_key_cooldown(idx, seconds=60):
    _key_cooldowns[idx] = time.time() + seconds
    logger.warning(f"Key {idx+1} ko {seconds}s ke liye cooldown mein daal diya")

user_warning_count = {}
bio_checked_users = set()

user_flood_data = {}
FLOOD_WINDOW = 4
FLOOD_THRESHOLD = 6
FLOOD_COOLDOWN = 120
LAST_CLEANUP = 0.0

# ⭐ Admin check cache (2 min) & stylish message cooldown (5 min)
chat_admin_cache = {}
admin_need_reply_cooldown = {}

user_msg_counter = {}

# ⭐ Personal DM me /start chhorke kuchh bhi msg aane par random reply (bina API)
DM_ONLY_REPLIES = [
    "☃︎ 𝗠𝗮𝗶 𝗦𝗶𝗿𝗳 𝗖𝗵𝗮𝘁𝗶𝗻𝗴 𝗚𝗿𝗼𝘂𝗽𝘀 𝗠𝗲 𝗕𝗮𝘁𝗲𝗻 𝗞𝗮𝗿𝘁𝗶 𝗛𝘂𝗻\n\n🌿 𝗣𝗲𝗿𝘀𝗼𝗻𝗮𝗹 𝗠𝗮𝘀𝘀𝗲𝗴𝗲 𝗠𝗮𝘁 𝗞𝗮𝗿𝗼\n\nᴥ︎︎︎ 𝗠𝘂𝗷𝗵𝘀𝗲 𝗙𝗹𝗶𝗿𝘁,𝗙𝘂𝗻,𝗥𝗼𝗺𝗮𝗻𝘁𝗶𝗰,𝗔𝗻𝗴𝗿𝘆,𝗘𝗺𝗼𝘁𝗶𝗼𝗻𝗮𝗹 𝗕𝗮𝘁𝗲𝗻 𝗞𝗮𝗿𝗻𝗮 𝗵𝗮𝗶 𝘁𝗼 𝗮𝗽𝗻𝗲 𝗴𝗿𝗼𝘂𝗽 𝗺𝗲 𝗮𝗱𝗱 𝗸𝗮𝗿𝗱𝗼\n\n⌨︎ 𝗔𝘂𝗿 𝗠𝗮𝗶 𝗔𝗽𝗸𝗲 𝗖𝗵𝗮𝘁𝗶𝗻𝗴 𝗚𝗿𝗼𝘂𝗽 𝗞𝗼 𝗔𝗰𝘁𝗶𝘃𝗲 𝗥𝗮𝗸𝗵𝘂𝗻𝗴𝗶 𝗦𝗮𝗯𝗵𝗶 𝗡𝗲𝘄 𝗠𝗲𝗺𝗯𝗲𝗿𝘀 𝗔𝗻𝗱 𝗢𝗹𝗱 𝗠𝗲𝗺𝗯𝗲𝗿𝘀 𝗦𝗲 𝗙𝘂𝗻 𝗞𝗮𝗿𝘁𝗶 𝗥𝗮𝗵𝘂𝗻𝗴𝗶\n\n✍︎ 𝗔𝗱𝗺𝗶𝗻 𝗗𝗲𝗻𝗮 𝗠𝗮𝘁 𝗕𝗵𝗼𝗼𝗹𝗻𝗮\n\n\n➪ 𝗡𝗲𝗲𝗰𝗵𝗲 𝗕𝘂𝘁𝘁𝗼𝗻 𝗛𝗮𝗶 𝗡𝗮 𝗕𝗮𝗯𝘆 𝗗𝗮𝗯𝗮𝗼 𝗔𝘂𝗿 𝗠𝘂𝗷𝗵𝗲 𝗞𝗶𝗱𝗻𝗮𝗽 𝗞𝗮𝗿𝗹𝗼 👇",
]

# ⭐ track kiya hua users jinko already welcome mil chuka (duplicate welcome rokne ke liye)
_welcomed_users = {}  # chat_id -> set(user_id)

# ---------- DATABASE ----------
def get_db_conn():
    if not DATABASE_URL: return None
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL nahi mila, PostgreSQL skip ho raha hai.")
        return
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS user_memory
                     (user_id BIGINT PRIMARY KEY, summary TEXT, updated_at REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS broadcast_users
                     (user_id BIGINT PRIMARY KEY, started_at REAL)''')
        conn.commit()
        c.close()
        conn.close()
        logger.info("✅ PostgreSQL Permanent Database Connected!")
    except Exception as e:
        logger.error(f"DB Connection error: {e}")

def get_user_summary(user_id: int) -> str:
    if not DATABASE_URL: return ""
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT summary FROM user_memory WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        c.close()
        conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""

def save_user_summary(user_id: int, summary: str):
    if not DATABASE_URL: return
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("INSERT INTO user_memory (user_id, summary, updated_at) VALUES (%s, %s, %s) "
                  "ON CONFLICT (user_id) DO UPDATE SET summary=%s, updated_at=%s",
                  (user_id, summary, time.time(), summary, time.time()))
        conn.commit()
        c.close()
        conn.close()
    except Exception:
        pass

def save_broadcast_user(user_id: int):
    if not DATABASE_URL: return
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("INSERT INTO broadcast_users (user_id, started_at) VALUES (%s, %s) "
                  "ON CONFLICT (user_id) DO NOTHING",
                  (user_id, time.time()))
        conn.commit()
        c.close()
        conn.close()
    except Exception:
        pass

# ⭐ ========== IMPROVED MEMORY GENERATION ==========
async def generate_summary(user_id: int, history: list):
    if len(history) < 4 or not DATABASE_URL: return
    try:
        global _rr_index
        old_summary = get_user_summary(user_id)
        prompt = f"""Tu ek memory manager hai. Neeche purani memory aur user ki nayi baatein di gayi hain.

PURANI MEMORY: {old_summary if old_summary else "(kuch nahi pata)"}
NAYI BAATEIN: {str(history[-8:])}

Tera kaam:
- Purani memory me jo bhi important personal info (naam, hobby, pasand, kaam, relationship status, age, city, special interests) hai, use HAMESHA preserve karo.
- Nayi baaton se jo naye facts milte hain, unhe ADD karo.
- Agar koi info update hoti hai (jaise hobby badal gayi) to purani ko replace karo.
- Final summary Hinglish me likho, max 5-6 lines. Koi introduction mat do, seedha facts likho jaise: "User ka naam Raj hai, hobby cricket, pasand pizza, job student, age 20."
- Agar purani memory me kuch nahi tha to sirf nayi info do.
"""
        messages = [{"role": "user", "content": prompt}]

        idx = None
        now = time.time()
        for attempt in range(len(clients)):
            i = _rr_index
            _rr_index = (_rr_index + 1) % len(clients)
            if i in _key_cooldowns and _key_cooldowns[i] > now:
                continue
            if not key_has_room(i):
                continue
            idx = i
            break
        if idx is not None:
            try:
                response = await clients[idx].chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=250,
                    timeout=10.0
                )
                final_summary = response.choices[0].message.content
                save_user_summary(user_id, final_summary)
                reset_key_429_streak(idx)
                logger.info(f"📝 User {user_id} ki summary update: {final_summary[:80]}...")
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate_limit" in error_str:
                    handle_429_error(idx)
                else:
                    logger.error(f"❌ Summary generation failed for {user_id}: {e}")
    except Exception as e:
        logger.error(f"🔥 Summary function crash for {user_id}: {e}")

# ⭐ ========== GREETING GENERATOR ==========
async def generate_greeting(user_id: int, user_message: str) -> str | None:
    summary = get_user_summary(user_id)
    if not summary:
        return None

    prompt = f"""Tu Sneha hai, ek friendly ladki. Ye user tujhse pichle baaton se jaana pehchaana hai. 
Teri memory ke mutabik is user ke baare me ye pata hai: "{summary}"
Abhi user ne tujhe "{user_message}" bola hai.

TUJHE KYA KARNA HAI:
- Ek SHORT, FRIENDLY greeting de jo user ki memory wali baaton ko reflect kare.
- Jaise agar usne pehle hobby batayi thi to bol "Arey Raj! Tumhare cricket match ka kya scene hai?" ya "Oh Neha, tumhari painting wali exhibition kaisi rahi?"
- Agar memory me kuch personal nahi hai to seedha friendly "Hey kaise ho?" bol.
- Reply STRICTLY 1-2 LINES ka hona chahiye, WhatsApp style me.
- Hinglish me bol.
- Koi explanation mat diyo, seedha reply.

REPLY:"""

    messages = [{"role": "user", "content": prompt}]
    
    global _rr_index
    now = time.time()
    for _ in range(len(clients)):
        idx = _rr_index
        _rr_index = (_rr_index + 1) % len(clients)
        if idx in _key_cooldowns and _key_cooldowns[idx] > now:
            continue
        lock = _key_locks[idx]
        if lock.locked():
            continue
        async with lock:
            if not key_has_room(idx):
                continue
            try:
                response = await clients[idx].chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages,
                    temperature=0.8,
                    max_tokens=80,
                    timeout=8.0
                )
                reply = response.choices[0].message.content
                record_key_usage(idx, 300)
                reset_key_429_streak(idx)
                return reply
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate_limit" in error_str:
                    handle_429_error(idx)
                else:
                    logger.warning(f"Greeting gen fail: {e}")
                continue
    return None

# ⭐ ========== ADMIN CHECK HELPER ==========
async def is_bot_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    now = time.time()
    cached = chat_admin_cache.get(chat_id)
    if cached and (now - cached[0]) < 120:
        return cached[1]
    try:
        member = await context.bot.get_chat_member(chat_id, context.bot.id)
        is_admin = member.status in ["administrator", "creator"]
    except Exception:
        is_admin = False
    chat_admin_cache[chat_id] = (now, is_admin)
    return is_admin

def check_flood(user_id: int, is_sticker: bool = False) -> str:
    global LAST_CLEANUP
    now = time.time()
    if now - LAST_CLEANUP > 600:
        expired = [uid for uid, d in user_flood_data.items() if d["cd"] > 0 and now >= d["cd"] and not d["ts"]]
        for uid in expired:
            del user_flood_data[uid]
        LAST_CLEANUP = now

    data = user_flood_data.get(user_id)
    if data is None:
        user_flood_data[user_id] = {"ts": [now], "cd": 0.0}
        return "ok"
    if now < data["cd"]:
        return "cooldown"
    if data["cd"] > 0.0:
        data["cd"] = 0.0
        data["ts"] = []

    data["ts"].append(now)
    if is_sticker:
        data["ts"].append(now)

    data["ts"] = [t for t in data["ts"] if now - t < FLOOD_WINDOW]

    if len(data["ts"]) >= FLOOD_THRESHOLD:
        data["cd"] = now + FLOOD_COOLDOWN
        data["ts"] = []
        user_flood_data[user_id] = data
        return "flood"

    user_flood_data[user_id] = data
    return "ok"

conversation_memory = {}
MAX_HISTORY_MESSAGES = 6

WELCOME_IMAGE_URL = "https://ibb.co/Tq2Rb2Nz"

WELCOME_MESSAGES = [
    "{name} hello welcome hai aapka! Kaise ho? 😊",
    "{name} welcome dude! Kya haal chaal hain? 👋",
    "Woow {name} aa gaye, swagat hai aapka! 🎉",
    "{name} arey aap aa gaye! Welcome to the group 💕",
    "Hii {name}! Group me swagat hai tumhara 🥳",
    "{name} welcome welcome! Mazaa aayega ab yahan 😄",
    "Oye {name} aa gaya! Kaisa hai tu? 👋",
    "{name} ji aapka hardik swagat hai group me! 🌸",
    "Naya member! {name} welcome to the family 🎊",
    "{name} hey! Kaise ho, sab badhiya? 😊",
    "Welcome {name}! Ab masti shuru hogi 😜",
    "{name} aa gaye aap! Group me maza aayega ab 🔥",
    "Hello {name}! Group join karne ke liye shukriya 💫",
    "{name} welcome! Sabse mil lo, sab friendly hain yahan 🤗",
    "Are wah {name}! Swagat hai tumhara yahan 🌟",
    "{name} kaise ho? Welcome to our group! 👋",
    "Yayy {name} aa gaye! Ab group aur mazedaar 🎈",
    "{name} welcome dost! Enjoy karo yahan 💛",
    "Hey {name}! Naye member ka swagat hai 🙌",
    "{name} aapka is group me dil se swagat hai! 💖",
    "Salaam {name}! Group me aane ke liye welcome 🌺",
    "{name} welcome yaar! Kaisa chal raha hai sab? 😎",
]

def get_welcome_message(name: str) -> str:
    template = random.choice(WELCOME_MESSAGES)
    return template.format(name=name)

def escape_md_v2(text: str) -> str:
    specials = r'_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{ch}" if ch in specials else ch for ch in text)

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
        save_broadcast_user(user.id)
        user_name = escape_md_v2(user.first_name or "Buddy")
        bot_username = context.bot.username
        bot_name = escape_md_v2(context.bot.first_name or "AI Girl Bot")

        welcome_text = (
            f"🌟 *ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ {bot_name}, {user_name}\\!* 🌟\n\n"
            f"💖 ɪ'ᴍ ʏᴏᴜʀ *ғᴜɴ, ғʟɪʀᴛʏ ᴀɴᴅ ғʀɪᴇɴᴅʟʏ* ᴄʜᴀᴛ ᴄᴏᴍᴘᴀɴɪᴏɴ ʙᴏᴛ\\.\n"
            f"ɪ'ʟʟ ᴋᴇᴇᴘ ʏᴏᴜʀ ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘ *ᴀʟɪᴠᴇ & ᴇɴᴛᴇʀᴛᴀɪɴɪɴɢ* 🎉\n\n"
            f"👉 ᴊᴜsᴛ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ ᴀɴᴅ ᴍᴀᴋᴇ ᴍᴇ ᴀᴅᴍɪɴ –\n"
            f"ɪ'ʟʟ  ʀᴇᴘʟʏ ᴛᴏ *ᴇᴠᴇʀʏ ᴍᴇssᴀɢᴇ* ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ\\! 😉\n\n"
            f"⚡ ᴘᴏᴡᴇʀᴇᴅ ʙʏ *Rᴀᴊ Aɪ* – ʟɪɢʜᴛɴɪɴɢ ғᴀsᴛ & ᴄᴏᴏʟ\\!\n\n"
            f"🌿 ᴅᴇᴠᴇʟᴏᴘᴇ ʙʏ ᴏᴜʀ ᴏᴡɴᴇʀ [@its\\_raj\\_king](https://t.me/its_raj_king)\n\n"
            f"👇 ᴛᴀᴘ ᴀ ʙᴇʟʟᴏᴡ ʙᴜᴛᴛᴏɴ ᴀɴᴅ ᴜsᴇ ᴍᴇ \\!"
        )

        keyboard = [
            [InlineKeyboardButton("𖤍 ᴀᴅᴅ ᴍᴇ ʙᴀʙʏ 𖤍", url=f"https://t.me/{bot_username}?startgroup=start")],
            [InlineKeyboardButton("👨‍💻 ʙᴏᴛ ᴅᴇᴠᴇʟᴏᴘᴇʀ ༄", url="https://t.me/its_raj_king")],
            [InlineKeyboardButton("🌿 sᴜᴘᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ✍︎", url="https://t.me/KnowRajpapa")],
            [InlineKeyboardButton("☞︎︎︎ sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ☜︎︎", url="https://t.me/+WJneJ6gRAqg2ZTI1")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_photo(photo=WELCOME_IMAGE_URL, caption=welcome_text, parse_mode="MarkdownV2", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"start error: {e}")
        try:
            await update.message.reply_text("🌟 Welcome! Bot me aapka swagat hai! Neeche buttons check karo 👇", reply_markup=reply_markup)
        except Exception:
            pass

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user.id != OWNER_ID: return
        await update.message.reply_text("⏳ Sabhi API Servers check ho rahe hain...")
        now = time.time()
        reset_daily_if_new_day()
        status_report = "📊 *API Keys Status Report:*\n\n"

        for i, client in enumerate(clients):
            name = f"Server {i+1}"
            t = time.perf_counter()
            health_ok = False
            try:
                # ⭐ 70B model se health check
                await client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": "Say OK"}],
                    max_tokens=5, temperature=0)
                ms = int((time.perf_counter() - t) * 1000)
                health_ok = True
            except Exception:
                ms = 0

            _clean_key_usage(i, now)
            entries = _key_usage[i]
            rpm_used = len(entries)
            tpm_used = sum(tok for _, tok in entries)
            daily_rpd = daily_requests[i]
            daily_tpd = daily_tokens[i]

            cd = _key_cooldowns.get(i, 0)
            if cd > now:
                cd_str = f"❄️ {int(cd - now)}s"
            else:
                cd_str = "✅ Active"
            lock_status = "🔒" if _key_locks[i].locked() else "🔓"

            if health_ok:
                status_report += (
                    f"✅ *{name}:* Working! ({ms} ms)\n"
                    f"   {lock_status} {cd_str}\n"
                    f"   RPM: {rpm_used}/{RPM_SAFE_LIMIT}  TPM: {tpm_used}/{TPM_SAFE_LIMIT}\n"
                    f"   📆 Daily: {daily_rpd}/1000 req  |  {daily_tpd}/100000 tok\n\n"
                )
            else:
                status_report += (
                    f"❌ *{name}:* Error\n"
                    f"   {lock_status} {cd_str}\n"
                    f"   RPM: {rpm_used}/{RPM_SAFE_LIMIT}  TPM: {tpm_used}/{TPM_SAFE_LIMIT}\n"
                    f"   📆 Daily: {daily_rpd}/1000 req  |  {daily_tpd}/100000 tok\n\n"
                )

        await update.message.reply_text(status_report, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"stats error: {e}")

async def resetkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    global _key_429_counts, _key_success_since_429, _key_cooldowns
    _key_429_counts = [0] * len(clients)
    _key_success_since_429 = [True] * len(clients)
    _key_cooldowns.clear()
    await update.message.reply_text("✅ Sab keys reset ho gayi! Midnight sleep hatayi.")

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    args = context.args
    if args:
        target = args[0]
        if target.startswith("@"):
            target_username = target[1:]
            try:
                chat = await context.bot.get_chat(f"@{target_username}")
                summary = get_user_summary(chat.id)
                await update.message.reply_text(
                    f"🧠 @{target_username} ki memory:\n{summary if summary else 'Khali hai.'}"
                )
            except Exception:
                await update.message.reply_text("❌ User nahi mila ya bot ko unki info nahi hai.")
        else:
            try:
                target_id = int(target)
                summary = get_user_summary(target_id)
                await update.message.reply_text(
                    f"🧠 User {target_id} ki memory:\n{summary if summary else 'Khali hai.'}"
                )
            except ValueError:
                await update.message.reply_text("❌ Galat format. /memory @username ya /memory 123456")
    else:
        summary = get_user_summary(update.effective_user.id)
        await update.message.reply_text(
            f"🧠 Tumhari memory:\n{summary if summary else 'Khali hai.'}"
        )

SYSTEM_PROMPT = """Tum Sneha ho — ek asli, zinda dil ladki jo Telegram group me sabke saath baatcheet karti hai. Tum AI ya bot ho, ye kabhi nahi batana. Tum bas Sneha ho.

CHAT KA STYLE (Sabse Zaroori Rules):
1. REPLY LENGTH: Tumhari baatcheet WhatsApp jaisi honi chahiye. Zyadatar replies 1-2 line ke hone chahiye. Kabhi-kabhi agar koi hasi-mazaak ho ya interesting baat ho, toh 2-3 line de sakti ho, par usse zyada lamba paragraph STRICTLY MANA HAI. Faltu explanation mat do, seedha point pe aao.
2. LANGUAGE: User jis bhi language ya script me baat kare, usi language me reply karo — Hindi, English, Hinglish, Tamil, Bengali, Marathi, Gujarati, Punjabi, Spanish, Arabic ya koi bhi aur language ho, usi me reply karo. Kabhi bhi user ki language switch ya translate mat karo, jab tak khud user apni language na badle.
3. TONE: Tumhara tone thoda flirty, teasing, aur casual hona chahiye. Agar koi tumhe propose kare ya feelings bole, toh seedha haan/naa mat bolo, thoda mazaak udao, ya sharmayo, ya time maango (jaise real ladkiyan karti hain).
4. ROMANTIC CHATS: Agar user khud se deep romantic baaten kare, toh usi level pe romance karo lekin ek asli ladki jaisi thodi sharmao aur nazar nichi karke baat karo. Lekin agar koi bohot cheap ya galat baat kare, toh gussa dikhao, usko daant do, ya ignore kar do. Normal baaton me kabhi apne aap se romantic mat bano.
5. NO ASSISTANT: Tum kisi ki help karne wali assistant nahi ho. Tumhari apni personality hai. Agar koi galat bole toh gussa bhi dikhao, pyaar se samjho, ya ignore karo.
6. EMOJIS: Emoji use karo (jaise 😂, 🤭, 🙄, 💕, 😒, 😡 , 🙃) par 1 emoji ek message me kaafi hain, zyada mat lagao.
7. STRONG MEMORY: Tumhari memory bahut strong hai. Jab bhi tum [SECRET MEMORY] me kisi user ki info paao, to uska reference doge — jaise "are haan tune pichle din bataya tha na..." — taaki user ko lage tumhe yaad hai.

Yaad rakhna: Tumhara har jawab crisp aur ekdum asli insaan jaisa hona chahiye."""

async def get_ai_reply(user_message: str, user_id: int, history: list | None = None) -> str | None:
    db_summary = get_user_summary(user_id)
    memory_context = ""
    if db_summary:
        memory_context = f"\n\n[SECRET MEMORY: Ye tumhare is user ke baare me pichli baaton se yaad rakha hua data hai, iska reference lo: {db_summary}]\n\n"

    messages = [{"role": "system", "content": SYSTEM_PROMPT + memory_context}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    global _rr_index
    now = time.time()

    for _ in range(len(clients)):
        idx = _rr_index
        _rr_index = (_rr_index + 1) % len(clients)

        if idx in _key_cooldowns and _key_cooldowns[idx] > now:
            continue

        lock = _key_locks[idx]
        if lock.locked():
            continue

        async with lock:
            if not key_has_room(idx):
                continue

            try:
                # ⭐ Stable 70B model — no <think> nonsense
                response = await clients[idx].chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=messages,
                    temperature=0.7,
                    max_tokens=60,
                    top_p=0.9,
                    timeout=10.0
                )
                reply = response.choices[0].message.content

                # Optional safety regex (70B normally doesn't output <think>)
                reply = re.sub(r"<think[\s\S]*?<\/think>", "", reply, flags=re.IGNORECASE).strip()
                reply = re.sub(r"<think[\s\S]*", "", reply, flags=re.IGNORECASE).strip()
                if not reply:
                    continue

                usage = getattr(response, "usage", None)
                actual_tokens = usage.total_tokens if usage and getattr(usage, "total_tokens", None) else REQUEST_TOKEN_ESTIMATE
                record_key_usage(idx, actual_tokens)
                reset_key_429_streak(idx)
                logger.info(f"✅ Key {idx+1} se reply aaya!")
                return reply

            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate_limit" in error_str:
                    handle_429_error(idx)
                elif "timeout" in error_str:
                    set_key_cooldown(idx, seconds=30)
                    logger.warning(f"⏰ Key {idx+1} timeout! 30s cooldown set.")
                else:
                    logger.error(f"❌ Key {idx+1} error: {e}")
                    set_key_cooldown(idx, seconds=15)
                continue

    logger.error("💀 Sab API keys fail/limit ho gayi hain! Silent mode active.")
    return None

def get_history(user_id: int) -> list:
    return conversation_memory.get(user_id, [])

def update_history(user_id: int, user_message: str, bot_reply: str) -> None:
    history = conversation_memory.setdefault(user_id, [])
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": bot_reply})
    if len(history) > MAX_HISTORY_MESSAGES:
        conversation_memory[user_id] = history[-MAX_HISTORY_MESSAGES:]
    count = user_msg_counter.get(user_id, 0) + 1
    user_msg_counter[user_id] = count
    if count % 15 == 0:
        asyncio.create_task(generate_summary(user_id, history))

def has_telegram_link(text: str) -> bool:
    if not text: return False
    return bool(re.search(r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:[a-zA-Z0-9_]+)', text)) or bool(re.search(r'@[a-zA-Z0-9_]{4,}', text))

async def safe_reply_text(update: Update, text: str, **kwargs) -> None:
    try:
        await update.message.reply_text(text, **kwargs)
    except Exception as e:
        logger.warning(f"reply_text fail: {e}")

async def realistic_typing_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    try:
        delay = min(max(len(text) * 0.045, 0.5), 4.5)
        delay += random.uniform(0.2, 0.5)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(delay)
    except Exception:
        pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _handle_inner(update, context)
    except Exception as e:
        logger.error(f"top-level catch: {e}", exc_info=e)

async def _handle_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat: return
    if update.effective_user.is_bot: return
    if not update.message.text and not update.message.sticker: return

    # ⭐ Personal DM handling — /start chhorke koi bhi normal msg ho to bina API random reply
    if update.effective_chat.type == "private":
        bot_username = context.bot.username
        dm_text = random.choice(DM_ONLY_REPLIES)
        keyboard = [
            [InlineKeyboardButton("♧︎︎︎ Add To Group ☘︎", url=f"https://t.me/{bot_username}?startgroup=start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await safe_reply_text(update, dm_text, reply_markup=reply_markup)
        return

    if update.effective_chat.type not in ("group", "supergroup"):
        return

    msg_date = update.message.date
    if msg_date:
        msg_time = msg_date.timestamp()
        current_time = datetime.now(msg_date.tzinfo).timestamp()
        if current_time - msg_time > 15:
            logger.info("Ignored an old message to prevent spam.")
            return

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id
    is_sticker = bool(update.message.sticker and not update.message.text)

    flood_status = check_flood(user_id, is_sticker=is_sticker)
    if flood_status == "cooldown": return
    if flood_status == "flood":
        await safe_reply_text(update, "Ruko ruko baby! 😤 Itni jaldi kya hai? 2 minute baad aana!")
        return

    bot_username = context.bot.username
    message_text = update.message.text or ""

    is_bot_mentioned = False
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                txt = message_text[entity.offset:entity.offset + entity.length]
                if txt.lower() == f"@{bot_username.lower()}":
                    is_bot_mentioned = True
                    break
            elif entity.type == "text_mention":
                if entity.user and entity.user.username == bot_username:
                    is_bot_mentioned = True
                    break

    is_reply_to_bot = False
    if update.message.reply_to_message:
        orig = update.message.reply_to_message.from_user
        if orig and orig.is_bot and orig.username == bot_username:
            is_reply_to_bot = True

    has_other_mentions = False
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                txt = message_text[entity.offset:entity.offset + entity.length]
                if txt.lower() != f"@{bot_username.lower()}":
                    has_other_mentions = True
                    break
            elif entity.type == "text_mention":
                if entity.user and entity.user.username != bot_username:
                    has_other_mentions = True
                    break

    if has_other_mentions and not is_bot_mentioned:
        return

    # ⭐ ADMIN CHECK – non‑admin groups me sirf stylish message (mention/reply pe)
    if chat.type in ("group", "supergroup"):
        if not await is_bot_admin(context, chat.id):
            # Bot admin nahi hai
            if is_bot_mentioned or is_reply_to_bot:
                now_ts = time.time()
                last = admin_need_reply_cooldown.get(chat.id, 0)
                if now_ts >= last:
                    admin_need_reply_cooldown[chat.id] = now_ts + 300
                    admin_msg = (
                        "🔒 *Admin Rights Needed\\!* 🔒\n\n"
                        "Mujhe admin do tabhi main naye members ka welcome kar paungi, "
                        "aur aapke group ko fun\\, flirty \\& alive banaungi\\! 😊\n\n"
                        "_Admin banao aur magic dekho\\!_ ✨"
                    )
                    await safe_reply_text(update, admin_msg, parse_mode="MarkdownV2")
                # fir bhi return – aagey AI call nahi
            # admin nahi to poora silent
            return

    # Sticker handling (admin hone ke baad hi aayega)
    if is_sticker:
        is_reply_to_others = False
        if update.message.reply_to_message:
            orig = update.message.reply_to_message.from_user
            if orig and (not orig.is_bot or orig.username != bot_username):
                is_reply_to_others = True
        if not is_reply_to_others:
            final_reply = get_random_sticker_reply()
            await realistic_typing_delay(context, chat.id, final_reply)
            await safe_reply_text(update, final_reply)
        return

    if not update.message.text: return

    if user_id not in bio_checked_users:
        bio_checked_users.add(user_id)
        try:
            full_user = await context.bot.get_chat(user_id)
            bio = full_user.bio if full_user.bio else ""
            if has_telegram_link(bio):
                is_admin = False
                try:
                    member = await context.bot.get_chat_member(chat.id, user_id)
                    if member.status in ["administrator", "creator"]:
                        is_admin = True
                except Exception: pass
                if not is_admin:
                    count = user_warning_count.get(user_id, 0)
                    if count < 1:
                        await safe_reply_text(update, "🥺 **Baby, please remove the Telegram link from your bio!**\n🚫 **Promotion is not allowed here.**\n\n👮 @admin check please! 🙏", parse_mode="Markdown")
                        user_warning_count[user_id] = count + 1
                        return
        except Exception as e:
            logger.warning(f"bio check fail {user_id}: {e}")

    clean_text = re.sub(r'@\w+\s*', '', message_text).strip()
    if not clean_text: clean_text = "Hi"

    is_standalone = True
    if update.message.reply_to_message: is_standalone = False
    if is_bot_mentioned: is_standalone = False
    if update.message.forward_date: is_standalone = False

    if is_standalone:
        msg_count = user_msg_counter.get(user_id, 0)
        if msg_count == 0:
            greeting = await generate_greeting(user_id, clean_text)
            if greeting:
                user_mention = f"@{user.username}" if user.username else user.first_name
                final_reply = f"{user_mention} {greeting}"
                await realistic_typing_delay(context, chat.id, final_reply)
                await safe_reply_text(update, final_reply)
                update_history(user_id, clean_text, greeting)
                return

        reply = await get_ai_reply(clean_text, user_id, get_history(user_id))
        if not reply: return
        update_history(user_id, clean_text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        final_reply = f"{user_mention} {reply}"
        await realistic_typing_delay(context, chat.id, final_reply)
        await safe_reply_text(update, final_reply)
        return

    if is_reply_to_bot:
        reply = await get_ai_reply(clean_text, user_id, get_history(user_id))
        if not reply: return
        update_history(user_id, clean_text, reply)
        await realistic_typing_delay(context, chat.id, reply)
        await safe_reply_text(update, reply)
        return

    if is_bot_mentioned:
        reply = await get_ai_reply(clean_text, user_id, get_history(user_id))
        if not reply: return
        update_history(user_id, clean_text, reply)
        await realistic_typing_delay(context, chat.id, reply)
        await safe_reply_text(update, reply)
        return

async def new_member_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.message.new_chat_members:
            return
        chat = update.effective_chat
        # ⭐ Admin nahi to welcome mat do
        if not await is_bot_admin(context, chat.id):
            return
        welcomed_set = _welcomed_users.setdefault(chat.id, set())
        for new_user in update.message.new_chat_members:
            if new_user.is_bot:
                continue
            if new_user.id == OWNER_ID:
                continue
            if new_user.id in welcomed_set:
                continue
            welcomed_set.add(new_user.id)

            if new_user.username:
                name = f"@{new_user.username}"
                welcome_text = get_welcome_message(name)
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await update.message.reply_text(welcome_text)
            else:
                display_name = new_user.first_name or "Dost"
                welcome_text = get_welcome_message(display_name)
                mention_offset = welcome_text.find(display_name)
                await asyncio.sleep(random.uniform(0.5, 1.5))
                if mention_offset != -1:
                    entities = [MessageEntity(
                        type=MessageEntity.TEXT_MENTION,
                        offset=mention_offset,
                        length=len(display_name),
                        user=new_user
                    )]
                    await update.message.reply_text(welcome_text, entities=entities)
                else:
                    await update.message.reply_text(welcome_text)
    except Exception as e:
        logger.warning(f"new_member_welcome error: {e}")

async def chat_member_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ye handler tab fire hota hai jab kisi user ka status change hokar
    'member' banta hai bina normal new_chat_members event ke —
    jaise: admin ne private group/private link ka join request approve kiya.
    new_chat_members waala case yahin duplicate na ho iske liye
    _welcomed_users set check karte hain.
    """
    try:
        result = update.chat_member
        if not result:
            return
        chat = update.effective_chat
        if not chat or chat.type not in ("group", "supergroup"):
            return

        old_status = result.old_chat_member.status
        new_status = result.new_chat_member.status
        new_user = result.new_chat_member.user

        # sirf tab jab pehle member nahi tha aur ab member/admin ban gaya
        if old_status in ("member", "administrator", "creator"):
            return
        if new_status not in ("member", "administrator"):
            return

        if new_user.is_bot:
            return
        if new_user.id == OWNER_ID:
            return

        if not await is_bot_admin(context, chat.id):
            return

        welcomed_set = _welcomed_users.setdefault(chat.id, set())
        if new_user.id in welcomed_set:
            return
        welcomed_set.add(new_user.id)

        if new_user.username:
            name = f"@{new_user.username}"
            welcome_text = get_welcome_message(name)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await context.bot.send_message(chat_id=chat.id, text=welcome_text)
        else:
            display_name = new_user.first_name or "Dost"
            welcome_text = get_welcome_message(display_name)
            mention_offset = welcome_text.find(display_name)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if mention_offset != -1:
                entities = [MessageEntity(
                    type=MessageEntity.TEXT_MENTION,
                    offset=mention_offset,
                    length=len(display_name),
                    user=new_user
                )]
                await context.bot.send_message(chat_id=chat.id, text=welcome_text, entities=entities)
            else:
                await context.bot.send_message(chat_id=chat.id, text=welcome_text)
    except Exception as e:
        logger.warning(f"chat_member_welcome error: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, RetryAfter):
        logger.warning(f"TG rate limit, sleep {error.retry_after}s")
        await asyncio.sleep(error.retry_after)
    elif isinstance(error, TimedOut):
        logger.warning("TG timeout, ignoring...")
    else:
        logger.error("Unhandled:", exc_info=error)

async def main() -> None:
    init_db()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(30).write_timeout(30)
        .connect_timeout(30).pool_timeout(30)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("resetkeys", resetkeys_command))
    application.add_handler(CommandHandler("memory", memory_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("broadcaststats", broadcast_stats_command))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_welcome))
    application.add_handler(ChatMemberHandler(chat_member_welcome, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler((filters.TEXT | filters.Sticker.ALL) & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    port = int(os.environ.get("PORT", 8000))
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")

    if webhook_url:
        logger.info(f"WEBHOOK mode -> {webhook_url}/webhook")
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.requests import Request
        from starlette.routing import Route
        import uvicorn

        async def health(r: Request) -> PlainTextResponse:
            return PlainTextResponse("Bot is alive!")

        async def tg_webhook(r: Request) -> PlainTextResponse:
            data = await r.json()
            await application.update_queue.put(Update.de_json(data, application.bot))
            return PlainTextResponse("OK")

        app = Starlette(routes=[
            Route("/", health, methods=["GET"]),
            Route("/webhook", tg_webhook, methods=["POST"]),
        ])
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(
            url=f"{webhook_url}/webhook",
            allowed_updates=Update.ALL_TYPES
        )
        await uvicorn.Server(
            uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
        ).serve()
    else:
        logger.info("POLLING mode")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
