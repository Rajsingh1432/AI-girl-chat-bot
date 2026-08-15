import asyncio
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

SUPPORT_LINK = "https://t.me/+0xoXWln4qiM2NTY9"

# ==========================================
# GAME DATA
# ==========================================
TRUTHS = [
    "Tumhare phone me sabse embarrassing photo kiski hai?",
    "Group me sabse boring insaan kaun hai? (Sach batao)",
    "Tumne kabhi kisi ke peeche kya baat ki hai jise tum bahut pasand karte ho?",
    "Sabse aakhri baar jab tum jhoot bole, kya tha?",
    "Agar tum 1 din ke liye invisible ho jao, toh sabse pehle kya karoge?",
    "Tumhara sabse bada regret kya hai?",
    "Kis celebrity pe tumhara secret crush hai?",
    "Tumne kabhi school/college me cheat kiya hai?",
    "Sabse weird habit tumhari kya hai jo kisi ko nahi pata?",
    "Agar tumhare pass abhi 1 crore mil jaye, pehle kya khareedoge?"
]
DARES = [
    "Group me apni sabse buri selfie bhej do abhi!",
    "Apne pehle crush ka naam batao.",
    "Emoji ka use karke apni zindagi ka safar batao.",
    "Group me sabse active insaan ko ek cheesy compliment do.",
    "Agli 5 minutes me jo bhi message aaye, uska reply sirf 'Aww' me karna.",
    "Apni sabse pasandida movie ka ek dialogue bhej.",
    "Group me 'I am a potato' likh aur 10 baar 'Sorry Sneha' likh.",
    "Kisi bhi random group member ko tag karke 'Tum mere ho' bolo.",
    "Apna phone wallpaper describe karo.",
    "Apni aawaz ka ek voice note bhej jisme tum gaana gao."
]
EMOJI_PUZZLES = [
    {"e": "🦁👑", "ans": "The Lion King", "opts": ["The Lion King", "Madagascar", "Tarzan", "Jurassic Park"]},
    {"e": "🕷️🕸️🦸‍♂️", "ans": "Spiderman", "opts": ["Spiderman", "Batman", "Superman", "Ironman"]},
    {"e": "🚢🧊💔", "ans": "Titanic", "opts": ["Titanic", "Avatar", "Speed", "The Matrix"]},
    {"e": "🧙‍♂️👓⚡", "ans": "Harry Potter", "opts": ["Harry Potter", "Lord of the Rings", "Star Wars", "Percy Jackson"]},
    {"e": "🦇🃏😈", "ans": "Batman", "opts": ["Batman", "Joker", "Venom", "The Dark Knight"]},
    {"e": "👨‍🚀🪐⏳", "ans": "Interstellar", "opts": ["Interstellar", "Gravity", "Inception", "The Martian"]},
    {"e": "🤠🚀🦖", "ans": "Jurassic Park", "opts": ["Jurassic Park", "Toy Story", "The Good Dinosaur", "Ice Age"]},
    {"e": "💍🌋🧝‍♂️", "ans": "Lord of the Rings", "opts": ["Lord of the Rings", "The Hobbit", "Game of Thrones", "Willow"]},
    {"e": "🦈🌊😱", "ans": "Jaws", "opts": ["Jaws", "Deep Blue Sea", "Meg", "Piranha"]},
    {"e": "👻🔪📞", "ans": "Scream", "opts": ["Scream", "Halloween", "The Ring", "It"]},
]
BRAIN_QUESTIONS = [
    {"q": "Agar 5 machines 5 min me 5 shirt banati hain, toh 100 machines 100 shirt kitne min me banayengi?", "opts": ["100 min", "5 min", "20 min", "50 min"], "ans": "5 min"},
    {"q": "Doctor ne 3 dawai di, har 30 min baad leni hai. Sab khatam hone me kitna time lagega?", "opts": ["90 min", "30 min", "60 min", "1.5 hours"], "ans": "60 min"},
    {"q": "Ek gaadi me 3 pehriyan (wheels) aur 4 tyre hain. Ye kya hai?", "opts": ["Ek gaadi", "Tyre", "Cycle", "Tractor"], "ans": "Tyre"},
    {"q": "Aisi kya cheez hai jo gir to jati hai, par tutti nahi?", "opts": ["Aasoo", "Raat", "Saanp", "Patta"], "ans": "Raat"},
    {"q": "Agar tum 1 ko 1 se jodo to 1 aata hai. Ye kya hai?", "opts": ["Maths", "Knot (Ganth)", "Pencil", "Line"], "ans": "Knot (Ganth)"},
    {"q": "Ek farmer ko 17 bhed hain. Sab mar gaye except 9. Kitne bache?", "opts": ["8", "9", "17", "0"], "ans": "9"},
    {"q": "Bina haath pair ke kaun building bana sakta hai?", "opts": ["Insaan", "Machine", "Makdi (Spider)", "Makkhi"], "ans": "Makdi (Spider)"},
    {"q": "Aisi kya cheez hai jo jitna zyada kato, utni badi hoti hai?", "opts": ["Darwaza", "Soorakh (Hole)", "Pencil", "Kapda"], "ans": "Soorakh (Hole)"},
    {"q": "Kaun sa fal mitai me aata hai aur sabzi me bhi?", "opts": ["Seb", "Kela", "Gajar", "Tamatar"], "ans": "Gajar"},
    {"q": "Duniya ka sabse bada island kaunsa hai?", "opts": ["Australia", "Greenland", "Sri Lanka", "India"], "ans": "Greenland"},
]

active_games = {}  # chat_id -> game_data

# ==========================================
# 1. MAIN MENU
# ==========================================
async def games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ⭐ Professional Layout: 2 Buttons Top, 1 Button Bottom
    keyboard = [
        [InlineKeyboardButton("🎮 Truth & Dare", callback_data="g_td"),
         InlineKeyboardButton("🎬 Emoji Puzzle", callback_data="g_puzzle")],
        [InlineKeyboardButton("🧠 Rapid Fire Quiz", callback_data="g_brain")]
    ]
    
    # Agar update.message hai toh reply_text, warna edit_message_text (agar kisi ne button daba ke open kiya)
    text = (
        "<blockquote><b>🎮 Sneha's Game Arcade 🎮</b></blockquote>\n\n"
        "Khelne ke liye niche koi bhi game choose karo:\n\n"
        "🎮 <b>Truth & Dare</b> - Sach bolo ya task karo\n"
        "🎬 <b>Emoji Puzzle</b> - Emojis dekh ke movie guess karo\n"
        "🧠 <b>Rapid Fire Quiz</b> - Dimag lagao aur jeeto\n\n"
        "<i>💡 Multiplayer games me 30 seconds ke andar join karna padega!</i>"
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
    
    # Instant Game: Truth & Dare
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
        
    # Multiplayer Games Setup
    elif data == "g_puzzle":
        await query.answer()
        await init_join_phase(update, context, chat_id, user, "puzzle")
        return
    elif data == "g_brain":
        await query.answer()
        await init_join_phase(update, context, chat_id, user, "brain")
        return
        
    # Join Button Logic
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
            f"⏳ <b>{game['type']} Shuru Ho Raha Hai!</b>\n\nNiche <b>Join</b> button dabao!\nTumhare paas <b>30 seconds</b> hain.\n\n👥 <b>Players Joined:</b>\n{players_list}",
            reply_markup=query.message.reply_markup,
            parse_mode="HTML"
        )
        return
        
    # Answer Buttons Logic
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
        
    active_games[chat_id] = {
        "type": "Emoji Puzzle" if g_type == "puzzle" else "Rapid Fire Quiz",
        "players": {user.id: {"name": user.first_name, "score": 0}},
        "phase": "joining",
        "round": 0,
        "current_ans": None,
        "answered": set(),
        "msg_id": None,
        "round_ended": False
    }
    
    keyboard = [[InlineKeyboardButton("🎯 Join Game", callback_data="g_join")]]
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ <b>{active_games[chat_id]['type']} Shuru Ho Raha Hai!</b>\n\nNiche <b>Join</b> button dabao!\nTumhare paas <b>30 seconds</b> hain.\n\n👥 <b>Players Joined:</b>\n- {user.first_name}",
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
        
    if game['type'] == "Emoji Puzzle":
        await ask_puzzle(update, context, chat_id)
    else:
        game['round'] = 1
        await ask_brain(update, context, chat_id)

# ==========================================
# 4. EMOJI PUZZLE LOGIC
# ==========================================
async def ask_puzzle(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    p = random.choice(EMOJI_PUZZLES)
    game['current_ans'] = p['ans']
    game['answered'] = set()
    game['round_ended'] = False
    
    opts = p['opts'].copy()
    random.shuffle(opts)
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"g_pans_{opt}")] for opt in opts]
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b>🎬 EMOJI PUZZLE! 🎬</b></blockquote>\n\nCan you guess the movie? 🤔\n\n<b>Emojis:</b> {p['e']}\n\nNiche se sahi jawab dabao!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    game['msg_id'] = msg.message_id
    asyncio.create_task(puzzle_timer(update, context, chat_id))

async def puzzle_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(20)
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'playing' or game.get('round_ended'): return
    
    game['round_ended'] = True
    await context.bot.send_message(chat_id, f"⏳ Time up! Kisi ko nahi pata. Sahi jawab tha: <b>{game['current_ans']}</b>", parse_mode="HTML")
    await end_game_winner(update, context, chat_id)

async def handle_puzzle_ans(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, chosen_ans: str):
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
    if chosen_ans == game['current_ans']:
        game['round_ended'] = True  # ⭐ LOCK BUTTONS
        game['players'][user.id]['score'] += 1
        
        # Remove buttons from the message so no one can click them anymore
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
            
        await query.answer("✅ Bilkul Sahi!", show_alert=True)
        await context.bot.send_message(chat_id, f"🎉 Bhai sahab! <b>{user.first_name}</b> ne pakdi movie! 🎯", parse_mode="HTML")
        await end_game_winner(update, context, chat_id)
    else:
        await query.answer("❌ Galat jawab! 😏", show_alert=True)

# ==========================================
# 5. RAPID FIRE LOGIC
# ==========================================
async def ask_brain(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = active_games.get(chat_id)
    if not game: return
    
    if game['round'] > 3:
        await end_game_winner(update, context, chat_id)
        return
        
    q_data = random.choice(BRAIN_QUESTIONS)
    game['current_ans'] = q_data['ans']
    game['answered'] = set()
    game['round_ended'] = False
    
    opts = q_data['opts'].copy()
    random.shuffle(opts)
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"g_bans_{opt}")] for opt in opts]
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote><b>⏳ ROUND {game['round']}/3</b></blockquote>\n\n❓ <b>Sawaal:</b> {q_data['q']}\n\nNiche se sahi jawab dabao!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    game['msg_id'] = msg.message_id
    asyncio.create_task(brain_timer(update, context, chat_id))

async def brain_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(20)
    game = active_games.get(chat_id)
    if not game or game['phase'] != 'playing' or game.get('round_ended'):
        return
        
    game['round_ended'] = True  # ⭐ LOCK BUTTONS
    await context.bot.send_message(chat_id, f"⏳ Time up! Sahi jawab tha: <b>{game['current_ans']}</b>", parse_mode="Markdown")
    
    await asyncio.sleep(1)
    game['round'] += 1
    await ask_brain(update, context, chat_id)

async def handle_brain_ans(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, chosen_ans: str):
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
    if chosen_ans == game['current_ans']:
        game['round_ended'] = True  # ⭐ LOCK BUTTONS
        game['players'][user.id]['score'] += 1
        
        # Remove buttons so no one else can click
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
            
        await query.answer("✅ Bilkul Sahi!", show_alert=True)
        await context.bot.send_message(chat_id, f"🎯 <b>{user.first_name}</b> ne point pakda! +1", parse_mode="HTML")
        
        await asyncio.sleep(1)
        game['round'] += 1
        await ask_brain(update, context, chat_id)
    else:
        await query.answer("❌ Galat jawab! 😏", show_alert=True)

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
                
    # Professional Support Button at the bottom
    keyboard = [[InlineKeyboardButton("📡 Join Support Group", url=SUPPORT_LINK)]]
    
    await context.bot.send_message(
        chat_id=chat_id, 
        text=win_text, 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
