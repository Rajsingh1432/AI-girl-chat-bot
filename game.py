import os
import random
import re
import html
import asyncio
import time
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# ⭐ ========== PREMIUM EMOJI + BUTTON STYLING ==========
try:
    from config import PREMIUM_EMOJIS, ButtonStyle
except ImportError:
    class ButtonStyle:
        PRIMARY = "primary"
        DANGER = "danger"
    # Tumhare diye hue 5 Premium Emojis + Bot defaults
    PREMIUM_EMOJIS = {
        "kidnap": "6001154049452283936",
        "fire": "15064709487953183440",       # 🔥
        "trophy": "24999002445444023072",      # 🏆
        "heart": "35064672027248427816",       # ❤️
        "sparkle": "45247087285538672245",     # ✨
        "gem": "55249320921935663770"          # 💎
    }

# ⭐ ========== AI SETUP FOR GAME ==========
_gkeys = [os.getenv(f"GROQ_API_KEY_{i}") for i in range(1, 101)]
_gkeys = [k for k in _gkeys if k]
_game_client = AsyncGroq(api_key=_gkeys[0]) if _gkeys else None

FALLBACK_QUESTIONS = [
    {"q": "Main jab thodi sad/huti hu, toh tum kya karoge?", "opts": ["Pyaar se manaaoge", "Mazaak sunaoge", "Chhod doge", "Bologe 'ro mat'"], "best": 0},
    {"q": "Agar main tumhe 'I love you' bolu, toh reaction?", "opts": ["Gale lag jaunga", "Propose karunga wapas", "Mazaak mein taal dunga", "Block kar dunga"], "best": 0}
]

active_games = {}

# ⭐ ========== DB FUNCTIONS ==========
def get_db_conn():
    if not DATABASE_URL: return None
    return psycopg2.connect(DATABASE_URL)

def add_points_to_db(user_id, points):
    if not DATABASE_URL: return
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("INSERT INTO user_memory (user_id, game_points) VALUES (%s, %s) "
                  "ON CONFLICT (user_id) DO UPDATE SET game_points = COALESCE(user_memory.game_points, 0) + %s",
                  (user_id, points, points))
        conn.commit()
        c.close(); conn.close()
    except Exception as e:
        print(f"DB Error add_points: {e}")

def get_user_total_points(user_id):
    if not DATABASE_URL: return 0
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT game_points FROM user_memory WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        c.close(); conn.close()
        return row[0] if row and row[0] else 0
    except Exception:
        return 0

def get_top_10_players():
    if not DATABASE_URL: return []
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT user_id, summary, game_points FROM user_memory WHERE game_points IS NOT NULL AND game_points > 0 ORDER BY game_points DESC LIMIT 10")
        rows = c.fetchall()
        c.close(); conn.close()
        results = []
        for uid, summary, pts in rows:
            name = "Player"
            if summary:
                for line in summary.split("\n"):
                    if line.strip().lower().startswith("naam:"):
                        val = line.split(":", 1)[1].strip()
                        if val.lower() not in ("not shared", ""):
                            name = val.split("(")[0].strip()
                        break
            results.append((uid, name, pts))
        return results
    except Exception as e:
        print(f"DB Error get_top_10: {e}")
        return []

# ⭐ ========== WELCOME KEYBOARD HELPER ==========
def get_welcome_game_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Play Game", callback_data="g_guide", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])]
    ])

# ⭐ ========== AI QUESTION GENERATOR ==========
async def generate_ai_question():
    if not _game_client:
        return random.choice(FALLBACK_QUESTIONS)
        
    prompt = """Tu ek flirty game bot hai. Ek fun, casual scenario banao jahan Sneha user se puch rahi hai ki wo kya karega.
Strictly is format me reply karo, no extra text:
Q: <1 line ka scenario>
A) <Option A>
B) <Option B>
C) <Option C>
D) <Option D>
BEST: <A/B/C/D>"""
    
    try:
        response = await asyncio.wait_for(
            _game_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=150
            ),
            timeout=4.0
        )
        text = response.choices[0].message.content.strip()
        
        q_match = re.search(r"Q:\s*(.*)", text)
        a_match = re.search(r"A\)\s*(.*)", text)
        b_match = re.search(r"B\)\s*(.*)", text)
        c_match = re.search(r"C\)\s*(.*)", text)
        d_match = re.search(r"D\)\s*(.*)", text)
        best_match = re.search(r"BEST:\s*([A-D])", text)
        
        if q_match and a_match and b_match and c_match and d_match and best_match:
            q = q_match.group(1).strip()
            opts = [a_match.group(1).strip(), b_match.group(1).strip(), c_match.group(1).strip(), d_match.group(1).strip()]
            best_letter = best_match.group(1).upper()
            best_idx = ["A", "B", "C", "D"].index(best_letter)
            return {"q": q, "opts": opts, "best": best_idx}
        else:
            raise ValueError("Parse fail")
    except Exception:
        q = random.choice(FALLBACK_QUESTIONS)
        return q

# ⭐ ========== GAME UI & LOGIC ==========
async def games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = context.bot.username
    keyboard = [
        [InlineKeyboardButton("Start Vibe Check", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Top 10 Leaders", callback_data="g_top", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["trophy"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    text = (
        f"<tg-emoji emoji-id=\"{PREMIUM_EMOJIS['fire']}\">🔥</tg-emoji> <b>Sneha's Vibe Check</b> <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['fire']}\">🔥</tg-emoji>\n\n"
        f"Kya tum mere dil ke aas paas bhi ho? <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['sparkle']}\">✨</tg-emoji>\n"
        "5 AI-generated sawaal honge, har baar naye! Sahi pe +10 points.\n"
        "Chat me koi spam nahi hoga, seedha popup aayega! ⚡\n\n"
        f"Apna score badhao aur <b>Top 10 Leaders</b> me apna naam dekho! <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji>"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await update.message.reply_text("🔴 Leaderboard sirf groups me dekha ja sakta hai! Group me /snehaleaderboard likho.")
        return
        
    top_players = get_top_10_players()
    if not top_players:
        await update.message.reply_text("🏆 <b>Leaderboard</b>\n\nAbhi koi khela nahi hai! Start playing to be #1.", parse_mode="HTML")
        return
        
    text = f"<tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji> <b>Top 10 Flirters</b> <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji>\n\n"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, (uid, name, pts) in enumerate(top_players):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        safe_name = html.escape(name)
        text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> - <b>{pts} pts</b>\n"
        
    bot_username = context.bot.username
    keyboard = [
        [InlineKeyboardButton("Start Vibe Check", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    bot_username = context.bot.username
    
    main_menu_keyboard = [
        [InlineKeyboardButton("Start Vibe Check", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Top 10 Leaders", callback_data="g_top", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["trophy"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    
    current_time = time.time()
    for uid in list(active_games.keys()):
        if current_time - active_games[uid].get("last_active", 0) > 300:
            del active_games[uid]
            
    # ⭐ DM BLOCK: Agar DM me game button dabaye, toh popup do
    if update.effective_chat.type == "private":
        keyboard = [
            [InlineKeyboardButton("ᴧᴅᴅ ϻє ʙᴧʙʏ", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
        ]
        text = f"🔴 <b>Game sirf groups me khel sakte ho!</b>\n\nMujhe kisi chat group me add karo aur wahan <code>/play</code> type karo ya /start wala button dabao! <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['fire']}\">🔥</tg-emoji>"
        await query.answer()
        try:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except:
            pass
        return
    
    # Group me button dabaye toh game menu do
    if data == "g_guide":
        await games_menu(update, context)
        return
        
    if data == "g_start":
        active_games[user.id] = {"q_idx": 0, "score": 0, "last_active": current_time}
        await ask_question(update, context, user)
        return
        
    elif data.startswith("g_ans_"):
        if user.id not in active_games:
            await query.answer("Game time out! Please start again.", show_alert=True)
            return
            
        active_games[user.id]["last_active"] = current_time
        parts = data.split("_")
        opt_idx = int(parts[2])
        game_data = active_games[user.id]
        current_q = game_data["current_q"]
        
        if opt_idx == current_q["best"]:
            game_data["score"] += 10
            await query.answer("✅ Sahi Jawab! +10 Points", show_alert=False)
        else:
            await query.answer("❌ Galat! 0 Points", show_alert=False)
            
        game_data["q_idx"] += 1
        
        if game_data["q_idx"] < 5:
            await ask_question(update, context, user)
        else:
            final_score = game_data["score"]
            add_points_to_db(user.id, final_score)
            total_pts = get_user_total_points(user.id)
            
            if final_score == 50:
                remark = f"Full Score! Tum sach meri jaan ho <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['heart']}\">❤️</tg-emoji>"
            elif final_score >= 30:
                remark = f"Mast! Tum mujhe thoda jante ho <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['gem']}\">💎</tg-emoji>"
            elif final_score >= 10:
                remark = f"Theek hai, try again <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['sparkle']}\">✨</tg-emoji>"
            else:
                remark = "Tumse na ho payega 😂"
                
            top_players = get_top_10_players()
            top_text = f"<tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji> <b>Top 10 Leaders</b> <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji>\n\n"
            if not top_players:
                top_text += "Abhi koi khela nahi hai!\n"
            else:
                medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
                for i, (uid, name, pts) in enumerate(top_players):
                    medal = medals[i] if i < len(medals) else f"{i+1}."
                    safe_name = html.escape(name)
                    top_text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> - <b>{pts} pts</b>\n"
            
            final_text = f"<b>Game Khatam!</b>\n\nIs game ka score: <b>{final_score}/50</b>\nTumhara Total Score: <b>{total_pts}</b> <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['heart']}\">❤️</tg-emoji>\n\n{remark}\n\n{top_text}"
            
            try:
                await query.edit_message_text(final_text, reply_markup=InlineKeyboardMarkup(main_menu_keyboard), parse_mode="HTML")
            except Exception: pass
            del active_games[user.id]
            
    elif data == "g_top":
        await query.answer()
        top_players = get_top_10_players()
        if not top_players:
            text = f"<tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji> <b>Leaderboard</b> <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji>\n\nAbhi koi khela nahi hai! Start playing to be #1."
        else:
            text = f"<tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji> <b>Top 10 Flirters</b> <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['trophy']}\">🏆</tg-emoji>\n\n"
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            for i, (uid, name, pts) in enumerate(top_players):
                medal = medals[i] if i < len(medals) else f"{i+1}."
                safe_name = html.escape(name)
                text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> - <b>{pts} pts</b>\n"
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(main_menu_keyboard), parse_mode="HTML")
        except Exception: pass

async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE, user):
    query = update.callback_query
    game_data = active_games[user.id]
    q_idx = game_data["q_idx"]
    
    try:
        await query.edit_message_text(f"<i>Sneha soch rahi hai sawaal {q_idx + 1}/5... <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['sparkle']}\">✨</tg-emoji></i>", parse_mode="HTML")
    except Exception: pass
    
    question = await generate_ai_question()
    game_data["current_q"] = question
    
    opts_with_idx = list(enumerate(question["opts"]))
    random.shuffle(opts_with_idx)
    
    keyboard = []
    for idx, text in opts_with_idx:
        if idx % 2 == 0:
            btn = InlineKeyboardButton(text, callback_data=f"g_ans_{idx}", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["gem"])
        else:
            btn = InlineKeyboardButton(text, callback_data=f"g_ans_{idx}", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["sparkle"])
        keyboard.append([btn])
    
    final_text = f"<b>Sawaal {q_idx + 1}/5</b> <tg-emoji emoji-id=\"{PREMIUM_EMOJIS['fire']}\">🔥</tg-emoji>\n\n{question['q']}"
    try:
        await query.edit_message_text(final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception: pass

async def newgame_command(update, context):
    await games_menu(update, context)
