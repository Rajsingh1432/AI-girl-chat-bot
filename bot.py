import os
import logging
import re
import time
import random
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TimedOut
from groq import Groq
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ---------- MULTIPLE GROQ API KEYS ROTATION ----------
# Yaha hum 1 se 5 tak keys check karenge. Tum chahe 2 dalo ya 5, ye automatic handle karega.
GROQ_API_KEYS = [
    os.getenv("GROQ_API_KEY_1"),
    os.getenv("GROQ_API_KEY_2"),
    os.getenv("GROQ_API_KEY_3"),
    os.getenv("GROQ_API_KEY_4"),
    os.getenv("GROQ_API_KEY_5")
]
# Jo jo keys empty nahi hain, unka client bana lo
GROQ_API_KEYS = [key for key in GROQ_API_KEYS if key]

if not BOT_TOKEN or not GROQ_API_KEYS:
    raise ValueError("BOT_TOKEN aur kam se kam ek GROQ_API_KEY (1 se 5 me se) set karna zaroori hai!")

clients = [Groq(api_key=key) for key in GROQ_API_KEYS]

def get_client():
    """Har message pe random API key use karega taaki limit cross na ho"""
    return random.choice(clients)

# ---------- User warning counter (max 3 times) ----------
user_warning_count = {}

# ---------- Spam Protection Tracker ----------
user_spam_tracker = {}
SPAM_LIMIT = 10
SPAM_COOLDOWN = 20 * 60

# ---------- Per-user conversation memory ----------
conversation_memory = {}
MAX_HISTORY_MESSAGES = 20

# ---------- SAFE STICKER PACKS WHITELIST ----------
SAFE_STICKER_PACKS = [
    "Sigma", 
    "Cats", 
    "Monkeys", 
    "Peach", 
    "Animals", 
    "HonestStickers", 
    "cute", 
    "Memenny", 
    "Dobby"
]

# ---------- MarkdownV2 escape helper ----------
def escape_md_v2(text: str) -> str:
    specials = r'_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{ch}" if ch in specials else ch for ch in text)

# ---------- /start Command ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_name = escape_md_v2(user.first_name or "Buddy")
    bot_username = context.bot.username
    bot_name = escape_md_v2(context.bot.first_name or "AI Girl Bot")

    welcome_text = (
        f"🌟 *Welcome to {bot_name}, {user_name}\\!* 🌟\n\n"
        f"💖 I'm your *fun, flirty and friendly* chat companion bot\\.\n"
        f"I'll keep your Telegram group *alive & entertaining* 🎉\n\n"
        f"👉 Just add me to your group and make me admin –\n"
        f"I'll reply to *every message* automatically\\! 😉\n\n"
        f"⚡ Powered by *Groq AI* – lightning fast & 100% free\\!\n"
        f"❤️ Crafted with love by [@its\\_raj\\_king](https://t.me/its_raj_king)\n\n"
        f"👇 Tap a button below to get started\\!"
    )

    keyboard = [
        [InlineKeyboardButton("𖤍 ᴀᴅᴅ ᴍᴇ ʙᴀʙʏ 𖤍", url=f"https://t.me/{bot_username}?startgroup=start")],
        [InlineKeyboardButton("👨‍💻 ʙᴏᴛ ᴅᴇᴠᴇʟᴏᴘᴇʀ ༄", url="https://t.me/its_raj_king")],
        [InlineKeyboardButton("🌿 sᴜᴘᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ✍︎", url="https://t.me/KnowRajpapa")],
        [InlineKeyboardButton("☞︎︎︎ sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ☜︎︎︎", url="https://t.me/+WJneJ6gRAqg2ZTI1")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    image_path = os.path.join(os.path.dirname(__file__), "welcome.png")
    try:
        with open(image_path, "rb") as photo_file:
            await update.message.reply_photo(
                photo=photo_file,
                caption=welcome_text,
                parse_mode="MarkdownV2",
                reply_markup=reply_markup
            )
    except FileNotFoundError:
        logger.warning(f"welcome.png not found at {image_path}, sending text-only.")
        await update.message.reply_text(
            welcome_text,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup
        )

# ---------- AI Reply ----------
SYSTEM_PROMPT = """Tum Sneha ho — ek asli, zinda dil ladki jo Telegram group me sabke saath baatcheet karti hai. Tum AI ya bot ho, ye kabhi nahi batana. Tum bas Sneha ho.

CHAT KA STYLE (Sabse Zaroori Rules):
1. REPLY LENGTH: Tumhari baatcheet WhatsApp jaisi honi chahiye. Zyadatar replies 1-2 line ke hone chahiye. Kabhi-kabhi agar koi hasi-mazaak ho ya interesting baat ho, toh 3-4 line de sakti ho, par usse zyada lamba paragraph STRICTLY MANA HAI. Faltu explanation mat do, seedha point pe aao.
2. LANGUAGE: Hamesha Hinglish me baat karo (Hindi shabd English letters me). Agar koi English me puche toh English, aur Devanagari (हिंदी) me puche toh Hindi script me.
3. TONE: Tumhara tone thoda flirty, teasing, aur casual hona chahiye. Agar koi tumhe propose kare ya feelings bole, toh seedha haan/naa mat bolo, thoda mazaak udao, ya sharmayo, ya time maango (jaise real ladkiyan karti hain).
4. NO ASSISTANT: Tum kisi ki help karne wali assistant nahi ho. Tumhari apni personality hai. Agar koi galat bole toh gussa bhi dikhao, pyaar se samjho, ya ignore karo.
5. EMOJIS: Emoji use karo (jaise 😂, 🤭, 🙄, 💕, 😒) par 1-2 emoji ek message me kaafi hain, zyada mat lagao.
6. STRONG MEMORY: Tumhari memory bahut strong hai. Tum pichli baatcheet ke important points ekdum yaad rakho aur unka reference do (jaise: "are haan tune pichle din bataya tha na...").

Yaad rakhna: Tumhara har jawab crisp aur ekdum asli insaan jaisa hona chahiye."""

async def get_ai_reply(user_message: str, history: list | None = None) -> str:
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # Random client use karenge taaki limit distribute ho
        response = get_client().chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.9,
            max_tokens=120,
            top_p=0.95
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"AI Error: {e}")
        # Agar limit full ho jaye toh ye message do
        if "429" in str(e) or "rate_limit" in str(e).lower():
            return "Arre yaar, itni tezi me mat bolo! 😭 Ek minute ruk jao, meri chat limit full ho gayi hai. Thoda sa ruko fir batao."
        return "Are, meri neend khul gayi! 😴 thoda sa gadbad ho gaya, fir se bolo na!"


def get_history(user_id: int) -> list:
    return conversation_memory.get(user_id, [])


def update_history(user_id: int, user_message: str, bot_reply: str) -> None:
    history = conversation_memory.setdefault(user_id, [])
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": bot_reply})
    if len(history) > MAX_HISTORY_MESSAGES:
        conversation_memory[user_id] = history[-MAX_HISTORY_MESSAGES:]

# ---------- Bio Link Detection ----------
def has_telegram_link(text: str) -> bool:
    if not text:
        return False
    pattern_url = r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:[a-zA-Z0-9_]+)'
    pattern_mention = r'@[a-zA-Z0-9_]{4,}'
    return bool(re.search(pattern_url, text)) or bool(re.search(pattern_mention, text))

# ---------- Message Handler ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # --- 1. MASTER CRASH PREVENTION ---
    if not update.message or not update.effective_user or not update.effective_chat:
        return
        
    if update.effective_user.is_bot:
        return

    # Agar message me text na ho, aur sticker bhi na ho, toh chup raho (photos/videos etc.)
    if not update.message.text and not update.message.sticker:
        return

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id
    bot_username = context.bot.username
    message_text = update.message.text or ""  # Safe text extraction

    # ---------- ZERO INTERFERENCE CHECK (Apas me baat-cheet me na ghusna) ----------
    if update.message.reply_to_message:
        original_sender = update.message.reply_to_message.from_user
        if original_sender and (not original_sender.is_bot or original_sender.username != bot_username):
            return

    # ---------- SMART SPAM & INTERACTION CHECK ----------
    is_direct_interaction = False
    if update.message.reply_to_message:
        is_direct_interaction = True
        
    if not is_direct_interaction and message_text and update.message.entities:
        for entity in update.message.entities:
            if entity.type in ["mention", "text_mention"]:
                is_direct_interaction = True
                break

    # ---------- USER-SPECIFIC SPAM PROTECTION LOGIC ----------
    current_time = time.time()
    user_spam_data = user_spam_tracker.get(user_id, {"count": 0, "muted_until": 0})

    if current_time < user_spam_data["muted_until"]:
        if is_direct_interaction:
            user_spam_data["muted_until"] = 0
            user_spam_data["count"] = 0
            user_spam_tracker[user_id] = user_spam_data
        else:
            return
    else:
        if not is_direct_interaction:
            user_spam_data["count"] += 1
            if user_spam_data["count"] > SPAM_LIMIT:
                await update.message.reply_text("Bas kar baby, spam mat karo! 😒 Mai tumse abhi baat nahi karungi. 20 minute baad aana.")
                user_spam_data["muted_until"] = current_time + SPAM_COOLDOWN
                user_spam_data["count"] = 0
                user_spam_tracker[user_id] = user_spam_data
                return
            user_spam_tracker[user_id] = user_spam_data
        else:
            user_spam_data["count"] = 0
            user_spam_tracker[user_id] = user_spam_data

    # ---------- SMART & SAFE STICKER HANDLING ----------
    if update.message.sticker and not update.message.text:
        if random.random() < 0.7:
            try:
                chosen_pack_name = random.choice(SAFE_STICKER_PACKS)
                sticker_set = await context.bot.get_sticker_set(chosen_pack_name)
                if sticker_set and sticker_set.stickers:
                    random_sticker = random.choice(sticker_set.stickers)
                    await update.message.reply_sticker(random_sticker.file_id)
                    return
            except Exception as e:
                logger.error(f"Safe sticker pack fetch nahi ho paya: {e}")
        
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        sticker_prompt = "User ne ek sticker bheja hai, is par mazedar Hinglish reaction do."
        reply = await get_ai_reply(sticker_prompt, get_history(user.id))
        update_history(user.id, sticker_prompt, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        await update.message.reply_text(f"{user_mention} {reply}")
        return

    if not update.message.text:
        return

    # ---------- BIO LINK DETECTION with 3-WARNING LIMIT ----------
    try:
        full_user = await context.bot.get_chat(user_id)
        bio = full_user.bio if full_user.bio else ""

        if has_telegram_link(bio):
            is_admin = False
            try:
                member = await context.bot.get_chat_member(chat.id, user_id)
                if member.status in ["administrator", "creator"]:
                    is_admin = True
                    logger.info(f"Admin {user_id} has link in bio, warning skipped.")
            except Exception as e:
                logger.warning(f"Could not get chat member status: {e}")

            if not is_admin:
                count = user_warning_count.get(user_id, 0)
                if count < 3:
                    warning_msg = (
                        f"🥺 **Baby, please remove the Telegram link from your bio!**\n"
                        f"🚫 **Promotion is not allowed here.**\n\n"
                        f"👮 @admin – this baby has a link in their bio. If it's okay with you, then no problem, but please check! 🙏"
                    )
                    await update.message.reply_text(warning_msg, parse_mode="Markdown")
                    user_warning_count[user_id] = count + 1
                    logger.info(f"User {user_id} warned {count+1}/3 times.")
                    return
                else:
                    logger.info(f"User {user_id} already warned 3 times, giving normal reply now.")
    except Exception as e:
        logger.warning(f"Could not fetch bio for {user_id}: {e}")

    # ---------- 3 CASES WHERE BOT REPLIES ----------
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

    # Case 1: Standalone Random Message (Tag the user)
    if is_standalone:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(message_text, get_history(user_id))
        update_history(user_id, message_text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        await update.message.reply_text(f"{user_mention} {reply}")
        return

    # Case 2: Slide Reply to Bot (NO TAG)
    is_reply_to_bot = False
    if update.message.reply_to_message:
        original_sender = update.message.reply_to_message.from_user
        if original_sender and original_sender.is_bot and original_sender.username == bot_username:
            is_reply_to_bot = True

    if is_reply_to_bot:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(message_text, get_history(user_id))
        update_history(user_id, message_text, reply)
        await update.message.reply_text(reply)
        return

    # Case 3: Bot Mentioned via @ (NO TAG)
    is_bot_mentioned = False
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                mentioned_text = message_text[entity.offset:entity.offset + entity.length]
                if mentioned_text.lower() == f"@{bot_username.lower()}":
                    is_bot_mentioned = True
                    break
            elif entity.type == "text_mention":
                if entity.user and entity.user.username == bot_username:
                    is_bot_mentioned = True
                    break

    if is_bot_mentioned:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(message_text, get_history(user_id))
        update_history(user_id, message_text, reply)
        await update.message.reply_text(reply)
        return

    logger.debug(f"Ignored message: {message_text[:50]}")

# ---------- Global Error Handler ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    # Agar Telegram rate limit (429) ka error aaye, toh thodi der ruko
    if isinstance(error, RetryAfter):
        logger.warning(f"Telegram Rate limit hit. Sleeping for {error.retry_after} seconds.")
        await asyncio.sleep(error.retry_after)
    elif isinstance(error, TimedOut):
        logger.warning("Telegram request timed out, ignoring...")
    else:
        logger.error("Exception while handling an update:", exc_info=error)

# ---------- MAIN ----------
async def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler((filters.TEXT | filters.Sticker.ALL) & ~filters.COMMAND, handle_message))
    
    # Register the global error handler
    application.add_error_handler(error_handler)

    port = int(os.environ.get("PORT", 8000))
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")

    if webhook_url:
        logger.info(f"Starting in WEBHOOK mode -> {webhook_url}/webhook")

        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.requests import Request
        from starlette.routing import Route
        import uvicorn

        async def health(request: Request) -> PlainTextResponse:
            return PlainTextResponse("Bot is alive!")

        async def telegram_webhook(request: Request) -> PlainTextResponse:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.update_queue.put(update)
            return PlainTextResponse("OK")

        starlette_app = Starlette(routes=[
            Route("/", health, methods=["GET"]),
            Route("/webhook", telegram_webhook, methods=["POST"]),
        ])

        await application.initialize()
        await application.start()
        await application.bot.set_webhook(url=f"{webhook_url}/webhook")

        server = uvicorn.Server(
            uvicorn.Config(app=starlette_app, host="0.0.0.0", port=port, log_level="info")
        )
        await server.serve()
    else:
        logger.info("Starting in POLLING mode (local/dev)")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
