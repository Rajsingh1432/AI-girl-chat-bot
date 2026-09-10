import os
import random
import re
import html
import asyncio
import time
import logging
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
logger = logging.getLogger(__name__)

# ⭐ ========== PREMIUM EMOJI + BUTTON STYLING ==========
try:
    from config import PREMIUM_EMOJIS, ButtonStyle
except ImportError:
    class ButtonStyle:
        PRIMARY = "primary"
        DANGER = "danger"
    PREMIUM_EMOJIS = {
        "kidnap": "6001154049452283936",
        "fire": "5064709487953183440",       # 🔥
        "trophy": "4999002445444023072",      # 🏆
        "heart": "5064672027248427816",       # ❤️
        "sparkle": "5247087285538672245",     # ✨
        "gem": "5249320921935663770"          # 💎
    }

# Shortcuts for HTML tags
FIRE = f'<tg-emoji emoji-id="{PREMIUM_EMOJIS["fire"]}">🔥</tg-emoji>'
TROPHY = f'<tg-emoji emoji-id="{PREMIUM_EMOJIS["trophy"]}">🏆</tg-emoji>'
HEART = f'<tg-emoji emoji-id="{PREMIUM_EMOJIS["heart"]}">❤️</tg-emoji>'
SPARKLE = f'<tg-emoji emoji-id="{PREMIUM_EMOJIS["sparkle"]}">✨</tg-emoji>'
GEM = f'<tg-emoji emoji-id="{PREMIUM_EMOJIS["gem"]}">💎</tg-emoji>'

# ⭐ ========== AI SETUP FOR GAME ==========
_gkeys = [os.getenv(f"GROQ_API_KEY_{i}") for i in range(1, 101)]
_gkeys = [k for k in _gkeys if k]
_game_client = AsyncGroq(api_key=_gkeys[0]) if _gkeys else None

FALLBACK_QUESTIONS = [
    {"q": "Agar main tumhe mall me akeli dekhu, toh tum kya karoge?", "opts": ["seedha propose maar du", "chupke se dekhta rahu", "dost bana ke line maru", "ignore karke nikal du"], "best": 0},
    {"q": "Mujhse pehli baar baat karke tumhara kya reaction tha?", "opts": ["mast ladki hai", "thodi pagal lag rahi thi", "boring hai", "aawaz sunkar dil ho gaya"], "best": 3}
]

active_games = {} # Buzzer state per group (chat_id)

# ⭐ ========== DB FUNCTIONS ==========
def get_db_conn():
    if not DATABASE_URL: return None
    return psycopg2.connect(DATABASE_URL)

def add_points_to_db(user_id, points, group_id, user_name):
    if not DATABASE_URL: return
    try:
        conn = get_db_conn()
        c = conn.cursor()
        
        # ⭐ SAFETY CHECK: Table exist nahi kar rha toh khud bana do
        c.execute("""CREATE TABLE IF NOT EXISTS group_game_points (
                        group_id BIGINT, 
                        user_id BIGINT, 
                        points INTEGER DEFAULT 0,
                        user_name TEXT DEFAULT 'Anonymous',
                        PRIMARY KEY(group_id, user_id)
                    )""")
        
        # 1. Global Points Update
        c.execute("INSERT INTO user_memory (user_id, game_points) VALUES (%s, %s) "
                  "ON CONFLICT (user_id) DO UPDATE SET game_points = GREATEST(0, COALESCE(user_memory.game_points, 0) + %s)",
                  (user_id, points, points))
                  
        # 2. Group Specific Points Update (FIXED PARAMETERS)
        c.execute("""INSERT INTO group_game_points (group_id, user_id, points, user_name) 
                     VALUES (%s, %s, %s, %s) 
                     ON CONFLICT (group_id, user_id) 
                     DO UPDATE SET points = GREATEST(0, group_game_points.points + %s), 
                                   user_name = EXCLUDED.user_name""",
                  (group_id, user_id, points, user_name, points)) # ⭐ Yahan se extra 'user_name' hata diya gaya hai
        conn.commit()
        c.close(); conn.close()
    except Exception as e:
        logger.error(f"DB Error add_points: {e}")

def get_top_3_group_players(group_id):
    if not DATABASE_URL: return []
    try:
        conn = get_db_conn()
        c = conn.cursor()
        
        c.execute("""CREATE TABLE IF NOT EXISTS group_game_points (
                        group_id BIGINT, 
                        user_id BIGINT, 
                        points INTEGER DEFAULT 0,
                        user_name TEXT DEFAULT 'Anonymous',
                        PRIMARY KEY(group_id, user_id)
                    )""")
        
        # ⭐ Ab hume user_memory table join karne ki zarurat nahi, direct group_game_points se naam lunga
        c.execute("""SELECT user_id, user_name, points 
                     FROM group_game_points 
                     WHERE group_id = %s AND points > 0
                     ORDER BY points DESC LIMIT 3""", (group_id,))
        rows = c.fetchall()
        c.close(); conn.close()
        results = []
        for uid, name, pts in rows:
            if not name:
                name = "Anonymous"
            results.append((uid, name, pts))
        return results
    except Exception as e:
        logger.error(f"DB Error get_top_3: {e}")
        return []

# ⭐ ========== WELCOME KEYBOARD HELPER ==========
def get_welcome_game_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Play Game", callback_data="g_guide", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])]
    ])

# ⭐ ========== BULLETPROOF AI QUESTION GENERATOR ==========
async def generate_ai_question():
    if not _game_client:
        logger.error("❌ GAME AI ERROR: Game client init nahi hua! API Keys check karo.")
        return random.choice(FALLBACK_QUESTIONS)
        
    system_prompt = "You output strictly in the requested format. No markdown, no extra text, no thinking tags. Output strictly in Hinglish (Roman Hindi)."
    user_prompt = """Tu ek flirty game bot hai (Sneha). Ek fun, casual scenario banao jahan Sneha user se puch rahi hai ki wo kya karega.
Strictly HINGLISH me likho. 
Options ekdum chote, casual aur realistic rakho (jaise 'seedha propose kar du', 'chupke se dekhta rahu', 'ignore karke nikal du'). Koi formal English ya bada paragraph nahi.
Is format me reply karo, no extra text:
Q: <1 line ka scenario>
A) <Option A>
B) <Option B>
C) <Option C>
D) <Option D>
BEST: <A/B/C/D>"""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    for attempt in range(3):
        try:
            logger.info(f"🔄 Game AI: Attempt {attempt+1} to generate question...")
            response = await asyncio.wait_for(
                _game_client.chat.completions.create(
                    model="openai/gpt-oss-20b",
                    messages=messages,
                    temperature=0.9,
                    max_tokens=400
                ),
                timeout=15.0 
            )
            text = response.choices[0].message.content.strip()
            logger.info(f"✅ Game AI Raw Response: {text}")
            
            # 1. Think Tags & Markdown Hatao
            text = re.sub(r"<think[\s\S]*?<\/think>", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"<think[\s\S]*", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text).strip()
            text = re.sub(r"\n?```$", "", text).strip()
            
            # 2. REGEX SEARCH
            q_match = re.search(r"\**\s*Q\s*\**\s*[:\-]\s*(.*?)(?=\**\s*A\s*\**\s*[\)\.])", text, re.DOTALL | re.IGNORECASE)
            a_match = re.search(r"\**\s*A\s*\**\s*[\)\.]\s*(.*?)(?=\**\s*B\s*\**\s*[\)\.])", text, re.DOTALL | re.IGNORECASE)
            b_match = re.search(r"\**\s*B\s*\**\s*[\)\.]\s*(.*?)(?=\**\s*C\s*\**\s*[\)\.])", text, re.DOTALL | re.IGNORECASE)
            c_match = re.search(r"\**\s*C\s*\**\s*[\)\.]\s*(.*?)(?=\**\s*D\s*\**\s*[\)\.])", text, re.DOTALL | re.IGNORECASE)
            d_match = re.search(r"\**\s*D\s*\**\s*[\)\.]\s*(.*?)(?=\**\s*BEST\s*\**\s*[:\-])", text, re.DOTALL | re.IGNORECASE)
            best_match = re.search(r"\**\s*BEST\s*\**\s*[:\-]\s*([A-D])", text, re.IGNORECASE)
            
            if q_match and a_match and b_match and c_match and d_match and best_match:
                q = q_match.group(1).strip()
                opts = [
                    a_match.group(1).strip(),
                    b_match.group(1).strip(),
                    c_match.group(1).strip(),
                    d_match.group(1).strip()
                ]
                best_letter = best_match.group(1).upper()
                best_idx = ["A", "B", "C", "D"].index(best_letter)
                
                logger.info("✅ Game AI: Question Parsed Successfully!")
                return {"q": q, "opts": opts, "best": best_idx}
            else:
                logger.warning(f"⚠️ Game AI: Parse Fail! Q={bool(q_match)}, A={bool(a_match)}, B={bool(b_match)}, C={bool(c_match)}, D={bool(d_match)}, Best={bool(best_match)}")
                continue
                
        except Exception as e:
            logger.error(f"❌ Game AI Exception: {e}")
            continue
            
    logger.warning("⚠️ Game AI: All attempts failed, using Fallback.")
    return random.choice(FALLBACK_QUESTIONS)

# ⭐ ========== GAME UI & LOGIC ==========
async def games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        bot_username = context.bot.username
        keyboard = [
            [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
        ]
        text = f"🔴 <b>DM me game nahi chalta!</b>\n\nMujhe kisi group me add karo aur wahan <code>/play</code> type karo! {FIRE}"
        if update.message:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    bot_username = context.bot.username
    keyboard = [
        [InlineKeyboardButton("Start Group Game", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Top 3 Champions", callback_data="g_top", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["trophy"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    text = (
        f"{FIRE} <b>Sneha's Buzzer Game</b> {FIRE}\n\n"
        f"Kya tum mere dil ke aas paas bhi ho? {SPARKLE}\n"
        "5 sawaal honge, sab ek sath khelenge!\n"
        "Pehle sahi jawab wala +10 pts payega. Galat pe -2 pts katenge. ⚡\n\n"
        f"Apna score badhao aur is group ke <b>Top 3</b> me apna naam dekho! {TROPHY}"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await update.message.reply_text("🔴 Leaderboard sirf groups me dekha ja sakta hai! Group me /leaderboard likho.")
        return
        
    chat_id = update.effective_chat.id
    top_players = get_top_3_group_players(chat_id)
    if not top_players:
        await update.message.reply_text(f"{TROPHY} <b>Leaderboard</b>\n\nAbhi koi khela nahi hai! Start playing to be #1.", parse_mode="HTML")
        return
        
    text = f"{TROPHY} <b>Top 3 Champions</b> {TROPHY}\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, name, pts) in enumerate(top_players):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        safe_name = html.escape(name)
        text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> <b>| {pts} {HEART}</b>\n"
        
    bot_username = context.bot.username
    keyboard = [
        [InlineKeyboardButton("Start Group Game", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat.id
    user = query.from_user
    bot_username = context.bot.username
    
    main_menu_keyboard = [
        [InlineKeyboardButton("Start Group Game", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Top 3 Champions", callback_data="g_top", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["trophy"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    
    # Cleanup old games
    current_time = time.time()
    for cid in list(active_games.keys()):
        if current_time - active_games[cid].get("last_active", 0) > 300:
            active_games.pop(cid, None)
            
    if update.effective_chat.type == "private":
        keyboard = [
            [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
        ]
        text = f"🔴 <b>Game sirf groups me khel sakte ho!</b>\n\nMujhe kisi chat group me add karo aur wahan <code>/play</code> type karo ya /start wala button dabao! {FIRE}"
        await query.answer()
        try:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except:
            pass
        return
    
    if data == "g_guide":
        await games_menu(update, context)
        return
        
    if data == "g_start":
        if chat_id in active_games:
            await query.answer("Game already chal raha hai group me! Join karo. 🔥", show_alert=True)
            return
            
        active_games[chat_id] = {"q_idx": 0, "locked": False, "msg_id": query.message.message_id, "last_active": current_time}
        await ask_question(context, chat_id)
        return
        
    elif data.startswith("g_ans_"):
        if chat_id not in active_games:
            await query.answer("Game start nahi hua! /play type karo.", show_alert=True)
            return
            
        game = active_games[chat_id]
        game["last_active"] = current_time
        
        # ⭐ BUZZER LOCK CHECK
        if game.get("locked", False):
            await query.answer("Bhai answer ho gaya! Next sawaal ka wait karo. 😒", show_alert=False)
            return
            
        parts = data.split("_")
        opt_idx = int(parts[2])
        current_q = game["current_q"]
        
        if opt_idx == current_q["best"]:
            # ✅ SAHI JAWAB
            game["locked"] = True
            # ⭐ User ka asli naam save kar rahe hain DB me
            add_points_to_db(user.id, 10, chat_id, user.first_name)
            
            await query.answer("✅ Sahi Jawab! +10 Points", show_alert=False)
            
            win_text = f"🎉 <b>{html.escape(user.first_name)}</b> ne pehle sahi jawab diya!\n\n<b>{current_q['q']}</b>\n✅ <b>Answer:</b> {current_q['opts'][current_q['best']]}\n\n<i>Next sawaal a raha hai...</i>"
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=game['msg_id'], text=win_text, parse_mode="HTML")
            except Exception: pass
            
            game["q_idx"] += 1
            
            if game["q_idx"] < 5:
                await asyncio.sleep(2)
                await ask_question(context, chat_id)
            else:
                await end_game_winner(context, chat_id)
                active_games.pop(chat_id, None)
                
        else:
            # ❌ GALAT JAWAB
            # ⭐ User ka asli naam save kar rahe hain DB me
            add_points_to_db(user.id, -2, chat_id, user.first_name)
            await query.answer("❌ Galat! -2 Points kat gaye.", show_alert=False)
            
    elif data == "g_top":
        await query.answer()
        top_players = get_top_3_group_players(chat_id)
        if not top_players:
            text = f"{TROPHY} <b>Leaderboard</b> {TROPHY}\n\nAbhi koi khela nahi hai! Start playing to be #1."
        else:
            text = f"{TROPHY} <b>Top 3 Champions</b> {TROPHY}\n\n"
            medals = ["🥇", "🥈", "🥉"]
            for i, (uid, name, pts) in enumerate(top_players):
                medal = medals[i] if i < len(medals) else f"{i+1}."
                safe_name = html.escape(name)
                text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> <b>| {pts} {HEART}</b>\n"
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(main_menu_keyboard), parse_mode="HTML")
        except Exception: pass

async def ask_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games[chat_id]
    q_idx = game["q_idx"]
    
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=game['msg_id'], text=f"<i>Sneha soch rahi hai sawaal {q_idx + 1}/5... {SPARKLE}</i>", parse_mode="HTML")
    except Exception: pass
    
    question = await generate_ai_question()
    game["current_q"] = question
    game["locked"] = False
    
    opts_with_idx = list(enumerate(question["opts"]))
    random.shuffle(opts_with_idx)
    
    keyboard = []
    for idx, text in opts_with_idx:
        if idx % 2 == 0:
            btn = InlineKeyboardButton(text, callback_data=f"g_ans_{idx}", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["gem"])
        else:
            btn = InlineKeyboardButton(text, callback_data=f"g_ans_{idx}", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["sparkle"])
        keyboard.append([btn])
    
    final_text = f"<b>Sawaal {q_idx + 1}/5</b> {FIRE}\n\n{question['q']}\n\n<i>Pehle sahi jawab wala +10 pts payega! Galat pe -2 pts katenge. ⚡</i>"
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=game['msg_id'], text=final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception:
        msg = await context.bot.send_message(chat_id=chat_id, text=final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        game['msg_id'] = msg.message_id
        
    asyncio.create_task(timeout_question(context, chat_id))

async def timeout_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(15)
    
    game = active_games.get(chat_id)
    if game and not game.get("locked", False):
        game["locked"] = True
        current_q = game["current_q"]
        
        win_text = f"⏳ <b>Time Up!</b> Kisi ne sahi jawab nahi diya.\n\n<b>{current_q['q']}</b>\n✅ <b>Answer:</b> {current_q['opts'][current_q['best']]}\n\n<i>Next sawaal a raha hai...</i>"
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=game['msg_id'], text=win_text, parse_mode="HTML")
        except Exception: pass
        
        game["q_idx"] += 1
        
        if game["q_idx"] < 5:
            await asyncio.sleep(2)
            await ask_question(context, chat_id)
        else:
            await end_game_winner(context, chat_id)
            active_games.pop(chat_id, None)

async def end_game_winner(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    top_players = get_top_3_group_players(chat_id)
    top_text = f"{TROPHY} <b>Is Group ke Top 3 Champions</b> {TROPHY}\n\n"
    if not top_players:
        top_text += "Abhi koi khela nahi hai!\n"
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, name, pts) in enumerate(top_players):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            safe_name = html.escape(name)
            top_text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> <b>| {pts} {HEART}</b>\n"
            
    final_text = f"<b>Game Khatam!</b> {FIRE}\n\n{top_text}"
    
    bot_username = context.bot.username
    main_menu_keyboard = [
        [InlineKeyboardButton("Start Group Game", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=game['msg_id'], text=final_text, reply_markup=InlineKeyboardMarkup(main_menu_keyboard), parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=final_text, reply_markup=InlineKeyboardMarkup(main_menu_keyboard), parse_mode="HTML")

async def newgame_command(update, context):
    await games_menu(update, context)
