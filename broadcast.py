import os
import time
import asyncio
import logging
import psycopg2
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import Forbidden, BadRequest, RetryAfter

logger = logging.getLogger(__name__)

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# Telegram flood limit se bachne ke liye har msg ke beech chhota gap
SEND_DELAY = 0.05  # ~20 messages/sec safe rate


def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL)


def get_all_broadcast_users() -> list:
    """broadcast_users table se sab user_id return karta hai."""
    if not DATABASE_URL:
        return []
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT user_id FROM broadcast_users")
        rows = c.fetchall()
        c.close()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.error(f"broadcast users fetch error: {e}")
        return []


def remove_broadcast_user(user_id: int):
    """Jo user bot ko block/delete kar chuka hai, use list se hata do."""
    if not DATABASE_URL:
        return
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("DELETE FROM broadcast_users WHERE user_id=%s", (user_id,))
        conn.commit()
        c.close()
        conn.close()
    except Exception:
        pass


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage: /broadcast <message>
    (reply karke bhi use kar sakte ho: kisi msg pe reply karke /broadcast likho,
    us reply wale msg ka content bhej dega — text/photo/video sab support karega)
    Sirf OWNER hi use kar sakta hai.
    """
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        await update.message.reply_text("🚫 Ye command sirf owner use kar sakta hai.")
        return

    # Message ya to command ke argument se lo, ya reply se
    broadcast_msg = None
    source_message = None

    if update.message.reply_to_message:
        source_message = update.message.reply_to_message
    else:
        text_after_command = update.message.text.split(" ", 1)
        if len(text_after_command) > 1 and text_after_command[1].strip():
            broadcast_msg = text_after_command[1].strip()

    if not broadcast_msg and not source_message:
        await update.message.reply_text(
            "⚠️ Kuch bhej ne ke liye message do.\n\n"
            "Use: `/broadcast <tumhara message>`\n"
            "Ya: kisi msg (text/photo/video) pe reply karke `/broadcast` likho.",
            parse_mode="Markdown"
        )
        return

    users = get_all_broadcast_users()
    total = len(users)
    if total == 0:
        await update.message.reply_text("⚠️ Abhi tak koi bhi user /start nahi kar chuka.")
        return

    status_msg = await update.message.reply_text(
        f"📡 Broadcast shuru ho raha hai...\n👥 Total users: {total}"
    )

    success = 0
    failed = 0
    blocked = 0

    for uid in users:
        try:
            if source_message:
                await source_message.copy(chat_id=uid)
            else:
                await context.bot.send_message(chat_id=uid, text=broadcast_msg)
            success += 1
        except Forbidden:
            # user ne bot ko block kar diya ya delete kar diya
            blocked += 1
            remove_broadcast_user(uid)
        except BadRequest:
            failed += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                if source_message:
                    await source_message.copy(chat_id=uid)
                else:
                    await context.bot.send_message(chat_id=uid, text=broadcast_msg)
                success += 1
            except Exception:
                failed += 1
        except Exception as e:
            logger.warning(f"broadcast fail for {uid}: {e}")
            failed += 1

        await asyncio.sleep(SEND_DELAY)

    await status_msg.edit_text(
        f"✅ Broadcast complete!\n\n"
        f"👥 Total: {total}\n"
        f"✅ Success: {success}\n"
        f"🚫 Blocked/Deleted: {blocked}\n"
        f"❌ Failed: {failed}"
    )
