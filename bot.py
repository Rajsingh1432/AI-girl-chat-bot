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

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

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

user_warning_count = {}

# ---------- ANTI-FLOOD PROTECTION ----------
user_flood_data = {}
FLOOD_WINDOW = 4
FLOOD_THRESHOLD = 6
FLOOD_COOLDOWN = 120
LAST_CLEANUP = 0.0


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
MAX_HISTORY_MESSAGES = 20

SAFE_STICKER_PACKS = ["Sigma", "Cats", "Monkeys", "Peach", "Animals",
                      "HonestStickers", "cute", "Memenny", "Dobby"]

# NAYA RESIZED IMAGE LINK (1280px - HD Quality, Fast Loading)
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
            f"ɪ'ʟʟ  ᴛᴏ *ᴇᴠᴇʀʏ ᴍᴇssᴀɢᴇ* ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ\\! 😉\n\n"
            f"⚡ ᴘᴏᴡᴇʀᴇᴅ ʙʏ *Rᴀᴊ Aɪ* – ʟɪɢʜᴛɴɪɴɢ ғᴀsᴛ & ᴄᴏᴏʟ\\!\n"
            f"❤️ ᴅᴇᴠᴇʟᴏᴘᴇ ʙʏ ᴏᴜʀ ᴏᴡɴᴇʀ [@its\\_raj\\_king](https://t.me/its_raj_king)\n\n"
            f"👇 ᴛᴀᴘ ᴀ ʙᴇʟʟᴏᴡ ʙᴜᴛᴛᴏɴ ᴀɴᴅ ᴜsᴇ ᴍᴇ \\!"
        )

        keyboard = [
            [InlineKeyboardButton("𖤍 ᴀᴅᴅ ᴍᴇ ʙᴀʙʏ 𖤍",
                                  url=f"https://t.me/{bot_username}?startgroup=start")],
            [InlineKeyboardButton("👨‍💻 ʙᴏᴛ ᴅᴇᴠᴇʟᴏᴘᴇʀ ༄",
                                  url="https://t.me/its_raj_king")],
            [InlineKeyboardButton("🌿 sᴜᴘᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ✍︎",
                                  url="https://t.me/KnowRajpapa")],
            [InlineKeyboardButton("☞︎︎︎ sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ☜︎︎︎",
                                  url="https://t.me/+WJneJ6gRAqg2ZTI1")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Direct URL se bhej rahe hain (No Render Timeout, No Fattti Hui Image)
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
                    model="llama-3.3-70b-versatile",
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
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    indices = list(range(len(clients)))
    random.shuffle(indices)
    last_error = None
    for i in indices:
        try:
            response = clients[i].chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages, temperature=0.9,
                max_tokens=120, top_p=0.95)
            return response.choices[0].message.content
        except Exception as e:
            last_error = e
            if "429" in str(e).lower() or "rate_limit" in str(e).lower():
                logger.warning(f"Server {i+1} rate limited, next...")
                continue
            else:
                logger.error(f"AI Error Server {i+1}: {e}")
                break
    if last_error and ("429" in str(last_error) or "rate_limit" in str(last_error).lower()):
        return "Arre yaar, meri saari chat limits full ho gayi hain abhi! 😭 1 minute ruk jao!"
    return "Are, meri neend khul gayi! 😴 thoda sa gadbad ho gaya, fir se bolo na!"


def get_history(user_id: int) -> list:
    return conversation_memory.get(user_id, [])


def update_history(user_id: int, user_message: str, bot_reply: str) -> None:
    history = conversation_memory.setdefault(user_id, [])
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": bot_reply})
    if len(history) > MAX_HISTORY_MESSAGES:
        conversation_memory[user_id] = history[-MAX_HISTORY_MESSAGES:]


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

    # ========== ANTI-FLOOD (SABSE PELE) ==========
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
            await context.bot.send_chat_action(chat_id=chat.id, action="typing")
            sticker_prompt = "User ne ek sticker bheja hai, is par mazedar Hinglish reaction do."
            reply = await get_ai_reply(sticker_prompt, get_history(user.id))
            update_history(user.id, sticker_prompt, reply)
            user_mention = f"@{user.username}" if user.username else user.first_name
            await safe_reply_text(update, f"{user_mention} {reply}")
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
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(message_text, get_history(user_id))
        update_history(user_id, message_text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        await safe_reply_text(update, f"{user_mention} {reply}")
        return

    is_reply_to_bot = False
    if update.message.reply_to_message:
        orig = update.message.reply_to_message.from_user
        if orig and orig.is_bot and orig.username == bot_username:
            is_reply_to_bot = True

    if is_reply_to_bot:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(message_text, get_history(user_id))
        update_history(user_id, message_text, reply)
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
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(message_text, get_history(user_id))
        update_history(user_id, message_text, reply)
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
