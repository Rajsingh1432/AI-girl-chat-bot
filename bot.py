import os
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from groq import Groq
from dotenv import load_dotenv

# .env फ़ाइल से वेरिएबल्स लोड (लोकल टेस्ट के लिए)
load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables से लें
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("BOT_TOKEN और GROQ_API_KEY सेट करना अनिवार्य है!")

# Groq क्लाइंट
client = Groq(api_key=GROQ_API_KEY)

async def get_ai_reply(user_message: str) -> str:
    """Groq के फ्री मॉडल से इंसानी जवाब लें"""
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",  # शक्तिशाली और फ्री
            messages=[
                {"role": "system", "content": """तुम एक मस्ती भरी, शरारती और प्यारी सी लड़की हो (जैसे Vick)।
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """हर मैसेज का AI से जवाब दें (बिना टैग के)"""
    if update.effective_user and update.effective_user.is_bot:
        return

    user_msg = update.message.text
    if not user_msg:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    reply = await get_ai_reply(user_msg)
    await update.message.reply_text(reply)

async def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Render.com Webhook सेटअप
    port = int(os.environ.get("PORT", 8000))
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
    
    if webhook_url:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        
        async def health(request):
            return JSONResponse({"status": "ok"})
        
        async def webhook(request):
            data = await request.json()
            await application.update_queue.put(Update.de_json(data, application.bot))
            return JSONResponse({"status": "ok"})

        app = Starlette(routes=[
            Route("/healthcheck", health),
            Route("/webhook", webhook, methods=["POST"]),
        ])

        await application.bot.set_webhook(url=f"{webhook_url}/webhook")
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        # लोकल टेस्ट के लिए Polling
        await application.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())