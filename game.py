import asyncio
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
# ⭐ Alag file se questions import kar rahe hain
from questions import EMOJI_PUZZLES, BRAIN_QUESTIONS, WORD_PUZZLES

SUPPORT_LINK = "https://t.me/+0xoXWln4qiM2NTY9"

# ⭐ ========== PREMIUM EMOJI + COLOR BUTTON STYLING ==========
# bot.py se independent rakha gaya hai (koi circular import na ho).
class ButtonStyle:
    PRIMARY = "primary"
    DANGER = "danger"

# Caption-emoji IDs (bot.py ke welcome-caption me use hui wahi 8 IDs)
GAME_EMOJIS = {
    "dance": "6332268261010315734",   # 💃
    "flower": "6332617871348210023",  # 🌸
    "kiss": "6318642082126763758",    # 😘
    "devil": "6318777236157633080",   # 😈
    "party": "5801018335919347111",   # 🎉
    "sparkle": "6143155267509948558", # ✨
    "crown": "6289279495257986194",   # 👑
    "fire": "6334360245090915308",    # 🔥
}

# ⭐ DUPLICATE QUESTIONS REMOVE KARNE KA HELPER
def _dedupe_questions(items):
    seen = set()
    unique = []
    for item in items:
        key = (
            item.get("q", ""),
            item.get("e", ""),
            item.get("ans", "")
        )
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique

# ⭐ Import ke baad hi dedupe karo taaki global pool me duplicate na jaye
EMOJI_PUZZLES = _dedupe_questions(EMOJI_PUZZLES)
BRAIN_QUESTIONS = _dedupe_questions(BRAIN_QUESTIONS)
WORD_PUZZLES = _dedupe_questions(WORD_PUZZLES)

# ⭐ GLOBAL POOL SYSTEM: Ye list poore bot ke lifetime ke liye yaad rakhega
# Jab tak ye khaali nahi hogi, koi question repeat nahi hoga (chahe 100 game khel lo)
GLOBAL_P_POOL = []
GLOBAL_B_POOL = []
GLOBAL_W_POOL = []

# Function to get a unique question globally
def get_unique_question(g_type):
    global GLOBAL_P_POOL, GLOBAL_B_POOL, GLOBAL_W_POOL
    
    if g_type == "puzzle":
        if not GLOBAL_P_POOL:
            GLOBAL_P_POOL = EMOJI_PUZZLES.copy()
            random.shuffle(GLOBAL_P_POOL)
        return GLOBAL_P_POOL.pop()
        
    elif g_type == "brain":
        if not GLOBAL_B_POOL:
            GLOBAL_B_POOL = BRAIN_QUESTIONS.copy()
            random.shuffle(GLOBAL_B_POOL)
        return GLOBAL_B_POOL.pop()
        
    elif g_type == "word":
        if not GLOBAL_W_POOL:
            GLOBAL_W_POOL = WORD_PUZZLES.copy()
            random.shuffle(GLOBAL_W_POOL)
        return GLOBAL_W_POOL.pop()

active_games = {}  # chat_id -> game_data

# ==========================================
# 1. MAIN MENU
# ==========================================
async def games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("Word Guess", callback_data="g_word", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=GAME_EMOJIS["sparkle"]),
         InlineKeyboardButton("Emoji Puzzle", callback_data="g_puzzle", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=GAME_EMOJIS["party"])],
        [InlineKeyboardButton("Rapid Fire Quiz", callback_data="g_brain", style=ButtonStyle.DANGER, icon_custom_emoji_id=GAME_EMOJIS["fire"])]
    ]
    
    text = (
        f"<blockquote><b><tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji> Sneha's Game Arcade <tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji></b></blockquote>\n\n"
        "Khelne ke liye niche koi bhi game choose karo:\n\n"
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji> <b>Word Guess</b> - Crossword style dimag lagao\n"
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> <b>Emoji Puzzle</b> - Movie guess karo (10 Rounds)\n"
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['fire']}\">🔥</tg-emoji> <b>Rapid Fire Quiz</b> - Trivia aur logic (10 Rounds)\n\n"
        f"<i><tg-emoji emoji-id=\"{GAME_EMOJIS['flower']}\">🌸</tg-emoji> Multiplayer games me 30 seconds ke andar join karna padega! Har sawaal ka time 30 seconds hoga.</i>"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            # ⭐ FIX: Agar original message photo/caption wala hai (jaise /start ka
            # welcome image), to edit_message_text fail ho jaata hai kyunki us
            # message me "text" hota hi nahi, sirf "caption" hota hai. Us case
            # me edit karne ki bajaye naya message bhej dete hain.
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ==========================================
# 2. BUTTON ROUTER
# ==========================================
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat.id
    user = query.from_user
    
    if data == "g_word":
        await query.answer()
        await init_join_phase(update, context, chat_id, user, "word")
        return
    elif data == "g_puzzle":
        await query.answer()
        await init_join_phase(update, context, chat_id, user, "puzzle")
        return
    elif data == "g_brain":
        await query.answer()
        await init_join_phase(update, context, chat_id, user, "brain")
        return
        
    elif data == "g_join":
        game = active_games.get(chat_id)
        if not game or game['phase'] != 'joining':
            await query.answer("Game already started ya cancel ho gaya! 🙄", show_alert=True)
            return
        if user.id in game['players']:
            await query.answer("Tu pehle se join kar chuka hai bawa! 🤭", show_alert=True)
            return
        game['players'][user.id] = {"name": user.first_name, "score": 0}
        
        players_list = "\n".join([f"- {p['name']}" for p in game['players'].values()])
        await query.edit_message_text(
            f"<blockquote><b><tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji> {game['type']} Shuru Ho Raha Hai!</b></blockquote>\n\nNiche <b>Join</b> button dabao!\nTumhare paas <b>30 seconds</b> hain.\n\n<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> <b>Players Joined:</b>\n{players_list}",
            reply_markup=query.message.reply_markup,
            parse_mode="HTML"
        )
        return
        
    elif data.startswith("g_wans_"):
        await handle_word_ans(update, context, chat_id, user, data[7:])
        return
        
    elif data.startswith("g_pans_"):
        await handle_puzzle_ans(update, context, chat_id, user, data[7:])
        return
        
    elif data.startswith("g_bans_"):
        await handle_brain_ans(update, context, chat_id, user, data[7:])
        return

# ==========================================
# 3. MULTIPLAYER JOIN LOGIC
# ==========================================
async def init_join_phase(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, g_type: str):
    if chat_id in active_games:
        await update.callback_query.answer("Arre baba, pehle wala game toh khatam hone do! 🙄", show_alert=True)
        return
        
    if g_type == "puzzle":
        g_name = "Emoji Puzzle"
    elif g_type == "brain":
        g_name = "Rapid Fire Quiz"
    else:
        g_name = "Word Guess"
    
    active_games[chat_id] = {
        "type": g_name,
        "g_type": g_type,
        "players": {user.id: {"name": user.first_name, "score": 0}},
        "phase": "joining",
        "round": 1,
        "total_rounds": 10,
        "current_ans_text": None,
        "correct_idx": None,
        "answered": set(),
        "msg_id": None,
        "round_ended": False,
        "timer_task": None
    }
    
    keyboard = [[InlineKeyboardButton("Join Game", callback_data="g_join", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=GAME_EMOJIS["fire"])]]
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b><tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji> {active_games[chat_id]['type']} Shuru Ho Raha Hai!</b></blockquote>\n\nNiche <b>Join</b> button dabao!\nTumhare paas <b>30 seconds</b> hain.\n\n<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> <b>Players Joined:</b>\n- {user.first_name}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    asyncio.create_task(join_timer(update, context, chat_id))

async def join_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(30)
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'joining': return
    
    game['phase'] = 'playing'
    players_count = len(game['players'])
    
    if players_count == 1:
        await context.bot.send_message(chat_id, "Koi nahi aya? Chalo koi baat nahi, tum akela hi kheloge! Game shuru! 🔥")
    else:
        await context.bot.send_message(chat_id, f"Times up! Total {players_count} log khel rahe hain. Chalo shuru karte hain! 🔥")
        
    if game['g_type'] == "puzzle":
        await ask_puzzle(update, context, chat_id)
    elif game['g_type'] == "brain":
        await ask_brain(update, context, chat_id)
    else:
        await ask_word(update, context, chat_id)


# ==========================================
# 4. WORD GUESS LOGIC (10 Rounds)
# ==========================================
async def ask_word(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    if game['round'] > game['total_rounds']:
        await end_game_winner(update, context, chat_id)
        return
        
    # ⭐ GLOBAL POOL SE QUESTION LO
    w = get_unique_question("word")
    
    opts = w['opts'].copy()
    random.shuffle(opts)
    
    correct_idx = opts.index(w['ans'])
    game['correct_idx'] = correct_idx
    game['current_ans_text'] = w['ans']
    game['answered'] = set()
    game['round_ended'] = False
    
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"g_wans_{i}", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=GAME_EMOJIS["flower"])] for i, opt in enumerate(opts)]
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b><tg-emoji emoji-id=\"{GAME_EMOJIS['flower']}\">🌸</tg-emoji> ROUND {game['round']}/{game['total_rounds']}</b></blockquote>\n\nCan you guess the word? <tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji>\n\n<b>{w['q']}</b>\n\nNiche se sahi jawab dabao!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    game['msg_id'] = msg.message_id
    game['timer_task'] = asyncio.create_task(word_timer(update, context, chat_id))

async def word_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        return
        
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'playing' or game.get('round_ended'): return
    
    game['round_ended'] = True
    roasts = [
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['flower']}\">🌸</tg-emoji> Time up! Kisi ka dimag nahi chala? Sahi jawab tha:",
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['flower']}\">🌸</tg-emoji> 30 second khatam! Bade khiladi lagte ho? Sahi jawab tha:",
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['flower']}\">🌸</tg-emoji> Arey bhai, itna easy sawaal tha! Sahi jawab:"
    ]
    await context.bot.send_message(chat_id, f"{random.choice(roasts)} <b>{game.get('current_ans_text', 'Unknown')}</b>\n\nChalo agla sawaal...", parse_mode="HTML")
    
    await asyncio.sleep(2)
    game['round'] += 1
    await ask_word(update, context, chat_id)

async def handle_word_ans(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, chosen_idx_str: str):
    query = update.callback_query
    game = active_games.get(chat_id)
    if not game:
        await query.answer("Game khatam ho chuki hai! 🙄", show_alert=True)
        return
        
    if game['phase'] != 'playing':
        await query.answer("Game abhi shuru nahi hua! 🙄", show_alert=True)
        return
        
    if game.get('round_ended'):
        await query.answer("Bhai ye round khatam ho chuka hai! 🙄", show_alert=True)
        return
        
    if user.id not in game['players']:
        await query.answer("Tu game join nahi kiya tha! 🙄", show_alert=True)
        return
    if user.id in game['answered']:
        await query.answer("Arre ek baar me ek hi jawab! 😡", show_alert=True)
        return
        
    game['answered'].add(user.id)
    
    try:
        chosen_idx = int(chosen_idx_str)
    except:
        return
        
    if chosen_idx == game['correct_idx']:
        game['round_ended'] = True
        if game.get('timer_task'):
            game['timer_task'].cancel()
            
        game['players'][user.id]['score'] += 1
        
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
            
        await query.answer("✅ Bilkul Sahi!", show_alert=True)
        await context.bot.send_message(chat_id, f"<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> Wah! <b>{user.first_name}</b> ne sahi word pakda! <tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji>\n\n✅ Sahi Jawab: <b>{game['current_ans_text']}</b>\n\n+1 Point!", parse_mode="HTML")
        
        await asyncio.sleep(2)
        game['round'] += 1
        await ask_word(update, context, chat_id)
    else:
        await query.answer("❌ Galat Jawab! Koi aur try karega.", show_alert=True)
        await context.bot.send_message(chat_id, f"❌ <b>{user.first_name}</b> galat jawab de gaya. Koi aur try karo! <tg-emoji emoji-id=\"{GAME_EMOJIS['flower']}\">🌸</tg-emoji>", parse_mode="HTML")


# ==========================================
# 5. EMOJI PUZZLE LOGIC (10 Rounds)
# ==========================================
async def ask_puzzle(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    if game['round'] > game['total_rounds']:
        await end_game_winner(update, context, chat_id)
        return
        
    # ⭐ GLOBAL POOL SE QUESTION LO
    p = get_unique_question("puzzle")
    
    opts = p['opts'].copy()
    random.shuffle(opts)
    
    correct_idx = opts.index(p['ans'])
    game['correct_idx'] = correct_idx
    game['current_ans_text'] = p['ans']
    game['answered'] = set()
    game['round_ended'] = False
    
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"g_pans_{i}", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=GAME_EMOJIS["party"])] for i, opt in enumerate(opts)]
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b><tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> ROUND {game['round']}/{game['total_rounds']}</b></blockquote>\n\nCan you guess the movie? <tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji>\n\n<b>Emojis:</b> {p['e']}\n\nNiche se sahi jawab dabao!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    game['msg_id'] = msg.message_id
    game['timer_task'] = asyncio.create_task(puzzle_timer(update, context, chat_id))

async def puzzle_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        return
        
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'playing' or game.get('round_ended'): return
    
    game['round_ended'] = True
    roasts = [
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> Time up! Kisi ka dimag nahi chala? Sahi jawab tha:",
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> 30 second khatam! Bade khiladi lagte ho? Sahi jawab tha:",
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> Arey bhai, itna easy sawaal tha! Sahi jawab:"
    ]
    await context.bot.send_message(chat_id, f"{random.choice(roasts)} <b>{game.get('current_ans_text', 'Unknown')}</b>\n\nChalo agla sawaal...", parse_mode="HTML")
    
    await asyncio.sleep(2)
    game['round'] += 1
    await ask_puzzle(update, context, chat_id)

async def handle_puzzle_ans(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, chosen_idx_str: str):
    query = update.callback_query
    game = active_games.get(chat_id)
    if not game:
        await query.answer("Game khatam ho chuki hai! 🙄", show_alert=True)
        return
        
    if game['phase'] != 'playing':
        await query.answer("Game abhi shuru nahi hua! 🙄", show_alert=True)
        return
        
    if game.get('round_ended'):
        await query.answer("Bhai ye round khatam ho chuka hai! 🙄", show_alert=True)
        return
        
    if user.id not in game['players']:
        await query.answer("Tu game join nahi kiya tha! 🙄", show_alert=True)
        return
    if user.id in game['answered']:
        await query.answer("Arre ek baar me ek hi jawab! 😡", show_alert=True)
        return
        
    game['answered'].add(user.id)
    
    try:
        chosen_idx = int(chosen_idx_str)
    except:
        return
        
    if chosen_idx == game['correct_idx']:
        game['round_ended'] = True
        if game.get('timer_task'):
            game['timer_task'].cancel()
            
        game['players'][user.id]['score'] += 1
        
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
            
        await query.answer("✅ Bilkul Sahi!", show_alert=True)
        await context.bot.send_message(chat_id, f"<tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji> Wah! <b>{user.first_name}</b> ne sahi jawab de diya! <tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji>\n\n✅ Sahi Jawab: <b>{game['current_ans_text']}</b>\n\n+1 Point!", parse_mode="HTML")
        
        await asyncio.sleep(2)
        game['round'] += 1
        await ask_puzzle(update, context, chat_id)
    else:
        await query.answer("❌ Galat Jawab! Koi aur try karega.", show_alert=True)
        await context.bot.send_message(chat_id, f"❌ <b>{user.first_name}</b> galat jawab de gaya. Koi aur try karo! <tg-emoji emoji-id=\"{GAME_EMOJIS['party']}\">🎉</tg-emoji>", parse_mode="HTML")


# ==========================================
# 6. RAPID FIRE LOGIC (10 Rounds)
# ==========================================
async def ask_brain(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    if game['round'] > game['total_rounds']:
        await end_game_winner(update, context, chat_id)
        return
        
    # ⭐ GLOBAL POOL SE QUESTION LO
    q_data = get_unique_question("brain")
    
    opts = q_data['opts'].copy()
    random.shuffle(opts)
    
    correct_idx = opts.index(q_data['ans'])
    game['correct_idx'] = correct_idx
    game['current_ans_text'] = q_data['ans']
    game['answered'] = set()
    game['round_ended'] = False
    
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"g_bans_{i}", style=ButtonStyle.DANGER, icon_custom_emoji_id=GAME_EMOJIS["fire"])] for i, opt in enumerate(opts)]
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b><tg-emoji emoji-id=\"{GAME_EMOJIS['fire']}\">🔥</tg-emoji> ROUND {game['round']}/{game['total_rounds']}</b></blockquote>\n\n<tg-emoji emoji-id=\"{GAME_EMOJIS['devil']}\">😈</tg-emoji> <b>Sawaal:</b> {q_data['q']}\n\nNiche se sahi jawab dabao!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    game['msg_id'] = msg.message_id
    game['timer_task'] = asyncio.create_task(brain_timer(update, context, chat_id))

async def brain_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        return
        
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'playing' or game.get('round_ended'):
        return
        
    game['round_ended'] = True
    roasts = [
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['fire']}\">🔥</tg-emoji> Time up! Koi point nahi mila? Sahi jawab tha:",
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['fire']}\">🔥</tg-emoji> 30 second khatam! Bade dimag wale lagte ho? Sahi jawab tha:",
        f"<tg-emoji emoji-id=\"{GAME_EMOJIS['fire']}\">🔥</tg-emoji> Arey bhai, itna easy sawaal tha! Sahi jawab:"
    ]
    await context.bot.send_message(chat_id, f"{random.choice(roasts)} <b>{game.get('current_ans_text', 'Unknown')}</b>\n\nChalo agla sawaal...", parse_mode="HTML")
    
    await asyncio.sleep(2)
    game['round'] += 1
    await ask_brain(update, context, chat_id)

async def handle_brain_ans(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, chosen_idx_str: str):
    query = update.callback_query
    game = active_games.get(chat_id)
    if not game:
        await query.answer("Game khatam ho chuki hai! 🙄", show_alert=True)
        return
        
    if game['phase'] != 'playing':
        await query.answer("Game abhi shuru nahi hua! 🙄", show_alert=True)
        return
        
    if game.get('round_ended'):
        await query.answer("Bhai ye round khatam ho chuka hai! 🙄", show_alert=True)
        return
        
    if user.id not in game['players']:
        await query.answer("Tu game join nahi kiya tha! 🙄", show_alert=True)
        return
    if user.id in game['answered']:
        await query.answer("Arre ek baar me ek hi jawab! 😡", show_alert=True)
        return
        
    game['answered'].add(user.id)
    
    try:
        chosen_idx = int(chosen_idx_str)
    except:
        return
        
    if chosen_idx == game['correct_idx']:
        game['round_ended'] = True
        if game.get('timer_task'):
            game['timer_task'].cancel()
            
        game['players'][user.id]['score'] += 1
        
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
            
        await query.answer("✅ Bilkul Sahi!", show_alert=True)
        await context.bot.send_message(chat_id, f"<tg-emoji emoji-id=\"{GAME_EMOJIS['fire']}\">🔥</tg-emoji> <b>{user.first_name}</b> ne dimag lagaya aur sahi jawab diya!\n\n✅ Sahi Jawab: <b>{game['current_ans_text']}</b>\n\n+1 Point!", parse_mode="HTML")
        
        await asyncio.sleep(2)
        game['round'] += 1
        await ask_brain(update, context, chat_id)
    else:
        await query.answer("❌ Galat Jawab! Koi aur try karega.", show_alert=True)
        await context.bot.send_message(chat_id, f"❌ <b>{user.first_name}</b> galat jawab de gaya. Koi aur try karo! <tg-emoji emoji-id=\"{GAME_EMOJIS['fire']}\">🔥</tg-emoji>", parse_mode="HTML")

# ==========================================
# 7. WINNER ANNOUNCEMENT
# ==========================================
async def end_game_winner(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.pop(chat_id, None)
    if not game: return
    
    scores = sorted(game['players'].values(), key=lambda x: x['score'], reverse=True)
    
    if not scores or scores[0]['score'] == 0:
        win_text = "😔 Kisi ka bhi koi point nahi bana. Koi winner nahi!"
    else:
        winner = scores[0]
        if len(scores) > 1 and scores[0]['score'] == scores[1]['score']:
            win_text = "🤝 Yeh game tie raha! Dono ne bahut achha khela."
        else:
            win_text = f"<blockquote><b>🏆 GAME KHATAM! WINNER IS... 🏆</b></blockquote>\n\n<tg-emoji emoji-id=\"{GAME_EMOJIS['crown']}\">👑</tg-emoji> <b>{winner['name']}</b> ne jeet liya with <b>{winner['score']}</b> points!\n\n"
            win_text += f"<tg-emoji emoji-id=\"{GAME_EMOJIS['sparkle']}\">✨</tg-emoji> <b>Final Scores:</b>\n"
            for i, p in enumerate(scores, 1):
                win_text += f"{i}. {p['name']}: {p['score']} points\n"
                
    keyboard = [[InlineKeyboardButton("Join Support Group", url=SUPPORT_LINK, style=ButtonStyle.PRIMARY, icon_custom_emoji_id=GAME_EMOJIS["flower"])]]
    
    await context.bot.send_message(
        chat_id=chat_id, 
        text=win_text, 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
