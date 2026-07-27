import os
import logging
import re
import asyncio
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

# ---------- /start Command ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_name = user.first_name or "Buddy"
    bot_username = context.bot.username
    bot_name = context.bot.first_name or "AI Girl Bot"

    welcome_text = (
        f"🌟 **𝐖𝐞𝐥𝐜𝐨𝐦𝐞 𝐭𝐨 {bot_name}, {user_name}!** 🌟\n\n"
        f"💖 I'm your *fun, flirty, and friendly* chat companion bot!\n"
        f"I'll keep your Telegram group **alive & entertaining** 🎉\n\n"
        f"👉 **Just add me to your group & give me admin** –\n"
        f"   I'll reply to *every message* automatically! 😉\n\n"
        f"⚡ **Powered by Groq AI** – lightning fast & 100% free!\n"
        f"❤️ **Crafted with love** by @its_raj_king\n\n"
        f"👇 *Tap a button below to get started!*"
    )

    keyboard = [
        [
            InlineKeyboardButton("𖤍 ᴋɪᴅɴᴀᴘ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ ʙᴀʙʏ 𖤍", url=f"https://t.me/{bot_username}?startgroup=start"),
            InlineKeyboardButton("♧︎︎︎ ʙᴏᴛ ᴅᴇᴠᴇʟᴏᴘᴇʀ ♧︎︎︎", url="https://t.me/its_raj_king")
        ],
        [
            InlineKeyboardButton("➪ sᴜᴘᴘᴏʀᴛ ✍︎", url="https://t.me/+WJneJ6gRAqg2ZTI1")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    image_url = "https://i.imgur.com/8KmK7XW.jpeg"

    await update.message.reply_photo(
        photo=image_url,
        caption=welcome_text,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# ---------- AI Reply ----------
async def get_ai_reply(user_message: str) -> str:
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[
                {"role": "system", "content": """तुम एक मस्ती भरी, शरारती और प्यारी सी लड़की हो (जैसे Vick).
                - हमेशा हिंदी या हिंग्लिश में बात करो।
                - बड़ी-बड़ी किताबी बातें मत करो, बिल्कुल सहज और दोस्ताना अंदाज में बोलो।
                - छोटे और मज़ेदार जवाब दो (2-3 लाइन)।
                - कभी ऐसा लगने दो कि तुम्हें उससे प्यार हो गया है (फ्रेंडली फ्लर्ट), कभी मज़ाक उड़ाओ, कभी गुस्सा करो – बिल्कुल इंसानों की तरह। 
                - अगर कोई गाली दे या बुरा बोले, तो उसे मज़ाक में टोक दो।"""},
                {"role": "user", "content": user_message}
            ],
            temperature=0.9,
            max_tokens=200,
            top_p=0.95
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "अरे, मेरी नींद खुल गई! 😴 थोड़ा सा गड़बड़ हो गया, फिर से बोलो ना!"

# ---------- Bio Link Detection ----------
def has_telegram_link(text: str) -> bool:
    if not text:
        return False
    pattern_url = r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:[a-zA-Z0-9_]+)'
    pattern_mention = r'@[a-zA-Z0-9_]{4,}'
    return bool(re.search(pattern_url, text)) or bool(re.search(pattern_mention, text))

# ---------- Message Handler ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Ignore bot itself
    if update.effective_user and update.effective_user.is_bot:
        return

    if not update.message.text:
        return

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id
    bot_username = context.bot.username

    # ---------- BIO LINK DETECTION with 3-WARNING LIMIT ----------
    try:
        full_user = await context.bot.get_chat(user_id)
        bio = full_user.bio if full_user.bio else ""
        
        if has_telegram_link(bio):
            # Check if admin
            try:
                member = await context.bot.get_chat_member(chat.id, user_id)
                if member.status in ["administrator", "creator"]:
                    logger.info(f"Admin {user_id} has link in bio, ignored.")
                    return
            except Exception as e:
                logger.warning(f"Could not get chat member status: {e}")

            # Check warning count
            count = user_warning_count.get(user_id, 0)
            if count >= 3:
                logger.info(f"User {user_id} already warned 3 times. No more warnings.")
                return

            # Send warning
            warning_msg = (
                f"🥺 **Baby, please remove the Telegram link from your bio!**\n"
                f"🚫 **Promotion is not allowed here.**\n\n"
                f"👮 @admin – this baby has a link in their bio. If it's okay with you, then no problem, but please check! 🙏"
            )
            await update.message.reply_text(warning_msg, parse_mode="Markdown")
            user_warning_count[user_id] = count + 1
            logger.info(f"User {user_id} warned {count+1}/3 times.")
            return
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
        reply = await get_ai_reply(update.message.text)
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
        reply = await get_ai_reply(update.message.text)
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
        reply = await get_ai_reply(update.message.text)
        user_mention = f"@{user.username}" if user.username else user.first_name
        await update.message.reply_text(f"{user_mention} {reply}")
        return

    logger.debug(f"Ignored message: {update.message.text[:50]}")

# ---------- MAIN (FIXED: Proper Webhook Initialization) ----------
async def main() -> None:
    # Build Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 8000))
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")

    if webhook_url:
        # ✅ Step 1: Initialize the Application (MUST DO!)
        await application.initialize()

        # ✅ Step 2: Start the Application (so update queue processes)
        await application.start()

        # ✅ Step 3: Set Webhook
        await application.bot.set_webhook(url=f"{webhook_url}/webhook")
        logger.info(f"✅ Webhook set to {webhook_url}/webhook")

        # ✅ Step 4: Get the built-in Starlette app and run it
        webhook_app = application.webhook_app
        import uvicorn

        # ✅ Step 5: Run uvicorn (without blocking the event loop)
        server = uvicorn.Server(
            uvicorn.Config(
                app=webhook_app,
                host="0.0.0.0",
                port=port,
                log_level="info"
            )
        )
        await server.serve()
    else:
        # Local polling mode
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        # Keep running until manually stopped
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
