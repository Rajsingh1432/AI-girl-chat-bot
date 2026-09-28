import random
import json
import os

STICKERS_FILE = "stickers.json"

def load_all_stickers():
    if os.path.exists(STICKERS_FILE):
        try:
            with open(STICKERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

async def save_sticker_if_owner(update, OWNER_ID):
    # Agar owner apne DM me sticker bhej raha hai toh usko save karo
    if update.effective_user.id == OWNER_ID and update.message.sticker and update.effective_chat.type == "private":
        sticker = update.message.sticker
        stickers = load_all_stickers()
        
        # Duplicate check
        for item in stickers:
            if item.get("file_unique_id") == sticker.file_unique_id:
                await update.message.reply_text("⚠️ Ye sticker pehle se save hai!")
                return
                
        data = {
            "file_id": sticker.file_id,
            "file_unique_id": sticker.file_unique_id,
            "emoji": sticker.emoji,
            "set_name": sticker.set_name
        }
        stickers.append(data)
        
        with open(STICKERS_FILE, "w", encoding="utf-8") as f:
            json.dump(stickers, f, indent=4, ensure_ascii=False)
            
        await update.message.reply_text(f"✅ Sticker save ho gaya! Total stickers: {len(stickers)}")

# Sticker par reply karne ke liye real human messages (Fallback)
STICKER_REPLIES = [
    # Hasi (Funny)
    "Hahaha ekdum hasi aa gayi 😅", "Ye kya bheja tumne? 😅", "Pagal ho kya tum? 🙃",
    "Ekdum mazaak chal raha hai kya? 🙃", "Hahaha, hasi control nahi ho rahi 😅",
    "Are waah, ekdum funny 😅", "Bhai ye kya chal raha hai yahan 😅",
    "Hahaha ekdum 😅", "Ekdum sahi mein 😅", "Kya bakwas sticker hai ye 😅",
    "Ye sticker dekhke hasi control nahi hui 😅", "Haha funny ho tum 😅",
    "Haha cute tha wo ☺️", "Hahaha, hasi aa rahi hai 😅", "Haha pagal insaan 🙃",
    "Are ruk kya kar rahi ho 😅", "Kya mazaak hai ye 🙃", "Tum ekdum pagal ho 🤫",
    "Haha itna mat hasao yaar 😅", "Ye kya reel bhej rahi ho? 🤫",
    
    # Tareef (Praising)
    "Mast sticker hai ye ☺️", "Mujhe ye pasand aaya ☺️", "Cute lag rahi ho 🥹",
    "Arey waah, sticker collection mast hai ☺️", "Bilkul perfect tha ye 😅",
    "Aww itna pyara 🥹", "Ekdum maza aagaya dekh ke ☺️", "Accha lag raha hai ☺️",
    "Kuch acha bhejo na ❤️", "Aur bhejo, acche lage ❤️", "Mast hai bilkul ☺️",
    "Itna cute sticker kahan se laye? 🥹", "Tumhara taste mast hai stickers me ☺️",
    "Ye sticker dekh ke mood fresh ho gaya ☺️", "Ekdum tumhare jaisa cute hai ye 🥹",
    
    # Daant (Scolding)
    "Mujhe stickers pasand nahi, text karo na 😒", "Kya point hai iska? 😒",
    "Uff itne stickers 😭", "Bas kar ab, text bolo! 😡", "Mujhe nahi pasand ye sticker 😒",
    "Mazaak mat udao ab 😡", "Bas bhi kar ab 😒", "Nahi nahi, ye galat hai 🙃",
    "Thodi shanti se bhejo 😭", "Mujhe accha nahi laga ye 😒", "Band karo ye stickers 😡",
    "Itna faltu sticker mat bhejo 😒", "Acha lalchao mat itne stickers bhejne ka 😒",
    "Are dimaag kharab mat karo itne stickers se 😭", "Text me bolo na, samajh nahi aata 😅",
    
    # Flirty / Casual
    "Aur bhejo na baby 🙃", "Sticker se zyada baat karne me maza aata hai ❤️",
    "Thoda text me baat karo na ☺️", "Text karo na yaar ☺️", "Aur kya kya hai tumhare paas? 🙃",
    "Stickers kam, baatein zyada karo ❤️", "Mera mood thik ho gaya dekh ke ☺️",
    "Ok ok samajh gayi 😅", "Samajh gayi tumhari baat 😅", "Haa baba samajh gayi 😅",
    "Acha ji, sticker bomb? 🙃", "Arre text me bolo na, samajh nahi aaya 😅",
    "Mujhe text wali baatein zyada pasand hain ❤️", "Sticker chhodo, muh se bolo na 🙃",
    "Itna direct mat ho baby 🙃", "Mast chal raha hai tumhara mood aaj ☺️",
    "Kya soch ke bheja ye? Batao na 🤫", "Hmm interesting sticker hai 🤫",
    "Ye kya signal de rahi ho mujhe? 🤫", "Sticker ke peeche chhup mat jao ❤️",
    
    # Short & Random
    "Hmm 🤫", "Ok ☺️", "Acha ☺️", "Haa 😅", "Nahi 😒", "Bas 😒",
    "Arre waah 🥹", "Uff 😒", "Lo ji 😅", "Kya baat hai ☺️",
    "Hmm thik hai 🙏", "Sahi hai 🙏", "Mast ☺️", "Cute 🥹", "Funny 😅",
    "Bakwas 😒", "Achha ji 🤫", "Hahaha 😅", "Pagal 🙃", "Abe yaar 😅",
    "Ohho 🙃", "Hmm baby ❤️", "Acha baby ☺️", "Nahi baby 🙃", "Haan ji 😅"
]

_sticker_pool = []  # For unique randomization (No quick repeat)

def get_random_sticker_reply():
    """Returns a tuple: ('sticker', file_id) or ('text', message_string)"""
    # 1. Pehle check karo agar koi saved sticker hai
    stickers = load_all_stickers()
    if stickers:
        return ('sticker', random.choice(stickers).get("file_id"))
    
    # 2. Agar koi sticker save nahi hai, toh purani text list se reply do
    global _sticker_pool
    if not _sticker_pool or len(_sticker_pool) == 0:
        _sticker_pool = STICKER_REPLIES.copy()
        random.shuffle(_sticker_pool)
    return ('text', _sticker_pool.pop())
