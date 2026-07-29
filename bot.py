import os
import logging
import re
import time
import random
import asyncio
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Render automatically yeh URL daal dega
DATABASE_URL = os.getenv("DATABASE_URL") 

GROQ_API_KEYS = [
    os.getenv("GROQ_API_KEY_1"),
    os.getenv("GROQ_API_KEY_2"),
    os.getenv("GROQ_API_KEY_3"),
    os.getenv("GROQ_API_KEY_4"),
    os.getenv("GROQ_API_KEY_5")
]
GROQ_API_KEYS = [key for key in GROQ_API_KEYS if key]

if not BOT_TOKEN or not GROQ_API_KEYS:
    raise ValueError("BOT_TOKEN aur kam se kam ek GROQ_API_KEY (1 se 5 me se) set karna zaroori hai!")

clients = [Groq(api_key=key) for key in GROQ_API_KEYS]

# ---------- ROUND-ROBIN API KEY ROTATION WITH COOLDOWN ----------
_rr_counter = {"i": 0}
key_cooldowns = {} # Key index -> Cooldown end time
COOLDOWN_TIME = 45 # Jaise hi limit hit ho, us key ko 45 sec ke liye rest do. 1 min se pehle reset ho jayegi.

def get_next_available_client():
    """Sirf wahi keys return karega jinki cooldown khatam ho chuki hai."""
    n = len(clients)
    now = time.time()
    
    for _ in range(n):
        idx = _rr_counter["i"] % n
        _rr_counter["i"] = (_rr_counter["i"] + 1) % n
        
        # Agar key cooldown mein nahi hai, toh use karo
        if key_cooldowns.get(idx, 0) <= now:
            return idx
            
    return None # Agar saari keys cooldown mein hain toh None return hoga

# ---------- 3 SECOND USER COOLDOWN ----------
user_last_message_time = {}
USER_COOLDOWN = 3.0 # 3 second ka gap zaruri hai takay bot spam na ho aur API limits bachengi

user_warning_count = {}

# ---------- ANTI-FLOOD PROTECTION (Heavy Spammers ke liye) ----------
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
    except Exception as e:
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
    except Exception as e:
        pass

async def generate_summary(user_id: int, history: list):
    if len(history) < 10 or not DATABASE_URL:
        return
    try:
        old_summary = get_user_summary(user_id)

        prompt = f"""Neeche ek user ki PURANI MEMORY di gayi hai aur uski KUCH NAYI BAATEIN di gayi hain.

PURANI MEMORY:
{old_summary if old_summary else "(abhi tak kuch yaad nahi hai)"}

NAYI BAATEIN:
{str(history[-10:])}

Ab in dono ko milakar EK NAYA, UPDATED memory summary likho jisme:
- Purani memory ke saare important facts bilkul mat bhulna.
- Total summary chhoti aur crisp rakho (max 5-6 lines).
- Hinglish me likho. Sirf final summary do."""

        messages = [{"role": "user", "content": prompt}]

        for _ in range(len(clients)):
            idx = get_next_available_client()
            if idx is None:
                break
            try:
                response = clients[idx].chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages, temperature=0.3, max_tokens=200)
                final_summary = response.choices[0].message.content
                save_user_summary(user_id, final_summary)
                break
            except Exception:
                key_cooldowns[idx] = time.time() + COOLDOWN_TIME
                continue
    except Exception:
        pass


def check_flood(user_id: int, is_sticker: bool = False) -> str:
    global LAST_CLEANUP
    now = time.time()
    if now - LAST_CLEANUP > 600:
        expired = [uid for uid, d in user_flood_data.items()
                   if d["cd"] > 0 and now >= d["cd"] and not d["ts"]]
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
MAX_HISTORY_MESSAGES = 10  # 10 rakha hai taaki tokens kam use hon aur limits na fakeele

SAFE_STICKER_PACKS = ["Sigma", "Cats", "Monkeys", "Peach", "Animals",
                      "HonestStickers", "cute", "Memenny", "Dobby"]

WELCOME_IMAGE_URL = "https://ibb.co/Tq2Rb2Nz"


def escape_md_v2(text: str) -> str:
    specials = r'_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{ch}" if ch in specials else ch for ch in text)


# ---------- /start Command ----------
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
            [InlineKeyboardButton("𖤍 ᴀᴅᴅ ᴍᴇ ʙᴀʙʏ 𖤍",
                                  url=f"https://t.me/{bot_username}?startgroup=start")],
            [InlineKeyboardButton("👨‍💻 ʙᴏᴛ ᴅᴇᴠᴇʟᴏᴘᴇʀ ༄",
                                  url="https://t.me/its_raj_king")],
            [InlineKeyboardButton("🌿 sᴜᴘᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ✍︎",
                                  url="https://t.me/KnowRajpapa")],
            [InlineKeyboardButton("☞︎︎︎ sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ☜︎︎",
                                  url="https://t.me/+WJneJ6gRAqg2ZTI1")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_photo(
            photo=WELCOME_IMAGE_URL,
            caption=welcome_text,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"start error: {e}")
        try:
            await update.message.reply_text(
                "🌟 Welcome! Bot me aapka swagat hai! Neeche buttons check karo 👇",
                reply_markup=reply_markup
            )
        except Exception:
            pass


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user.id != OWNER_ID:
            return
        await update.message.reply_text("⏳ Sabhi API Servers check ho rahe hain...")
        status_report = "📊 *API Keys Status Report:*\n\n"
        for i, client in enumerate(clients):
            name = f"Server {i+1}"
            t = time.perf_counter()
            try:
                client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": "Say OK"}],
                    max_tokens=2, temperature=0)
                ms = int((time.perf_counter() - t) * 1000)
                status_report += f"✅ *{name}:* Working!\n⚡ {ms} ms\n\n"
            except Exception as e:
                ms = int((time.perf_counter() - t) * 1000)
                status_report += f"❌ *{name}:* {str(e)[:50]}\n\n"
        await update.message.reply_text(status_report, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"stats error: {e}")


# System Prompt mein sabse important line add ki gayi hai: "Reply in 2-3 short lines"
SYSTEM_PROMPT = """Tum Sneha ho — ek asli, zinda dil ladki jo Telegram group me sabke saath baatcheet karti hai. Tum AI ya bot ho, ye kabhi nahi batana. Tum bas Sneha ho.

CHAT KA STYLE (Sabse Zaroori Rules):

1. REPLY LENGTH — STRICTLY 2-3 LINES: Tumhara reply hamesha sirf 2 ya maximum 3 short lines ka hona chahiye. Samne wale ke message ka size dekh kar usse thoda bada ya chhota jawab do, par kabhi bhi paragraph ya lamba text mat likho. Real insaan Telegram pe short baat karta hai.

2. LANGUAGE: Hamesha Hinglish me baat karo (Hindi shabd English letters me). Agar koi English me puche toh English, aur Devanagari (हिंदी) me puche toh Hindi script me.

3. MOOD-MATCHING:
   - NORMAL USERS: Casual, friendly, halki-phulki baat karo.
   - ROMANTIC/FLIRTY USERS: Thoda flirt/tease karo, natural aur simple. Filmy drama mat bolo.
   - DEEP ROMANCE CHaahne WALE USERS: Sweet, emotional aur close baatein karo, par explicit kabhi mat bano.
   - Romance ka level unki baaton se match karo, khud se aage mat bado.

4. AGAR KOI GAALI DE YA BADTAMEEZI KARE: Seedha gussa dikhao (😠, 🙄), daant do, par tum khud kabhi gaali ya abusive language use nahi karogi.

5. NO ASSISTANT: Tum kisi ki help karne wali assistant nahi ho. Tumhari apni personality aur mood hai.

6. EMOJIS: 1-2 emoji ek message me kaafi hain, zyada mat lagao.

7. STRONG MEMORY: Pichli baatcheet ke important points yaad rakho aur unka reference do.

Yaad rakhna: 2-3 lines me sab cover karo, ekdum asli insaan jaisa lage — na filmy, na robotic."""


async def get_ai_reply(user_message: str, user_id: int, history: list | None = None) -> str:
    db_summary = get_user_summary(user_id)
    memory_context = ""
    if db_summary:
        memory_context = f"\n\n[SECRET MEMORY: Ye tumhare is user ke baare me pichli baaton se yaad rakha hua data hai, iska reference lo: {db_summary}]\n\n"

    messages = [{"role": "system", "content": SYSTEM_PROMPT + memory_context}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    
    last_error = None

    for _ in range(len(clients)):
        idx = get_next_available_client()
        if idx is None:
            # Agar saari keys cooldown mein hain toh thoda wait karo aur check karo
            await asyncio.sleep(2)
            idx = get_next_available_client()
            if idx is None:
                break
                
        try:
            response = clients[idx].chat.completions.create(
                model="llama-3.1-8b-instant", # 8B limits bachata hai aur fast reply deta hai
                messages=messages, 
                temperature=0.9,
                max_tokens=80, # 80 tokens = approx 2-3 lines. Limits full hone ka durr gayab
                top_p=0.95,
                timeout=4.0  
            )
            return response.choices[0].message.content
            
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            if "429" in err_str or "rate_limit" in err_str:
                logger.warning(f"Server {idx+1} slow/limited. 45 sec cooldown set.")
                key_cooldowns[idx] = time.time() + COOLDOWN_TIME
                continue
            else:
                logger.error(f"AI Error Server {idx+1}: {e}")
                break
                
    if last_error and ("429" in str(last_error) or "rate_limit" in str(last_error).lower()):
        return "Arre yaar, meri saari chat limits thodi der ke liye full ho gayi hain! 😭 1 minute ruk jao!"
    return "Are, meri neend khul gayi! 😴 thoda sa gadbad ho gaya, fir se bolo na!"


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
    if not text:
        return False
    return bool(re.search(r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:[a-zA-Z0-9_]+)', text)) or \
           bool(re.search(r'@[a-zA-Z0-9_]{4,}', text))


async def safe_reply_text(update: Update, text: str, **kwargs) -> None:
    try:
        await update.message.reply_text(text, **kwargs)
    except Exception as e:
        logger.warning(f"reply_text fail: {e}")


async def safe_reply_sticker(update: Update, file_id: str) -> None:
    try:
        await update.message.reply_sticker(file_id)
    except Exception as e:
        logger.warning(f"reply_sticker fail: {e}")


# ---------- REALISTIC TYPING SIMULATOR ----------
async def realistic_typing_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    try:
        # Chhote messages ke liye kam delay
        delay = min(max(len(text) * 0.04, 0.6), 4.0)
        delay += random.uniform(0.2, 0.6)
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
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if update.effective_user.is_bot:
        return
    if not update.message.text and not update.message.sticker:
        return

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id
    is_sticker = bool(update.message.sticker and not update.message.text)

    # ========== 3 SECOND USER COOLDOWN (API LIMITS SAVE KARNE KE LIYE) ==========
    now = time.time()
    if not is_sticker:
        last_time = user_last_message_time.get(user_id, 0)
        if now - last_time < USER_COOLDOWN:
            return # User 3 sec mein 2 message bhej raha hai, ignore karo (silent drop)
        user_last_message_time[user_id] = now

    # ========== HEAVY SPAM CHECK ==========
    flood_status = check_flood(user_id, is_sticker=is_sticker)
    if flood_status == "cooldown":
        return
    if flood_status == "flood":
        await safe_reply_text(update, "Ruko ruko baby! 😤 Itni jaldi kya hai? 2 minute baad aana!")
        return

    # ========== ZERO INTERFERENCE ==========
    bot_username = context.bot.username
    message_text = update.message.text or ""

    if update.message.reply_to_message:
        orig = update.message.reply_to_message.from_user
        if orig and (not orig.is_bot or orig.username != bot_username):
            return

    # ========== STICKER HANDLING ==========
    if is_sticker:
        try:
            if random.random() < 0.7:
                chosen_pack_name = random.choice(SAFE_STICKER_PACKS)
                sticker_set = await context.bot.get_sticker_set(chosen_pack_name)
                if sticker_set and sticker_set.stickers:
                    await safe_reply_sticker(update, random.choice(sticker_set.stickers).file_id)
                    return
        except Exception as e:
            logger.warning(f"sticker pack fail: {e}")

        try:
            sticker_prompt = "User ne ek sticker bheja hai, is par mazedar Hinglish reaction do."
            reply = await get_ai_reply(sticker_prompt, user_id, get_history(user.id))
            update_history(user.id, sticker_prompt, reply)
            user_mention = f"@{user.username}" if user.username else user.first_name
            final_reply = f"{user_mention} {reply}"
            
            await realistic_typing_delay(context, chat.id, final_reply)
            await safe_reply_text(update, final_reply)
        except Exception as e:
            logger.error(f"sticker AI fail: {e}")
        return

    if not update.message.text:
        return

    # ========== BIO LINK DETECTION ==========
    try:
        full_user = await context.bot.get_chat(user_id)
        bio = full_user.bio if full_user.bio else ""
        if has_telegram_link(bio):
            is_admin = False
            try:
                member = await context.bot.get_chat_member(chat.id, user_id)
                if member.status in ["administrator", "creator"]:
                    is_admin = True
            except Exception:
                pass
            if not is_admin:
                count = user_warning_count.get(user_id, 0)
                if count < 3:
                    await safe_reply_text(update,
                        "🥺 **Baby, please remove the Telegram link from your bio!**\n"
                        "🚫 **Promotion is not allowed here.**\n\n"
                        "👮 @admin – this baby has a link in their bio. "
                        "If it's okay with you, then no problem, but please check! 🙏",
                        parse_mode="Markdown")
                    user_warning_count[user_id] = count + 1
                    return
    except Exception as e:
        logger.warning(f"bio check fail {user_id}: {e}")

    # ========== REPLY LOGIC ==========
    is_standalone = True
    if update.message.reply_to_message:
        is_standalone = False
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type in ["mention", "text_mention"]:
                is_standalone = False
                break
    if update.message.forward_date:
        is_standalone = False

    if is_standalone:
        reply = await get_ai_reply(message_text, user_id, get_history(user_id))
        update_history(user_id, message_text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        final_reply = f"{user_mention} {reply}"
        
        await realistic_typing_delay(context, chat.id, final_reply)
        await safe_reply_text(update, final_reply)
        return

    is_reply_to_bot = False
    if update.message.reply_to_message:
        orig = update.message.reply_to_message.from_user
        if orig and orig.is_bot and orig.username == bot_username:
            is_reply_to_bot = True

    if is_reply_to_bot:
        reply = await get_ai_reply(message_text, user_id, get_history(user_id))
        update_history(user_id, message_text, reply)
        
        await realistic_typing_delay(context, chat.id, reply)
        await safe_reply_text(update, reply)
        return

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

    if is_bot_mentioned:
        reply = await get_ai_reply(message_text, user_id, get_history(user_id))
        update_history(user_id, message_text, reply)
        
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
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Sticker.ALL) & ~filters.COMMAND,
            handle_message))
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
