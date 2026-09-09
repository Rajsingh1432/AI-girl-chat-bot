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
    # TUMHARE 5 PREMIUM EMOJIS + KIDNAP (VERIFIED)
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
    {"q": "Main jab thodi sad/huti hu, toh tum kya karoge?", "opts": ["Pyaar se manaaoge", "Mazaak sunaoge", "Chhod doge", "Bologe 'ro mat'"], "best": 0},
    {"q": "Agar main tumhe 'I love you' bolu, toh reaction?", "opts": ["Gale lag jaunga", "Propose karunga wapas", "Mazaak mein taal dunga", "Block kar dunga"], "best": 0},
    {"q": "Hum dono kahan ghoomne jayenge pehli date pe?", "opts": ["Beach pe sunset", "Movie aur dinner", "Ghar pe Netflix", "Kahin nahi jaana"], "best": 0},
    {"q": "Meri sabse badi khasiyat kya hai?", "opts": ["Tumhara nature", "Tumhari smile", "Tumhara dimag", "Kuch nahi"], "best": 1},
    {"q": "Agar koi mujhe tang kare group me, toh tum?", "opts": ["Usko daaloge", "Mujhe ignore karne bologe", "Khud hasoge", "Uski taraf support karoge"], "best": 0}
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
        logger.error(f"DB Error add_points: {e}")

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
            name = None
            if summary:
                for line in summary.split("\n"):
                    if line.strip().lower().startswith("naam:"):
                        val = line.split(":", 1)[1].strip()
                        if val.lower() not in ("not shared", ""):
                            name = val.split("(")[0].strip()
                        break
            if not name:
                name = "Anonymous"
            results.append((uid, name, pts))
        return results
    except Exception as e:
        logger.error(f"DB Error get_top_10: {e}")
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
        
    system_prompt = "You output strictly in the requested format. No markdown, no extra text, no thinking tags."
    user_prompt = """Tu ek flirty game bot hai. Ek fun, casual scenario banao jahan Sneha user se puch rahi hai ki wo kya karega.
Strictly aur ONLY is format me reply karo:
Q: <1 line ka scenario>
A) <Option A (Sweet/Caring)>
B) <Option B (Funny/Teasing)>
C) <Option C (Neutral/Boring)>
D) <Option D (Rude/Toxic)>
BEST: <A/B/C/D>"""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    for attempt in range(3): # ⭐ 3 Baar Try Karega
        try:
            logger.info(f"🔄 Game AI: Attempt {attempt+1} to generate question...")
            response = await asyncio.wait_for(
                _game_client.chat.completions.create(
                    model="openai/gpt-oss-20b",
                    messages=messages,
                    temperature=0.9,
                    max_tokens=200
                ),
                timeout=15.0 # ⭐ Timeout badha kar 15s kar diya
            )
            text = response.choices[0].message.content.strip()
            logger.info(f"✅ Game AI Raw Response: {text[:150]}...")
            
            # 1. Think Tags Hatao
            text = re.sub(r"<think[\s\S]*?<\/think>", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"<think[\s\S]*", "", text, flags=re.IGNORECASE).strip()
            
            # 2. Markdown Code Blocks Hatao
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text).strip()
            text = re.sub(r"\n?```$", "", text).strip()
            
            # 3. Regex Parsing
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            q = None
            opts = []
            best_letter = None
            
            for line in lines:
                q_match = re.match(r"^\**Q\**\s*[:\-]\s*(.*)", line, re.IGNORECASE)
                if q_match and not q:
                    q = q_match.group(1).strip()
                    continue
                    
                opt_match = re.match(r"^\**[A-D]\**[\)\.]\s*(.*)", line, re.IGNORECASE)
                if opt_match and len(opts) < 4:
                    opts.append(opt_match.group(1).strip())
                    continue
                    
                best_match = re.match(r"^\**BEST\**\s*[:\-]\s*([A-D])", line, re.IGNORECASE)
                if best_match:
                    best_letter = best_match.group(1).upper()
                    
            if q and len(opts) == 4 and best_letter in ["A", "B", "C", "D"]:
                best_idx = ["A", "B", "C", "D"].index(best_letter)
                logger.info("✅ Game AI: Question Parsed Successfully!")
                return {"q": q, "opts": opts, "best": best_idx}
            else:
                logger.warning(f"⚠️ Game AI: Parse Fail! Q={q}, Opts={len(opts)}, Best={best_letter}")
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
        [InlineKeyboardButton("Start Vibe Check", callback_data="g_start", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["fire"])],
        [InlineKeyboardButton("Top 10 Leaders", callback_data="g_top", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["trophy"])],
        [InlineKeyboardButton("Add Me Baby", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])]
    ]
    text = (
        f"{FIRE} <b>Sneha's Vibe Check</b> {FIRE}\n\n"
        f"Kya tum mere dil ke aas paas bhi ho? {SPARKLE}\n"
        "5 AI-generated sawaal honge, har baar naye! Sahi pe +10 points.\n"
        f"Chat me koi spam nahi hoga, seedha popup aayega! ⚡\n\n"
        f"Apna score badhao aur <b>Top 10 Leaders</b> me apna naam dekho! {TROPHY}"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await update.message.reply_text("🔴 Leaderboard sirf groups me dekha ja sakta hai! Group me /snehaleaderboard likho.")
        return
        
    top_players = get_top_10_players()
    if not top_players:
        await update.message.reply_text(f"{TROPHY} <b>Leaderboard</b>\n\nAbhi koi khela nahi hai! Start playing to be #1.", parse_mode="HTML")
        return
        
    text = f"{TROPHY} <b>Top 10 Flirters</b> {TROPHY}\n\n"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, (uid, name, pts) in enumerate(top_players):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        safe_name = html.escape(name)
        text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> <b>| {pts} {HEART}</b>\n"
        
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
                remark = f"Full Score! Tum sach meri jaan ho {HEART}"
            elif final_score >= 30:
                remark = f"Mast! Tum mujhe thoda jante ho {GEM}"
            elif final_score >= 10:
                remark = f"Theek hai, try again {SPARKLE}"
            else:
                remark = "Tumse na ho payega 😂"
                
            top_players = get_top_10_players()
            top_text = f"{TROPHY} <b>Top 10 Leaders</b> {TROPHY}\n\n"
            if not top_players:
                top_text += "Abhi koi khela nahi hai!\n"
            else:
                medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
                for i, (uid, name, pts) in enumerate(top_players):
                    medal = medals[i] if i < len(medals) else f"{i+1}."
                    safe_name = html.escape(name)
                    top_text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> <b>| {pts} {HEART}</b>\n"
            
            final_text = f"<b>Game Khatam!</b>\n\nIs game ka score: <b>{final_score}/50</b>\nTumhara Total Score: <b>{total_pts}</b> {HEART}\n\n{remark}\n\n{top_text}"
            
            try:
                await query.edit_message_text(final_text, reply_markup=InlineKeyboardMarkup(main_menu_keyboard), parse_mode="HTML")
            except Exception: pass
            del active_games[user.id]
            
    elif data == "g_top":
        await query.answer()
        top_players = get_top_10_players()
        if not top_players:
            text = f"{TROPHY} <b>Leaderboard</b> {TROPHY}\n\nAbhi koi khela nahi hai! Start playing to be #1."
        else:
            text = f"{TROPHY} <b>Top 10 Flirters</b> {TROPHY}\n\n"
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            for i, (uid, name, pts) in enumerate(top_players):
                medal = medals[i] if i < len(medals) else f"{i+1}."
                safe_name = html.escape(name)
                text += f"{medal} <a href='tg://user?id={uid}'>{safe_name}</a> <b>| {pts} {HEART}</b>\n"
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(main_menu_keyboard), parse_mode="HTML")
        except Exception: pass

async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE, user):
    query = update.callback_query
    game_data = active_games[user.id]
    q_idx = game_data["q_idx"]
    
    try:
        await query.edit_message_text(f"<i>Sneha soch rahi hai sawaal {q_idx + 1}/5... {SPARKLE}</i>", parse_mode="HTML")
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
    
    final_text = f"<b>Sawaal {q_idx + 1}/5</b> {FIRE}\n\n{question['q']}"
    try:
        await query.edit_message_text(final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception: pass

async def newgame_command(update, context):
    await games_menu(update, context)
