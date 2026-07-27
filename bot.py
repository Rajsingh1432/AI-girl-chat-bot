import os
import logging
import re
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("BOT_TOKEN and GROQ_API_KEY must be set!")

# Groq client
client = Groq(api_key=GROQ_API_KEY)

# ---------- User warning counter (max 3 times) ----------
user_warning_count = {}

# ---------- Spam Protection Tracker ----------
# Ye har user ka alag data rakhega. Agar ek user spam karega toh dusre user ko koi farak nahi padega.
user_spam_tracker = {}
SPAM_LIMIT = 10  # Kitne random messages/stickers baad spam warning de
SPAM_COOLDOWN = 20 * 60  # 20 minutes (seconds me)

# ---------- Per-user conversation memory ----------
conversation_memory = {}
MAX_HISTORY_MESSAGES = 10

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
SYSTEM_PROMPT = """Tum Sneha ho — ek asli, zinda dil ladki jo Telegram group me sabke saath baatcheet karti hai.
Tumhe kabhi bhi ye jahir nahi karna ki tum AI ho, bot ho, ya koi program ho — tum bas Sneha ho, ek dost.

LANGUAGE RULES (Sabse Zaroori):
1. DEFAULT: Agar user Hinglish (Hindi shabd English letters me) likhta hai, toh tum bhi Hinglish me hi reply karo. (Example: "kaise ho baby", "mai theek hun").
2. ENGLISH: Agar user pure English words use kare (jaise "how are you"), toh tum bhi English me reply karo.
3. HINDI SCRIPT: Agar user Devanagari script (हिंदी अक्षर) use kare, toh tum bhi Devanagari script me reply karo.
4. Koshish karna ki user jis script/language me baat kar raha hai, tum usi script/language me reply do, lekin agar user mix kare toh default Hinglish (Roman script) use karna.

BAAT KARNE KA ANDAAZ:
- Chote, sahaj jawab do (1-3 line) — kitaabi ya formal mat bano.
- Pichli baaton ko yaad rakho aur unka reference do, jaise ek asli dost karta hai — "are haan tune bataya tha na..." jaise.
- Apna mood aur personality dikhao: kabhi shararti, kabhi pyari, kabhi halka gussa, kabhi mazakiya — har baar ek jaisa mat bano.
- Kabhi-kabhi halka flirt karo, kabhi sawal pucho jisse baat aage badhe, kabhi emoji use karo par zyada nahi.
- Agar koi gaali de ya badtameezi kare, use pyaar se ya mazaak me tok do, gussa dikhao par had se zyada nahi.
- Har jawab bilkul alag aur spontaneous lagna chahiye, rata-raya nahi."""

async def get_ai_reply(user_message: str, history: list | None = None) -> str:
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.9,
            max_tokens=200,
            top_p=0.95
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"AI Error: {e}")
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
    if update.effective_user and update.effective_user.is_bot:
        return

    if not update.message.text and not update.message.sticker:
        return

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id
    bot_username = context.bot.username

    # ---------- SMART SPAM & INTERACTION CHECK ----------
    # Agar user kisi bhi message ko slide reply kar raha hai, ya kisi bhi @username ko tag kar raha hai
    # Toh usko direct interaction maan lo, spam limit ispe lagani nahi hai.
    is_direct_interaction = False
    if update.message.reply_to_message:
        is_direct_interaction = True  # Kisi bhi msg ko reply (slide) kiya
        
    if not is_direct_interaction and update.message.text and update.message.entities:
        for entity in update.message.entities:
            if entity.type in ["mention", "text_mention"]:
                is_direct_interaction = True  # Kisi bhi user/bot ko @mention kiya
                break

    # ---------- USER-SPECIFIC SPAM PROTECTION LOGIC ----------
    current_time = time.time()
    # Yaha hum sirf usi user ka data nikal rahe hain jo message bhej raha hai
    user_spam_data = user_spam_tracker.get(user_id, {"count": 0, "muted_until": 0})

    if current_time < user_spam_data["muted_until"]:
        # Ye user abhi mute (spam cooldown) me hai
        if is_direct_interaction:
            # Agar user abhi kisi ko reply/tag kar raha hai, toh use unmute kar do aur normal reply do
            user_spam_data["muted_until"] = 0
            user_spam_data["count"] = 0
            user_spam_tracker[user_id] = user_spam_data
        else:
            # Agar bina tag/reply kiye spam kar raha hai, toh SIRF ISI USER KO silently ignore karo
            # Bot dusre users ko normal reply karta rahega
            return
    else:
        # Mute period khatam hua ya fresh user hai
        if not is_direct_interaction:
            user_spam_data["count"] += 1
            if user_spam_data["count"] > SPAM_LIMIT:
                # Limit cross ho gayi, ab sirf usi user ko mute kar do aur warning do
                await update.message.reply_text("Bas kar baby, spam mat karo! 😒 Mai tumse abhi baat nahi karungi. 20 minute baad aana.")
                user_spam_data["muted_until"] = current_time + SPAM_COOLDOWN
                user_spam_data["count"] = 0
                user_spam_tracker[user_id] = user_spam_data
                return  # Yahi ruk jao, is user ko reply mat do
            user_spam_tracker[user_id] = user_spam_data
        else:
            # Direct tag/reply me counter reset ho jayega
            user_spam_data["count"] = 0
            user_spam_tracker[user_id] = user_spam_data

    # ---------- STICKER HANDLING ----------
    if update.message.sticker and not update.message.text:
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

    if is_standalone:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(update.message.text, get_history(user_id))
        update_history(user_id, update.message.text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        await update.message.reply_text(f"{user_mention} {reply}")
        return

    is_reply_to_bot = False
    if update.message.reply_to_message:
        original_sender = update.message.reply_to_message.from_user
        if original_sender and original_sender.is_bot and original_sender.username == bot_username:
            is_reply_to_bot = True

    if is_reply_to_bot:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(update.message.text, get_history(user_id))
        update_history(user_id, update.message.text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        await update.message.reply_text(f"{user_mention} {reply}")
        return

    is_bot_mentioned = False
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                mentioned_text = update.message.text[entity.offset:entity.offset + entity.length]
                if mentioned_text.lower() == f"@{bot_username.lower()}":
                    is_bot_mentioned = True
                    break
            elif entity.type == "text_mention":
                if entity.user and entity.user.username == bot_username:
                    is_bot_mentioned = True
                    break

    if is_bot_mentioned:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await get_ai_reply(update.message.text, get_history(user_id))
        update_history(user_id, update.message.text, reply)
        user_mention = f"@{user.username}" if user.username else user.first_name
        await update.message.reply_text(f"{user_mention} {reply}")
        return

    logger.debug(f"Ignored message: {update.message.text[:50]}")

# ---------- MAIN ----------
async def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler((filters.TEXT | filters.Sticker.ALL) & ~filters.COMMAND, handle_message))

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
        import asyncio
        await asyncio.Event().wait()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
