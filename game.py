import asyncio
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
# ⭐ Alag file se questions import kar rahe hain
from questions import EMOJI_PUZZLES, BRAIN_QUESTIONS

SUPPORT_LINK = "https://t.me/+0xoXWln4qiM2NTY9"

TRUTHS = [
    "Tumhare phone me sabse embarrassing photo kiski hai?", "Group me sabse boring insaan kaun hai?", 
    "Tumne kabhi kisi ke peeche kya baat ki hai jise tum bahut pasand karte ho?", "Sabse aakhri baar jab tum jhoot bole, kya tha?",
    "Agar tum 1 din ke liye invisible ho jao, toh sabse pehle kya karoge?", "Tumhara sabse bada regret kya hai?",
    "Kis celebrity pe tumhara secret crush hai?", "Tumne kabhi school/college me cheat kiya hai?",
    "Sabse weird habit tumhari kya hai jo kisi ko nahi pata?", "Agar tumhare pass abhi 1 crore mil jaye, pehle kya khareedoge?",
    "Tum kabhi kisi ko date pe reject kar chuke ho?", "Group me sabse pyara insaan kaun hai?",
    "Tumhara sabse khatarnak dream kya tha?", "Kis insaan ki story tum hamesha skip karte ho?",
    "Tumne kabhi apne best friend ki backstabbing ki hai?", "Sabse choti baat jo tumhe irritate karti hai?",
    "Agar tum apna naam change kar sako toh kya rakhoge?", "Tumhara sabse bada fear kya hai?",
    "Kis bande ke sath tum akele ek room me nahi rehna chahoge?", "Tumne kabhi kisi ka dil dukhaya hai aur sorry nahi bola?"
]
DARES = [
    "Group me apni sabse buri selfie bhej do abhi!", "Apne pehle crush ka naam batao.",
    "Emoji ka use karke apni zindagi ka safar batao.", "Group me sabse active insaan ko ek cheesy compliment do.",
    "Agli 5 minutes me jo bhi message aaye, uska reply sirf 'Aww' me karna.", "Apni sabse pasandida movie ka ek dialogue bhej.",
    "Group me 'I am a potato' likh aur 10 baar 'Sorry Sneha' likh.", "Kisi bhi random group member ko tag karke 'Tum mere ho' bolo.",
    "Apna phone wallpaper describe karo.", "Apni aawaz ka ek voice note bhej jisme tum gaana gao.",
    "Apni sabse embarrassing moment batao.", "Group me sabse last message bhejne wale insaan ko 'Bhagwan' bolo.",
    "Agli baar jab tum message karo, toh har word ka last letter capitalize karna.", "Kisi ko tag karke unka tareef karo jaise wo tumhari life ka hero ho.",
    "Abhi apni current battery percentage batao.", "Apna favorite gaana 1 line me gao (text me).",
    "Group me sabse quiet insaan ko tag karke 'Bol kya hua' bolo.", "Apni morning routine 3 points me batao."
]

active_games = {}  # chat_id -> game_data

# ==========================================
# 1. MAIN MENU
# ==========================================
async def games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🎮 Truth & Dare", callback_data="g_td"),
         InlineKeyboardButton("🎬 Emoji Puzzle", callback_data="g_puzzle")],
        [InlineKeyboardButton("🧠 Rapid Fire Quiz", callback_data="g_brain")]
    ]
    
    text = (
        "<blockquote><b>🎮 Sneha's Game Arcade 🎮</b></blockquote>\n\n"
        "Khelne ke liye niche koi bhi game choose karo:\n\n"
        "🎮 <b>Truth &amp; Dare</b> - Sach bolo ya task karo\n"
        "🎬 <b>Emoji Puzzle</b> - Movie guess karo (10 Rounds)\n"
        "🧠 <b>Rapid Fire Quiz</b> - Dimag lagao (10 Rounds)\n\n"
        "<i>💡 Multiplayer games me 20 seconds ke andar join karna padega! Har sawaal ka time 15 seconds hoga.</i>"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ==========================================
# 2. BUTTON ROUTER
# ==========================================
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat.id
    user = query.from_user
    
    if data == "g_td":
        await query.answer()
        if random.random() > 0.5:
            await query.message.reply_text(
                f"<blockquote><b>🤔 SACH BOL BANDA! 🤔</b></blockquote>\n\n"
                f"Chalo <b>{user.first_name}</b>, ab nahi bach paoge 😏\n\n"
                f"<b>❓ Sawaal:</b> {random.choice(TRUTHS)}",
                parse_mode="HTML"
            )
        else:
            await query.message.reply_text(
                f"<blockquote><b>😈 SNEHA KA DARE! 😈</b></blockquote>\n\n"
                f"Oye <b>{user.first_name}</b>, ab tera kaam mushkil ho gaya 😎\n\n"
                f"<b>🔥 Tarefa:</b> {random.choice(DARES)}",
                parse_mode="HTML"
            )
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
            f"⏳ <b>{game['type']} Shuru Ho Raha Hai!</b>\n\nNiche <b>Join</b> button dabao!\nTumhare paas <b>20 seconds</b> hain.\n\n👥 <b>Players Joined:</b>\n{players_list}",
            reply_markup=query.message.reply_markup,
            parse_mode="HTML"
        )
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
        
    is_puzzle = (g_type == "puzzle")
    
    p_pool = EMOJI_PUZZLES.copy()
    b_pool = BRAIN_QUESTIONS.copy()
    random.shuffle(p_pool)
    random.shuffle(b_pool)
    
    active_games[chat_id] = {
        "type": "Emoji Puzzle" if is_puzzle else "Rapid Fire Quiz",
        "players": {user.id: {"name": user.first_name, "score": 0}},
        "phase": "joining",
        "round": 1,
        "total_rounds": 10,  # ⭐ 10 ROUNDS STRICT
        "current_ans_text": None,
        "correct_idx": None,
        "answered": set(),
        "msg_id": None,
        "round_ended": False,
        "p_pool": p_pool,
        "b_pool": b_pool
    }
    
    keyboard = [[InlineKeyboardButton("🎯 Join Game", callback_data="g_join")]]
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ <b>{active_games[chat_id]['type']} Shuru Ho Raha Hai!</b>\n\nNiche <b>Join</b> button dabao!\nTumhare paas <b>20 seconds</b> hain.\n\n👥 <b>Players Joined:</b>\n- {user.first_name}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    asyncio.create_task(join_timer(update, context, chat_id))

async def join_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(20) # 20 sec join time
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'joining': return
    
    game['phase'] = 'playing'
    players_count = len(game['players'])
    
    if players_count == 1:
        await context.bot.send_message(chat_id, "Koi nahi aya? Chalo koi baat nahi, tum akela hi kheloge! Game shuru! 🔥")
    else:
        await context.bot.send_message(chat_id, f"Times up! Total {players_count} log khel rahe hain. Chalo shuru karte hain! 🔥")
        
    if game['type'] == "Emoji Puzzle":
        await ask_puzzle(update, context, chat_id)
    else:
        await ask_brain(update, context, chat_id)

# ==========================================
# 4. EMOJI PUZZLE LOGIC (10 Rounds)
# ==========================================
async def ask_puzzle(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    # ⭐ STRICT CHECK: 10 rounds poore hone par hi winner declare hoga
    if game['round'] > game['total_rounds']:
        await end_game_winner(update, context, chat_id)
        return
        
    if not game['p_pool']:
        game['p_pool'] = EMOJI_PUZZLES.copy()
        random.shuffle(game['p_pool'])
        
    p = game['p_pool'].pop()
    
    opts = p['opts'].copy()
    random.shuffle(opts)
    
    # ⭐ 100% ACCURATE INDEX SYSTEM
    correct_idx = opts.index(p['ans'])
    game['correct_idx'] = correct_idx
    game['current_ans_text'] = p['ans']
    game['answered'] = set()
    game['round_ended'] = False
    
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"g_pans_{i}")] for i, opt in enumerate(opts)]
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b>🎬 ROUND {game['round']}/{game['total_rounds']}</b></blockquote>\n\nCan you guess the movie? 🤔\n\n<b>Emojis:</b> {p['e']}\n\nNiche se sahi jawab dabao!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    game['msg_id'] = msg.message_id
    asyncio.create_task(puzzle_timer(update, context, chat_id))

async def puzzle_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(15) # ⭐ 15 SEC TIMER
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'playing' or game.get('round_ended'): return
    
    game['round_ended'] = True
    # ⭐ ROAST ON TIMEOUT
    roasts = [
        "⏳ Time up! Kisi ka dimag nahi chala? 😏 Sahi jawab tha:",
        "⏳ 15 second khatam! Bade khiladi lagte ho? 😭 Sahi jawab tha:",
        "⏳ Arey bhai, itna easy sawaal tha! 🙄 Sahi jawab:"
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
        game['players'][user.id]['score'] += 1
        
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
            
        await query.answer("✅ Bilkul Sahi!", show_alert=True)
        await context.bot.send_message(chat_id, f"🎉 Wah! <b>{user.first_name}</b> ne sahi jawab de diya! 🎯\n\n✅ Sahi Jawab: <b>{game['current_ans_text']}</b>\n\n+1 Point!", parse_mode="HTML")
        
        await asyncio.sleep(2)
        game['round'] += 1
        await ask_puzzle(update, context, chat_id)
    else:
        await query.answer("❌ Galat Jawab!", show_alert=True)
        await context.bot.send_message(chat_id, f"❌ <b>{user.first_name}</b>, ye galat jawab hai! Soch samajh kar daba. 🤔", parse_mode="HTML")


# ==========================================
# 5. RAPID FIRE LOGIC (10 Rounds)
# ==========================================
async def ask_brain(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    # ⭐ STRICT CHECK: 10 rounds poore hone par hi winner declare hoga
    if game['round'] > game['total_rounds']:
        await end_game_winner(update, context, chat_id)
        return
        
    if not game['b_pool']:
        game['b_pool'] = BRAIN_QUESTIONS.copy()
        random.shuffle(game['b_pool'])
        
    q_data = game['b_pool'].pop()
    
    opts = q_data['opts'].copy()
    random.shuffle(opts)
    
    # ⭐ 100% ACCURATE INDEX SYSTEM
    correct_idx = opts.index(q_data['ans'])
    game['correct_idx'] = correct_idx
    game['current_ans_text'] = q_data['ans']
    game['answered'] = set()
    game['round_ended'] = False
    
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"g_bans_{i}")] for i, opt in enumerate(opts)]
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b>⏳ ROUND {game['round']}/{game['total_rounds']}</b></blockquote>\n\n❓ <b>Sawaal:</b> {q_data['q']}\n\nNiche se sahi jawab dabao!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    game['msg_id'] = msg.message_id
    asyncio.create_task(brain_timer(update, context, chat_id))

async def brain_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(15) # ⭐ 15 SEC TIMER
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'playing' or game.get('round_ended'):
        return
        
    game['round_ended'] = True
    # ⭐ ROAST ON TIMEOUT
    roasts = [
        "⏳ Time up! Koi point nahi mila? 😏 Sahi jawab tha:",
        "⏳ 15 second khatam! Bade dimag wale lagte ho? 😭 Sahi jawab tha:",
        "⏳ Arey bhai, itna easy sawaal tha! 🙄 Sahi jawab:"
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
        game['players'][user.id]['score'] += 1
        
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
            
        await query.answer("✅ Bilkul Sahi!", show_alert=True)
        await context.bot.send_message(chat_id, f"🎯 <b>{user.first_name}</b> ne dimag lagaya aur sahi jawab diya!\n\n✅ Sahi Jawab: <b>{game['current_ans_text']}</b>\n\n+1 Point!", parse_mode="HTML")
        
        await asyncio.sleep(2)
        game['round'] += 1
        await ask_brain(update, context, chat_id)
    else:
        await query.answer("❌ Galat Jawab!", show_alert=True)
        await context.bot.send_message(chat_id, f"❌ <b>{user.first_name}</b>, galat jawab! Aur socho. 🤔", parse_mode="HTML")

# ==========================================
# 6. WINNER ANNOUNCEMENT
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
            win_text = f"<blockquote><b>🏆 GAME KHATAM! WINNER IS... 🏆</b></blockquote>\n\n👑 <b>{winner['name']}</b> ne jeet liya with <b>{winner['score']}</b> points!\n\n"
            win_text += "📊 <b>Final Scores:</b>\n"
            for i, p in enumerate(scores, 1):
                win_text += f"{i}. {p['name']}: {p['score']} points\n"
                
    keyboard = [[InlineKeyboardButton("📡 Join Support Group", url=SUPPORT_LINK)]]
    
    await context.bot.send_message(
        chat_id=chat_id, 
        text=win_text, 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
