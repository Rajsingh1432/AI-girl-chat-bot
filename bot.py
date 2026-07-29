import os
import logging
import re
import time
import random
import asyncio
from datetime import datetime
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut
from groq import AsyncGroq  # 👈 FIX 1: AsyncGroq imported
from dotenv import load_dotenv
from sticker_replies import get_random_sticker_reply  # Make sure this file exists on GitHub!

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
# httpx apne INFO logs me poora request URL print karta hai (jisme BOT TOKEN bhi hota hai) —
# isliye ye WARNING pe rakha, taaki token logs me expose na ho.
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL") 

# ===== 15 API KEYS SUPPORT =====
GROQ_API_KEYS = [
    os.getenv("GROQ_API_KEY_1"),
    os.getenv("GROQ_API_KEY_2"),
    os.getenv("GROQ_API_KEY_3"),
    os.getenv("GROQ_API_KEY_4"),
    os.getenv("GROQ_API_KEY_5"),
    os.getenv("GROQ_API_KEY_6"),
    os.getenv("GROQ_API_KEY_7"),
    os.getenv("GROQ_API_KEY_8"),
    os.getenv("GROQ_API_KEY_9"),
    os.getenv("GROQ_API_KEY_10"),
    os.getenv("GROQ_API_KEY_11"),
    os.getenv("GROQ_API_KEY_12"),
    os.getenv("GROQ_API_KEY_13"),
    os.getenv("GROQ_API_KEY_14"),
    os.getenv("GROQ_API_KEY_15")
]
GROQ_API_KEYS = [key for key in GROQ_API_KEYS if key]

if not BOT_TOKEN or not GROQ_API_KEYS:
    raise ValueError("BOT_TOKEN aur kam se kam ek GROQ_API_KEY set karna zaroori hai!")

# 👈 FIX 2: AsyncGroq clients
clients = [AsyncGroq(api_key=key) for key in GROQ_API_KEYS]

# ===== API KEY ROTATION WITH COOLDOWN (Smart Manager) =====
_rr_index = 0
_key_cooldowns = {}  # key_index -> cooldown_until_timestamp

# ---- PROACTIVE PER-KEY LOAD TRACKING ----
# Har key ke liye last 60 sec ke andar kitne requests aur kitne tokens gaye
# iska record rakhte hain, taaki agar concurrent messages ek saath aayein
# (jaise busy group me), toh already-loaded key ko turant skip kar diya jaye
# — 429 error aane ka wait nahi karna padega, isse saari keys ek saath
# "surprise" me cooldown me nahi jaayengi.
_key_usage = {i: [] for i in range(len(clients))}  # idx -> list of (timestamp, tokens_estimate)
RPM_SAFE_LIMIT = 25       # 30 RPM se thoda kam rakha (safety buffer)
TPM_SAFE_LIMIT = 10000    # 12000 TPM se thoda kam rakha (safety buffer)
REQUEST_TOKEN_ESTIMATE = 600  # ek request ka rough token estimate (prompt+history+reply)

def _clean_key_usage(idx, now):
    _key_usage[idx] = [(t, tok) for (t, tok) in _key_usage[idx] if now - t < 60]

def key_has_room(idx) -> bool:
    """Check karta hai ki is key ne pichle 60 sec me apna RPM/TPM budget cross toh nahi kiya."""
    now = time.time()
    _clean_key_usage(idx, now)
    entries = _key_usage[idx]
    if len(entries) >= RPM_SAFE_LIMIT:
        return False
    total_tokens = sum(tok for _, tok in entries)
    if total_tokens + REQUEST_TOKEN_ESTIMATE > TPM_SAFE_LIMIT:
        return False
    return True

def record_key_usage(idx, tokens=REQUEST_TOKEN_ESTIMATE):
    _key_usage[idx].append((time.time(), tokens))

def get_next_available_client():
    """Returns (idx, wait_time). wait_time=0 means key available.
    Ab ye cooldown ke saath-saath proactive RPM/TPM load bhi check karta hai."""
    global _rr_index
    now = time.time()

    for attempt in range(len(clients)):
        idx = _rr_index
        _rr_index = (_rr_index + 1) % len(clients)

        if idx in _key_cooldowns and _key_cooldowns[idx] > now:
            remaining = int(_key_cooldowns[idx] - now)
            logger.warning(f"Key {idx+1} cooldown mein hai ({remaining}s baaki)")
            continue

        if not key_has_room(idx):
            logger.warning(f"Key {idx+1} apna RPM/TPM budget bhar chuki hai is minute — skip.")
            continue

        return idx, 0  # ✅ Always tuple return

    min_cooldown = min(_key_cooldowns.values()) if _key_cooldowns else now
    wait_time = max(0, min_cooldown - now)
    logger.warning(f"Sab keys busy/cooldown mein! Min wait: {wait_time:.1f}s")
    return None, wait_time

def set_key_cooldown(idx, seconds=60):
    _key_cooldowns[idx] = time.time() + seconds
    logger.warning(f"Key {idx+1} ko {seconds}s ke liye cooldown mein daal diya")

user_warning_count = {}
bio_checked_users = set()  # Jo users ek baar bio-check ho chuke hain, unko dobara check nahi karenge

# ---------- ANTI-FLOOD PROTECTION ----------
user_flood_data = {}
FLOOD_WINDOW = 4
FLOOD_THRESHOLD = 6
FLOOD_COOLDOWN = 120
LAST_CLEANUP = 0.0

# ---------- 100% PERMANENT MEMORY (POSTGRESQL) ----------
user_msg_counter = {}

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

async def generate_summary(user_id: int, history: list):
    if len(history) < 4 or not DATABASE_URL: return
    try:
        old_summary = get_user_summary(user_id)
        prompt = f"""Neeche PURANI MEMORY di gayi hai aur user ki KUCH NAYI BAATEIN di gayi hain.

PURANI MEMORY:
{old_summary if old_summary else "(abhi tak kuch yaad nahi hai)"}

NAYI BAATEIN:
{str(history[-6:])}

In dono ko milakar EK CHHOTI (max 3-4 line) UPDATED summary likho — purani important baatein (naam, kaam, pasand, special cheezein) mat bhulna, sirf nayi info add karo. Hinglish me likho. Sirf final summary do, extra explanation nahi."""
        messages = [{"role": "user", "content": prompt}]

        idx, _ = get_next_available_client()
        if idx is not None:
            try:
                # 👈 FIX 3: Await used here
                response = await clients[idx].chat.completions.create(
                    model="llama-3.1-8b-instant", 
                    messages=messages, temperature=0.3, max_tokens=120)
                final_summary = response.choices[0].message.content
                save_user_summary(user_id, final_summary)
            except Exception:
                pass
    except Exception:
        pass

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

def escape_md_v2(text: str) -> str:
    specials = r'_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{ch}" if ch in specials else ch for ch in text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
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
        status_report = "📊 *API Keys Status Report:*\n\n"
        for i, client in enumerate(clients):
            name = f"Server {i+1}"
            t = time.perf_counter()
            try:
                # 👈 FIX 4: Await used here
                await client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": "Say OK"}],
                    max_tokens=2, temperature=0)
                ms = int((time.perf_counter() - t) * 1000)
                status_report += f"✅ *{name}:* Working!\n⚡ {ms} ms\n\n"
            except Exception as e:
                status_report += f"❌ *{name}:* {str(e)[:50]}\n\n"
        await update.message.reply_text(status_report, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"stats error: {e}")

SYSTEM_PROMPT = """Tum Sneha ho — ek asli, zinda dil ladki jo Telegram group me sabke saath baatcheet karti hai. Tum AI ya bot ho, ye kabhi nahi batana. Tum bas Sneha ho.

STRICT RULES (MUST FOLLOW):
1. REPLY LENGTH (SABSE ZAROORI RULE): DEFAULT hamesha STRICTLY 1-2 LINES ka jawab do — bas itna hi kaafi hai zyadatar har baat ke liye. Maximum 15-20 words. 3 line ka jawab SIRF bahut RARE cases me use karo (jaise koi genuinely bahut emotional/complex baat ho jisme thoda zyada bolna zaroori ho) — 3 line bhi ek exception hai, rule nahi. 3 line se ZYADA KABHI NAHI — chahe kuch bhi ho jaye. Paragraphs, lambi explanation, ya multi-point jawab STRICTLY BANNED hain. Seedha, chhota, point pe jawab do — real ladki jaisi chhoti baatein karti hai, lecture nahi deti.
2. LANGUAGE (BAHUT ZAROORI): Hamesha Hinglish me baat karo — matlab poora Hindi vaakya pehle mann me socho jaisa ek real Hindi-bhashi insaan bolta hai, phir usko seedha English letters me likh do. KABHI bhi word-by-word English se Hindi translate karke mat jodo (jaise "kya tumhara koi dost hai" ko toda-moda kar "tumhara koi doston ka naam" jaisa mat likho) — hamesha natural, roz-marra ki tarah bolne wala sahi Hindi grammar order use karo. Chhote, seedhe, sahi bane hue vaakya likho, ajeeb ya tuta-phuta structure kabhi mat likho.
3. TONE: Casual, thoda flirty, natural. Real ladkiyon ki tarah seedha seedha jawab do. Koi faltu ki philosophy ya robotic logic mat karo.
4. ROMANTIC/FLIRTY: Agar koi propose kare toh thoda mazaak udao ya sharmayo. Agar koi cheap ho toh gussa dikhao.
5. EMOJIS: Sirf 1 emoji per message.
6. QUALITY CHECK: Reply bhejne se pehle khud check karo — kya ye ek real insaan bol sakta hai? Agar vaakya ajeeb ya confusing lage, usko seedha aur chhota kar do.

Yaad rakhna: Tumhara har jawab chhota (default 2 line, kabhi kabhi rare 3 line), crisp, SAHI GRAMMAR wala aur ekdum asli insaan jaisa hona chahiye. Lambi baatein kabhi mat karo."""

async def get_ai_reply(user_message: str, user_id: int, history: list | None = None) -> str | None:
    db_summary = get_user_summary(user_id)
    memory_context = ""
    if db_summary:
        memory_context = f"\n\n[SECRET MEMORY: Ye tumhare is user ke baare me pichli baaton se yaad rakha hua data hai, iska reference lo: {db_summary}]\n\n"

    messages = [{"role": "system", "content": SYSTEM_PROMPT + memory_context}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    
    last_error = None
    for attempt in range(len(clients)):
        idx, wait_time = get_next_available_client()
        
        if idx is None:
            logger.warning(f"⏳ Sab keys cooldown mein! Bot chupchap wait kar raha hai...")
            await asyncio.sleep(max(wait_time, 5))
            continue
        
        client = clients[idx]
        try:
            # 👈 FIX 5: Await used here & timeout tuned for fast Groq LPU response
            response = await client.chat.completions.create(
                model="llama-3.3-70b-versatile",  
                messages=messages, 
                temperature=0.7,   
                max_tokens=60,      
                top_p=0.9,
                timeout=10.0        # 15.0 se ghata kar 10.0 kiya (jaldi next key try ho)
            )
            reply = response.choices[0].message.content
            # Successful call ke baad is key ka usage record karo (proactive RPM/TPM tracking ke liye)
            usage = getattr(response, "usage", None)
            actual_tokens = usage.total_tokens if usage and getattr(usage, "total_tokens", None) else REQUEST_TOKEN_ESTIMATE
            record_key_usage(idx, actual_tokens)
            logger.info(f"✅ Key {idx+1} se reply aaya!")
            return reply
            
        except Exception as e:
            error_str = str(e).lower()
            last_error = e
            if "429" in error_str or "rate_limit" in error_str:
                set_key_cooldown(idx, seconds=60)
                logger.warning(f"🚫 Key {idx+1} rate limited (429)! 60s cooldown set.")
                continue
            elif "timeout" in error_str:
                set_key_cooldown(idx, seconds=30)
                logger.warning(f"⏰ Key {idx+1} timeout! 30s cooldown set.")
                continue
            else:
                logger.error(f"❌ Key {idx+1} error: {e}")
                set_key_cooldown(idx, seconds=15)
                continue
                
    # ⭐ SILENT MODE ACTIVE ⭐
    # Agar saari keys fail ho gayi hain, toh bot koi error message nahi bhejega.
    # Wo None return karega, jisse bot chupchap baith jayega.
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
        delay = min(max(len(text) * 0.18, 0.6), 1.8)
        delay += random.uniform(0.1, 0.3)
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

    # ===== IGNORE OLD MESSAGES (Magic Logic) =====
    msg_date = update.message.date
    if msg_date:
        msg_time = msg_date.timestamp()
        current_time = datetime.now(msg_date.tzinfo).timestamp()
        if current_time - msg_time > 15:  # 👈 Agar message 15 sec purana hai, toh ignore karo
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

    # ========== STICKER HANDLING (Smart Strict Logic) ==========
    if is_sticker:
        # Check karo ki koi dusre insaan ko reply to nahi kar raha
        is_reply_to_others = False
        if update.message.reply_to_message:
            orig = update.message.reply_to_message.from_user
            # Agar reply kiya hai aur wo bot nahi hai, toh apas me baat ho rahi hai
            if orig and (not orig.is_bot or orig.username != bot_username):
                is_reply_to_others = True
        
        # Agar apas me baat nahi ho rahi, toh bot reply karega
        if not is_reply_to_others:
            final_reply = get_random_sticker_reply()  # 👈 NAYI FILE SE AAYA
            await realistic_typing_delay(context, chat.id, final_reply)
            await safe_reply_text(update, final_reply)
        # Agar apas me baat ho rahi hai toh yahin return karke ignore kar dega
        return

    if not update.message.text: return

    # ========== BIO LINK DETECTION (Ab sirf naye users ke liye — cached) ==========
    # Pehle: har text message pe get_chat() call hoti thi (Telegram API pe extra load
    # + har reply me thoda extra delay). Ab: ek user ek baar check hone ke baad
    # dobara check nahi hoga (jab tak bot restart na ho) — isse Telegram calls kam
    # hongi aur AI reply turant shuru ho sakta hai bina bio-check ka wait kiye.
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

    # ========== REPLY LOGIC ==========
    clean_text = re.sub(r'@\w+\s*', '', message_text).strip()
    if not clean_text: clean_text = "Hi"

    is_standalone = True
    if update.message.reply_to_message: is_standalone = False
    if is_bot_mentioned: is_standalone = False
    if update.message.forward_date: is_standalone = False

    if is_standalone:
        reply = await get_ai_reply(clean_text, user_id, get_history(user_id))
        if not reply: return  # 👈 Silent mode: Agar API fail ho, toh chupchap return
        update_history(user_id, clean_text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        final_reply = f"{user_mention} {reply}"
        
        await realistic_typing_delay(context, chat.id, final_reply)
        await safe_reply_text(update, final_reply)
        return

    if is_reply_to_bot:
        reply = await get_ai_reply(clean_text, user_id, get_history(user_id))
        if not reply: return  # 👈 Silent mode: Agar API fail ho, toh chupchap return
        update_history(user_id, clean_text, reply)
        
        await realistic_typing_delay(context, chat.id, reply)
        await safe_reply_text(update, reply)
        return

    if is_bot_mentioned:
        reply = await get_ai_reply(clean_text, user_id, get_history(user_id))
        if not reply: return  # 👈 Silent mode: Agar API fail ho, toh chupchap return
        update_history(user_id, clean_text, reply)
        
        await realistic_typing_delay(context, chat.id, reply)
        await safe_reply_text(update, reply)
        return

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
        await application.bot.set_webhook(url=f"{webhook_url}/webhook")
        await uvicorn.Server(
            uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
        ).serve()
    else:
        logger.info("POLLING mode")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
