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
DATABASE_URL = os.getenv("DATABASE_URL")

GROQ_API_KEYS = [
    os.getenv("GROQ_API_KEY_1"),
    os.getenv("GROQ_API_KEY_2"),
    os.getenv("GROQ_API_KEY_3"),
    os.getenv("GROQ_API_KEY_4"),
    os.getenv("GROQ_API_KEY_5"),
]
GROQ_API_KEYS = [key for key in GROQ_API_KEYS if key]

if not BOT_TOKEN or not GROQ_API_KEYS:
    raise ValueError("BOT_TOKEN aur GROQ_API_KEYS zaroori hain!")

clients = [Groq(api_key=key) for key in GROQ_API_KEYS]

# ===== API KEY ROTATION WITH COOLDOWN =====
_rr_index = 0
_key_cooldowns = {}  # key_index -> cooldown_until_timestamp

def get_next_available_client():
    """Returns next available client, skipping cooldown keys"""
    global _rr_index
    now = time.time()
    
    for attempt in range(len(clients)):
        idx = _rr_index
        _rr_index = (_rr_index + 1) % len(clients)
        
        # Check if this key is in cooldown
        if idx in _key_cooldowns and _key_cooldowns[idx] > now:
            remaining = int(_key_cooldowns[idx] - now)
            logger.warning(f"Key {idx+1} cooldown mein hai ({remaining}s baaki)")
            continue
        
        return idx
    
    # All keys in cooldown - find the one with least remaining time
    min_cooldown = min(_key_cooldowns.values()) if _key_cooldowns else now
    wait_time = max(0, min_cooldown - now)
    logger.warning(f"Sab keys cooldown mein! Min wait: {wait_time:.1f}s")
    return None, wait_time

def set_key_cooldown(idx, seconds=60):
    """Set cooldown for a specific key"""
    _key_cooldowns[idx] = time.time() + seconds
    logger.warning(f"Key {idx+1} ko {seconds}s ke liye cooldown mein daal diya")

# ===== ANTI-FLOOD =====
user_last_message_time = {}

def check_flood(user_id, cooldown=3):
    """Returns True if user is flooding"""
    now = time.time()
    last_time = user_last_message_time.get(user_id, 0)
    
    if now - last_time < cooldown:
        return True
    
    user_last_message_time[user_id] = now
    return False

# ===== DATABASE =====
def get_db_conn():
    if not DATABASE_URL:
        return None
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"DB connection error: {e}")
        return None

def init_db():
    conn = get_db_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS user_memory (
                    user_id BIGINT PRIMARY KEY,
                    summary TEXT,
                    updated_at REAL
                )''')
                cur.execute('''CREATE TABLE IF NOT EXISTS user_history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    role TEXT,
                    content TEXT,
                    timestamp REAL
                )''')
        logger.info("✅ PostgreSQL connected!")
    except Exception as e:
        logger.error(f"DB init error: {e}")
    finally:
        if conn:
            conn.close()

def save_user_message(user_id, role, content):
    conn = get_db_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO user_history (user_id, role, content, timestamp) VALUES (%s, %s, %s, %s)",
                    (user_id, role, content, time.time())
                )
                cur.execute("DELETE FROM user_history WHERE user_id = %s AND id NOT IN (SELECT id FROM user_history WHERE user_id = %s ORDER BY timestamp DESC LIMIT 8)", (user_id, user_id))
    except Exception as e:
        logger.error(f"Save error: {e}")
    finally:
        if conn:
            conn.close()

def get_user_history(user_id, limit=6):
    """Get last 6 messages only (instead of 20)"""
    conn = get_db_conn()
    if not conn:
        return []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT role, content FROM user_history WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s", (user_id, limit))
                rows = cur.fetchall()
        return rows[::-1]
    except Exception as e:
        logger.error(f"Get history error: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_db_summary(user_id):
    conn = get_db_conn()
    if not conn:
        return ""
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT summary FROM user_memory WHERE user_id = %s", (user_id,))
                result = cur.fetchone()
        return result[0] if result else ""
    except Exception as e:
        logger.error(f"Get summary error: {e}")
        return ""
    finally:
        if conn:
            conn.close()

def update_db_summary(user_id, summary):
    conn = get_db_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute('''INSERT INTO user_memory (user_id, summary, updated_at) 
                               VALUES (%s, %s, %s) 
                               ON CONFLICT (user_id) DO UPDATE SET summary = %s, updated_at = %s''',
                            (user_id, summary, time.time(), summary, time.time()))
    except Exception as e:
        logger.error(f"Update summary error: {e}")
    finally:
        if conn:
            conn.close()

def clear_db_memory(user_id):
    conn = get_db_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_history WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM user_memory WHERE user_id = %s", (user_id,))
    except Exception as e:
        logger.error(f"Clear memory error: {e}")
    finally:
        if conn:
            conn.close()

# ===== MEMORY MANAGEMENT =====
MAX_HISTORY = 6  # 20 se 6 kar diya - tokens 70% kam

async def summarize_history(self, user_id):
    """Summarize old conversation to save tokens"""
    old_history = get_user_history(user_id, limit=20)
    if len(old_history) < MAX_HISTORY:
        return
    
    to_summarize = old_history[:-MAX_HISTORY]
    if not to_summarize:
        return
    
    summary_text = "\n".join([f"{role}: {content}" for role, content in to_summarize])
    existing_summary = get_db_summary(user_id)
    
    summarize_prompt = f"""Existing summary: {existing_summary}

New conversation to summarize:
{summary_text}

Create a concise summary in 2-3 sentences. Focus on key facts only."""

    try:
        response = self.client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": summarize_prompt}],
            max_tokens=200,
            temperature=0.3,
        )
        new_summary = response.choices[0].message.content
        update_db_summary(user_id, new_summary)
        
        # Delete old messages from DB
        conn = get_db_conn()
        if conn:
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM user_history WHERE user_id = %s AND timestamp < (SELECT MIN(timestamp) FROM (SELECT timestamp FROM user_history WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s) AS t)", (user_id, user_id, MAX_HISTORY))
            except Exception as e:
                logger.error(f"Delete old messages error: {e}")
            finally:
                if conn:
                    conn.close()
    except Exception as e:
        logger.error(f"Summarize error: {e}")

# ===== AI REPLY =====
async def get_ai_reply(self, user_id, user_message):
    """Get AI response with smart API key rotation"""
    await self.summarize_history(user_id)
    
    db_summary = get_db_summary(user_id)
    history = get_user_history(user_id, limit=MAX_HISTORY)
    
    messages = []
    if db_summary:
        messages.append({"role": "system", "content": f"Previous context summary: {db_summary}"})
    
    messages.append({"role": "system", "content": SYSTEM_PROMPT})
    
    for role, content in history:
        messages.append({"role": role, "content": content})
    
    messages.append({"role": "user", "content": user_message})
    
    last_error = None
    for attempt in range(len(clients)):
        result = get_next_available_client()
        
        if result is None:
            wait_time = 30
            logger.warning(f"⏳ Sab keys cooldown mein! {wait_time}s wait...")
            await asyncio.sleep(wait_time)
            continue
        
        idx = result
        client = clients[idx]
        
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",  # Fast model
                messages=messages,
                temperature=0.7,
                max_tokens=250,
                top_p=0.95,
                timeout=10,
            )
            reply = response.choices[0].message.content
            logger.info(f"✅ Key {idx+1} se reply aaya!")
            return reply
            
        except Exception as e:
            error_str = str(e).lower()
            last_error = e
            
            if "429" in error_str or "rate_limit" in error_str:
                # Rate limit hit - set cooldown for this key
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
    
    # All keys failed
    logger.error(f"💀 Sab API keys fail ho gayi! Last error: {last_error}")
    return "Sorry baby, mai thoda apne honey ke sath busy hun. 1 minute ruko kar phir mai hazir ho jaungi usko bye bolke! 😔"

# ===== SYSTEM PROMPT =====
SYSTEM_PROMPT = """Tum Sneha ho — ek asli, zinda-dil ladki jo Telegram group me chat karti hai. Tumhara nature casual, friendly aur thoda flirty hai. Tum Hindi aur English mix (Hinglish) bolti ho, bilkul natural insaan ki tarah.

Tumhre rules:
1. Reply lamba na rakho — 2-3 lines max, natural aur casual tone
2. Hinglish use karo — Hindi + English mix
3. Emojis use karo but over mat karo — 😊😂🤭❤️ bas
4. Tum koi AI ya bot nahi ho — tum ek ladki ho
5. Tum apne opinions rakhti ho, arguments me participate karti ho
6. Casual questions pe simple reply do
7. Koi insaan ki tareef me interested ho sakti ho
8. Tumhe pasand hai music, movies, aur late-night chats
9. Tum thodi dramatic bhi ho sakti ho
10. Tum kabhi kabhi sarcastic bhi hoti ho
11. Short messages do, paragraph mat banao
12. Tum real emotions dikhao — khush, naraz, excited, sab"""

# ===== ESCAPE MARKDOWN =====
def escape_md_v2(text):
    if not text:
        return ""
    specials = r'_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{ch}" if ch in specials else ch for ch in text)

# ===== WELCOME MESSAGE =====
WELCOME_TEXT = (
    "🌟 *Welcome! Main Sneha hoon!* 🌸\n\n"
    "Main ek AI girl bot hoon. Mujhse kisi bhi topic pe chat kar sakte ho!\n\n"
    "👉 Just reply to my message or tag me with @username!\n\n"
    "💖 Enjoy chatting!"
)

def get_welcome_keyboard():
    keyboard = [
        [InlineKeyboardButton("💬 Chat with me", callback_data="start_chat")],
        [
            InlineKeyboardButton("👑 Add to Group", url=f"https://t.me/{os.getenv('BOT_USERNAME', 'bot')}?startgroup=true"),
            InlineKeyboardButton("ℹ️ About", callback_data="about_info"),
        ],
        [InlineKeyboardButton("👤 Owner", url="https://t.me/its_raj_king")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ===== START COMMAND =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type == "private":
        await update.message.reply_text(WELCOME_TEXT, reply_markup=get_welcome_keyboard(), parse_mode="Markdown")
    else:
        await update.message.reply_text("👋 Main group me active hoon! Tag karo ya reply karo!")

# ===== STATS COMMAND =====
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    
    now = time.time()
    status_lines = []
    for i, key in enumerate(GROQ_API_KEYS):
        cooldown_remaining = int(_key_cooldowns.get(i, 0) - now) if i in _key_cooldowns else 0
        if cooldown_remaining > 0:
            status_lines.append(f"🔑 Key {i+1}: ⏳ Cooldown ({cooldown_remaining}s)")
        else:
            status_lines.append(f"🔑 Key {i+1}: ✅ Ready")
    
    text = "📊 *API Keys Status:*\n\n" + "\n".join(status_lines)
    await update.message.reply_text(text, parse_mode="Markdown")

# ===== MAIN MESSAGE HANDLER =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    
    user = update.effective_user
    user_id = user.id
    chat_type = update.effective_chat.type
    text = message.text.strip()
    bot_username = context.bot.username.lower()
    
    # ===== PRIVATE CHAT: Direct reply =====
    if chat_type == "private":
        if check_flood(user_id, cooldown=2):
            await message.reply_text("Ruk jao baby! Thoda der baar message karo. 🤭")
            return
        
        save_user_message(user_id, "user", text)
        
        processing_msg = await message.reply_text("Soch rahi hoon... 🤔")
        
        try:
            reply = await get_ai_reply(context, user_id, text)
            save_user_message(user_id, "assistant", reply)
            await processing_msg.edit_text(escape_md_v2(reply), parse_mode="MarkdownV2")
        except Exception as e:
            logger.error(f"Private chat reply error: {e}")
            await processing_msg.edit_text("Kuch error aa gaya! Phir try karo. 😅")
        return
    
    # ===== GROUP CHAT: Check conditions =====
    is_admin = (user_id == OWNER_ID)
    
    # Check if bot's @username is mentioned
    is_mentioned = f"@{bot_username}" in text.lower()
    
    # Check if this is a reply to bot's message
    is_reply_to_bot = (
        message.reply_to_message 
        and message.reply_to_message.from_user 
        and message.reply_to_message.from_user.id == context.bot.id
    )
    
    # ⭐ FIX: Admin OR mentioned OR reply_to_bot -> ALL should get response
    should_reply = is_admin or is_mentioned or is_reply_to_bot
    
    if not should_reply:
        return
    
    # Flood check (skip for admin)
    if not is_admin and check_flood(user_id, cooldown=3):
        await message.reply_text("Ruk jao! Spam mat karo. 🤭")
        return
    
    # Clean the text - remove bot's @username from message
    clean_text = re.sub(r'@\w+\s*', '', text).strip()
    if not clean_text:
        clean_text = "Hi"
    
    save_user_message(user_id, "user", clean_text)
    
    try:
        typing_action = await message.reply_text("Soch rahi hoon... 🤔")
        reply = await get_ai_reply(context, user_id, clean_text)
        save_user_message(user_id, "assistant", reply)
        await typing_action.edit_text(escape_md_v2(reply), parse_mode="MarkdownV2")
    except RetryAfter as e:
        logger.warning(f"Flood wait: {e.retry_after}s")
        await asyncio.sleep(e.retry_after)
        try:
            reply = await get_ai_reply(context, user_id, clean_text)
            save_user_message(user_id, "assistant", reply)
            await typing_action.edit_text(escape_md_v2(reply), parse_mode="MarkdownV2")
        except Exception as e2:
            logger.error(f"Retry error: {e2}")
    except Exception as e:
        logger.error(f"Group reply error: {e}")

# ===== STICKER HANDLER =====
async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.sticker:
        return
    
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    
    if chat_type == "private":
        sticker_emoji = message.sticker.emoji or "😄"
        replies = {
            "😀": "Haha cute sticker! 😄",
            "😂": "Haha mujhe bhi hasi aa rahi! 😂",
            "❤️": "Aww love you too! ❤️",
            "😢": "Arre kya hua? Sad mat ho! 🤗",
            "😡": "Arre gussa kyu? Cool down! 😅",
            "👍": "Done! 👍",
            "🔥": "Fire! 🔥",
        }
        reply_text = replies.get(sticker_emoji, f"Nice sticker! {sticker_emoji}")
        await message.reply_text(reply_text)
    elif chat_type in ["group", "supergroup"]:
        bot_username = context.bot.username.lower()
        text = message.caption or ""
        is_mentioned = f"@{bot_username}" in text.lower()
        is_reply_to_bot = (
            message.reply_to_message 
            and message.reply_to_message.from_user 
            and message.reply_to_message.from_user.id == context.bot.id
        )
        is_admin = (user_id == OWNER_ID)
        
        if is_admin or is_mentioned or is_reply_to_bot:
            sticker_emoji = message.sticker.emoji or "😄"
            await message.reply_text(f"Cute sticker! {sticker_emoji} Mujhe text message karo na! 😊")

# ===== BIO LINK =====
async def handle_bio_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        return
    if hasattr(update, 'chat_join_request'):
        await update.approve_chat_join_request(chat_id=update.chat_join_request.chat.id)

# ===== CALLBACK QUERY =====
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    
    await query.answer()
    data = query.data
    
    if data == "start_chat":
        await query.message.reply_text("💬 Mujhe message karo! Main yahan hoon! 😊")
    elif data == "about_info":
        await query.message.reply_text(
            "ℹ️ *About Me*\n\n"
            "Main Sneha hoon — Apki crush - cutie 🥹!\n\n"
            "🤖 Powered by: Raj Engine 3.0 (Database)\n"
            "💬 Language: Hinglish - Hindi - English \n\n"
            "👤 My Honey: @its_raj_king\n\n"
            "Mujhe group me add karo aur chat karo mai apke liye hamesha hazir hun!",
            parse_mode="Markdown"
        )

# ===== MAIN =====
def main():
    init_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    
    # Messages
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    ))
    
    # Stickers
    application.add_handler(MessageHandler(
        filters.Sticker.ALL,
        handle_sticker
    ))
    
    # Callbacks
    application.add_handler(MessageHandler(
        filters.CALLBACK_QUERY,
        handle_callback
    ))
    
    # Health check for Render
    port = int(os.environ.get("PORT", 8000))
    
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    
    async def health(request):
        return PlainTextResponse("OK")
    
    web_app = Starlette(routes=[Route("/", health)])
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=os.environ.get("WEBHOOK_PATH", ""),
        webhook_url=f"{os.environ.get('RENDER_EXTERNAL_URL', '')}/{os.environ.get('WEBHOOK_PATH', '')}" if os.environ.get('RENDER_EXTERNAL_URL') else None,
        web_app=web_app
    )

if __name__ == "__main__":
    main()
