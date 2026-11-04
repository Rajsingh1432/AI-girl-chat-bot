import os
import json
import logging
import re
import time
import random
import asyncio
import html
import hashlib
import io
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import psycopg2
from psycopg2 import pool as pg_pool
from telegram import InputFile, Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, ReactionTypeEmoji, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import RetryAfter, TimedOut, Forbidden
from groq import AsyncGroq
from dotenv import load_dotenv
from sticker_replies import get_random_sticker_reply, save_sticker_if_owner
from broadcast import broadcast_command, broadcast_stats_command, broadcastgc_command
from wordseek_engine import WordSeekEngine

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

engine = WordSeekEngine()

try:
    from config import PREMIUM_EMOJIS, ButtonStyle
except ImportError:
    class ButtonStyle:
        PRIMARY = "primary"
        DANGER = "danger"
    PREMIUM_EMOJIS = {
        "kidnap": "6001154049452283936",
        "developer": "5362079447136610876",
        "channel": "6257898707551785373",
        "support": "5359622339296256165",
        "fire": "5280588940980542826",
    }

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "openai/gpt-oss-20b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")

# Group me bina mention ke (bot admin ho tab) kitne % messages ka reply de. 1.0 = sabka (purana behaviour)
GROUP_REPLY_CHANCE = float(os.getenv("GROUP_REPLY_CHANCE", "1.0"))
# Kaafi din baad gayab user ko DM me yaad dilana (Nova jaisa "VPS ka kya hua?")
PROACTIVE_CHECKINS = os.getenv("PROACTIVE_CHECKINS", "1") == "1"
WELCOME_IMAGE_URL = os.getenv("WELCOME_IMAGE_URL", "https://ibb.co/7H2zgCT")
MAX_HISTORY_MESSAGES = 20
# Sneha ke cute voice notes (pyaar bhari baatein / special moments pe)
VOICE_REPLIES = os.getenv("VOICE_REPLIES", "1") == "1"
VOICE_COOLDOWN = int(os.getenv("VOICE_COOLDOWN", "300"))  # ek user ko 5 min me max ek voice
VOICE_NAME = os.getenv("VOICE_NAME", "hi-IN-SwaraNeural")
MAX_MESSAGE_AGE = 180  # Render cold-start me message late pahunchta hai, isliye 15s bahut kam tha

GROQ_API_KEYS = []
all_keys_str = os.getenv("GROQ_API_KEYS", "")
if all_keys_str:
    GROQ_API_KEYS = [k.strip() for k in all_keys_str.split(",") if k.strip()]
else:
    for i in range(1, 101):
        key = os.getenv(f"GROQ_API_KEY_{i}")
        if key and key.strip():
            GROQ_API_KEYS.append(key.strip())

if not BOT_TOKEN or not GROQ_API_KEYS:
    raise ValueError("BOT_TOKEN aur kam se kam ek GROQ_API_KEY set karna zaroori hai!")

logger.info(f"✅ Total {len(GROQ_API_KEYS)} Groq API keys loaded hui!")
clients = [AsyncGroq(api_key=key, max_retries=0) for key in GROQ_API_KEYS]

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


# ============================================================
# GROQ KEY MANAGER
# ============================================================
RPM_SAFE_LIMIT = 28
TPM_SAFE_LIMIT = 7500
DAILY_REQUEST_LIMIT = 950
DAILY_TOKEN_LIMIT = 190000
MAX_CONCURRENT_REQUESTS = 30
MIN_DISPATCH_GAP = 0.1
DISPATCH_JITTER = 0.05

_key_cooldowns = {}
_key_usage = {i: [] for i in range(len(clients))}  # har entry mutable [time, tokens]
daily_requests = [0] * len(clients)
daily_tokens = [0] * len(clients)
last_reset_day = time.strftime("%Y%m%d")
_key_429_counts = [0] * len(clients)
_concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
_global_request_lock = asyncio.Lock()
_last_dispatch_time = 0.0


def _clean_key_usage(idx, now):
    _key_usage[idx] = [e for e in _key_usage[idx] if now - e[0] < 60]


def key_has_room(idx, est=700) -> bool:
    now = time.time()
    _clean_key_usage(idx, now)
    entries = _key_usage[idx]
    if len(entries) >= RPM_SAFE_LIMIT:
        return False
    if sum(e[1] for e in entries) + est > TPM_SAFE_LIMIT:
        return False
    if daily_tokens[idx] + est > DAILY_TOKEN_LIMIT:
        return False
    if daily_requests[idx] + 1 > DAILY_REQUEST_LIMIT:
        return False
    return True


def reset_daily_if_new_day():
    global last_reset_day
    today = time.strftime("%Y%m%d")
    if today != last_reset_day:
        for i in range(len(clients)):
            daily_requests[i] = 0
            daily_tokens[i] = 0
            _key_429_counts[i] = 0
            if _key_cooldowns.get(i, 0) - time.time() > 3600:
                _key_cooldowns.pop(i, None)
        last_reset_day = today
        logger.info(f"🔄 Naya din shuru hua, {len(clients)} keys reset")


def pre_record_key_usage(idx, est):
    reset_daily_if_new_day()
    entry = [time.time(), est]
    _key_usage[idx].append(entry)
    daily_requests[idx] += 1
    daily_tokens[idx] += est
    return entry


def settle_key_usage(idx, entry, actual):
    # entry in-place update hota hai, isliye list prune hone par bhi galat entry nahi chhooti
    daily_tokens[idx] += actual - entry[1]
    entry[1] = actual


def set_key_cooldown(idx, seconds=60):
    _key_cooldowns[idx] = time.time() + seconds


def handle_429_error(idx, err):
    msg = str(err).lower()
    retry_after = None
    try:
        retry_after = float(err.response.headers.get("retry-after"))
    except Exception:
        pass
    _key_429_counts[idx] += 1
    is_daily = (any(k in msg for k in ("per day", "tpd", "rpd", "daily", "exhausted"))
                or daily_tokens[idx] >= DAILY_TOKEN_LIMIT or daily_requests[idx] >= DAILY_REQUEST_LIMIT)
    if is_daily:
        now = time.time()
        secs = int(retry_after + 5) if retry_after else int((now // 86400 + 1) * 86400 - now)
        set_key_cooldown(idx, seconds=secs)
        logger.warning(f"🔴 Key {idx+1} DAILY LIMIT, {secs}s cooldown")
    else:
        secs = min(int(retry_after + 1), 120) if retry_after else 30
        set_key_cooldown(idx, seconds=secs)
        logger.warning(f"🚫 Key {idx+1} 429 burst, {secs}s cooldown")


def pick_best_key(now, est=700, exclude=()):
    best_idx, best_score = None, None
    for i in range(len(clients)):
        if i in exclude or _key_cooldowns.get(i, 0) > now or not key_has_room(i, est):
            continue
        score = len(_key_usage[i]) * 1000 + daily_tokens[i]
        if best_score is None or score < best_score:
            best_idx, best_score = i, score
    return best_idx


async def throttle_dispatch():
    global _last_dispatch_time
    async with _global_request_lock:
        wait = MIN_DISPATCH_GAP - (time.time() - _last_dispatch_time)
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, DISPATCH_JITTER))
        _last_dispatch_time = time.time()


async def call_groq(messages, *, model, temperature=0.8, max_tokens=400, timeout=20.0,
                    reasoning="low", top_p=None, max_attempts=4, tag="chat"):
    """Ek hi jagah se saari Groq calls (chat, summary, fun commands). Text return karta hai ya None."""
    est = sum(len(m["content"]) for m in messages) // 3 + 250
    tried = set()
    for _ in range(min(max_attempts, len(clients))):
        idx = pick_best_key(time.time(), est, tried)
        if idx is None:
            break
        tried.add(idx)
        entry = pre_record_key_usage(idx, est)
        t0 = time.time()
        try:
            async with _concurrency_semaphore:
                await throttle_dispatch()
                kwargs = dict(model=model, messages=messages, temperature=temperature,
                              max_completion_tokens=max_tokens, timeout=timeout)
                if top_p:
                    kwargs["top_p"] = top_p
                if model.startswith("openai/gpt-oss"):
                    # reasoning tokens bhi max tokens me ginte hain, isliye low rakha (warna reply khali/kata aata tha)
                    kwargs.update(reasoning_effort=reasoning, include_reasoning=False)
                resp = await clients[idx].chat.completions.create(**kwargs)
            usage = getattr(resp, "usage", None)
            actual = getattr(usage, "total_tokens", None) or est
            settle_key_usage(idx, entry, actual)
            _key_429_counts[idx] = 0
            text = (resp.choices[0].message.content or "").strip()
            if text:
                logger.info(f"🔑 Key {idx+1}/{len(clients)} ✓ [{tag}] {model.split('/')[-1]} | {actual} tok | {time.time() - t0:.1f}s")
                return text
            logger.warning(f"⚠️ Key {idx+1} [{tag}] khali reply (finish={getattr(resp.choices[0], 'finish_reason', '?')}), agli key try")
        except Exception as e:
            s = str(e).lower()
            if "429" in s or "rate_limit" in s:
                handle_429_error(idx, e)
            elif "timeout" in s or "timed out" in s:
                set_key_cooldown(idx, 20)
                logger.warning(f"⏰ Key {idx+1} timeout")
            elif "400" in s or "parse" in s:
                logger.warning(f"⚠️ Key {idx+1} 400/parse error: {str(e)[:120]}")
            else:
                set_key_cooldown(idx, 15)
                logger.error(f"❌ Key {idx+1} error: {e}")
    logger.warning("⏳ Koi key available nahi / sab fail.")
    return None


# ============================================================
# EMOJI / TEXT CLEANING
# ============================================================
CHAT_PREMIUM_EMOJIS = {
    "🙏": "6059691494464294436",
    "😪": "5998863839746070871",
    "🔥": "5998905410734529922",
    "😦": "6001118779180849174",
    "🤥": "6037218073793007354",
    "😴": "5215224877379779914",
    "❤️": "6077843336222219470",
    "🥰": "6077912377821501374",
    "🤗": "6075816433781054015",
    "🤩": "6075359633944352264",
    "😊": "5348497377567206779",
    "👑": "6156698214222272110",
}
# Prompt me sirf wahi emojis jo premium map me hain (pehle ✅🍼⚡💬 bol deta tha jo hamesha delete ho jaate the)
PROMPT_EMOJIS = [e for e in CHAT_PREMIUM_EMOJIS if e != "👑"]

_EMOJI_FALLBACK_MAP = {
    "😀": "😊", "😃": "😊", "😄": "😊", "😁": "😊", "🙂": "😊", "☺": "😊",
    "😍": "🥰", "😘": "🥰", "💕": "❤️", "💖": "❤️", "💗": "❤️", "💓": "❤️", "💞": "❤️",
    "😢": "😪", "😭": "😪", "🥺": "😪",
    "😏": "🤥", "😜": "🤥", "😉": "🤥", "😂": "🤥", "🤣": "🤥", "🤔": "🤥", "😎": "🤥",
    "🙌": "🙏", "🤲": "🙏", "💤": "😴", "😮": "😦", "😲": "😦", "😳": "😦",
}
_SINGLE_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F1E6-\U0001F1FF\u2B50\u2B55][\uFE0F]?")
_ALLOWED_NORM = {k.replace("\uFE0F", ""): k for k in CHAT_PREMIUM_EMOJIS}
_last_used_emoji = {}


def sanitize_reply_emojis(text: str, user_id: int | None = None) -> str:
    if not text:
        return text
    text = text.replace("\u200d", "")
    kept = {"done": False}

    def _replace(m):
        base = m.group(0).replace("\uFE0F", "")
        base = _EMOJI_FALLBACK_MAP.get(base, base).replace("\uFE0F", "")
        key = _ALLOWED_NORM.get(base)
        if not key or kept["done"]:
            return ""
        kept["done"] = True
        return key

    result = _SINGLE_EMOJI.sub(_replace, text)
    result = re.sub(r"[ \t]{2,}", " ", result).strip()
    if user_id is not None:
        used = next((e for e in CHAT_PREMIUM_EMOJIS if e in result), None)
        if used:
            if _last_used_emoji.get(user_id) == used:
                # wahi emoji lagatar? hata do (random doosra lagane se matlab bigad jaata tha, jaise chill pe 😪)
                result = re.sub(r"[ \t]{2,}", " ", result.replace(used, "", 1)).strip()
                _last_used_emoji.pop(user_id, None)
            else:
                _last_used_emoji[user_id] = used
    return result


_POSSESSIVE = {"mera", "tera", "tumhara", "uska", "apna", "mere", "tere", "tumhare", "uske", "apne", "meri", "teri", "tumhari", "uski", "aapka", "aapke"}
_MALE = r"(?:bhai|bhaiya|bro|bruh|dude|boss|buddy)"


def strip_hallucinated_patterns(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"\s*\([^)]{25,120}\)\s*", " ", text).strip()
    text = re.sub(r"\s*\[[^\]]{15,120}\]\s*", " ", text).strip()
    # sirf 'address' wale bhai/bro hatao, "mera bhai" jaise real use nahi
    text = re.sub(rf"(?i)\b(hey|hi|hii|hello|oye|yo)[\s,]+{_MALE}\b[\s,]*", r"\1 ", text)
    text = re.sub(rf"(?i)^{_MALE}\b[\s,]+", "", text)
    text = re.sub(rf"(?i),\s*{_MALE}\b", "", text)

    def _trail(m):
        prev = re.findall(r"[a-zA-Z]+", text[:m.start()])
        return m.group(0) if prev and prev[-1].lower() in _POSSESSIVE else ""
    text = re.sub(rf"(?i)\s+{_MALE}\b(?=[\s!?.]*$)", _trail, text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def remove_duplicated_reply_content(text: str) -> str:
    if not text or len(text) < 20:
        return text
    s = text.strip()
    n = len(s)
    mid = n // 2
    for sp in range(max(mid - 15, 1), min(mid + 16, n)):
        a, b = s[:sp].strip(), s[sp:].strip()
        if len(a) > 10 and a == b:
            return a
    sentences = re.split(r"(?<=[.?!])\s+", s)
    out = []
    for x in sentences:
        x = x.strip()
        if not x:
            continue
        if out and out[-1].lower().rstrip(".?!") == x.lower().rstrip(".?!"):
            continue
        out.append(x)
    return " ".join(out)


_STOPWORDS = {"aaj", "kaisa", "kaisi", "wala", "wali", "phir", "fir", "lekin", "par", "yahan", "wahan", "sirf", "bahut", "bohot",
              "theek", "acha", "accha", "arre", "hmm", "haan", "chalo", "chal", "raha", "rahi", "rahe", "karo", "karte", "karna",
              "kiya", "hota", "hoti", "lagta", "lagti", "sach", "mujhe", "tumko", "humko", "hai", "ho", "hoon", "tha", "thi", "the", "aur", "ka", "ki", "ke", "ko", "se", "me", "mein", "toh", "to",
              "bhi", "kya", "kaise", "tum", "tumhe", "mera", "meri", "mere", "tumhara", "tumhari", "and", "for", "you",
              "your", "my", "just", "abhi", "thoda", "yaar", "nahi", "bas", "kuch", "koi", "hum", "main"}


def _content_words(t):
    return {w for w in re.findall(r"[a-zA-Z]+", t.lower()) if len(w) > 2 and w not in _STOPWORDS}


def reply_repeats_recent_topic(reply, history, min_shared=4) -> bool:
    if not reply or not history:
        return False
    recent = [m.get("content", "") for m in history[-6:] if m.get("role") == "assistant"]
    new = _content_words(reply)
    return len(new) >= min_shared and any(len(new & _content_words(o)) >= min_shared for o in recent)


def cap_reply_sentences(text: str, max_sentences: int = 3, max_chars: int = 420) -> str:
    if not text:
        return text
    units = []
    for line in (ln.strip() for ln in text.split("\n") if ln.strip()):
        units.extend(p.strip() for p in re.split(r"(?<=[.?!])\s+", line) if p.strip())
    out = " ".join(units[:max_sentences])
    if len(out) > max_chars:
        cut = out[:max_chars]
        out = cut[:max(cut.rfind(" "), 40)].rstrip(",;:") + "..."
    return out


def clean_reply_text(text: str, user_id: int | None = None) -> str:
    if not text:
        return text
    text = re.sub(r"(?im)^(user says|need reply|language|context)\s*:.*$", "", text).strip()
    text = re.sub(r"\s*[—–]\s*", ", ", text)          # em/en dash -> comma (hyphen wale words safe)
    text = re.sub(r"\s+-\s+", ", ", text)
    text = re.sub(r"^[\-•*,\s]+", "", text)
    text = re.sub(r"[,\s]+$", "", text) if text.endswith(",") else text.strip()
    text = re.sub(r" {2,}", " ", text)
    return sanitize_reply_emojis(text, user_id=user_id)


def clean_leaked_template_fragments(reply: str) -> str:
    if not reply:
        return reply
    c = re.sub(r"\s*\[[^\]]{0,60}\]\s*$", "", reply).strip()
    c = re.sub(r"\s*\[[^\[\]]{0,60}$", "", c).strip()
    return c or reply


def strip_echoed_user_message(reply: str, user_message: str) -> str:
    """Sirf tab hatao jab model ne user ka POORA lamba message copy karke newline/colon ke baad jawab likha ho.
    (Pehle 'Employees' jaise 1 word pe bhi reply ka pehla word kaat deta tha: 'Employees ke baare me...' -> 'ke baare me...')"""
    if not reply or not user_message:
        return reply
    r, u = reply.strip(), user_message.strip()
    if len(u.split()) < 4 or not r.lower().startswith(u.lower()):
        return reply
    rest = r[len(u):]
    if rest[:1] in ("\n", ":", "|") or rest.lstrip()[:1] in ("-", "—", ":"):
        rem = rest.lstrip("-—:|,.\n ").strip()
        return rem or reply
    return reply


BOT_LIKE_PHRASES = ["main aapki madad", "main aapki sahayata", "i am an ai", "i am a bot", "as an ai",
                    "as a language model", "language model", "i'm an ai", "kaise madad kar sakti",
                    "i can't help", "i cannot help", "can't assist", "cannot assist", "i can't comply", "i'm sorry, but", "sorry, but i can't", "i won't be able to"]


def has_devanagari(t: str) -> bool:
    return sum(1 for ch in t if "\u0900" <= ch <= "\u097F") >= 3


def reply_reuses_stuck_word(reply, history) -> bool:
    """Pichle 3 replies me jo kaam ka word 2+ baar aaya, wo naye reply me phir aaye toh atka hua (coffee/plan jaisa)."""
    recent = [m.get("content", "") for m in (history or [])[-6:] if m.get("role") == "assistant"][-3:]
    if len(recent) < 2:
        return False
    counts = {}
    for r in recent:
        for w in {w for w in _content_words(r) if len(w) >= 4}:
            counts[w] = counts.get(w, 0) + 1
    stuck = {w for w, c in counts.items() if c >= 2}
    return bool(stuck & _content_words(reply))


_TU_RE = re.compile(r"(?i)\b(?:tu|tum|tera|teri|tere|tujhe|tujhse|tumhara|tumhari|tumhare|tumhe|tumko|tumse|tumne|tumlog)\b"
                    r"|\b(?:kaise|kaisi|kahan)\s+ho\b|\b(?:rahe|rahi)\s+ho\b|\baap\b[^.?!]{0,25}\bho\b")
_OVERUSED = {"coffee", "cofee", "coffe", "cafe", "plan", "plans", "music", "gaane", "gaana", "gana", "song", "songs",
             "netflix", "movie", "movies", "rom"}


def reply_uses_overused_topic(reply, user_message, history) -> bool:
    said = set(re.findall(r"[a-z]+", reply.lower())) & _OVERUSED
    if not said:
        return False
    user_text = " ".join([user_message] + [m.get("content", "") for m in (history or [])[-6:] if m.get("role") == "user"])
    return bool(said - set(re.findall(r"[a-z]+", user_text.lower())))  # user ne khud na bola ho toh hi reject


def reject_reason(reply, user_message, history, allow_tu=False):
    if not reply:
        return "empty"
    low = reply.lower()
    if any(p in low for p in BOT_LIKE_PHRASES):
        return "bot_like"
    if has_devanagari(reply) and not has_devanagari(user_message):
        return "devanagari"
    if not allow_tu and _TU_RE.search(reply):
        return "tu_form"
    if reply_uses_overused_topic(reply, user_message, history):
        return "overused"
    if reply_repeats_recent_topic(reply, history) or reply_reuses_stuck_word(reply, history):
        return "repeat"
    return None


def humanize_reply(text: str) -> str:
    """Insaan aakhir me full stop nahi lagate aur hamesha Capital se shuru nahi karte."""
    t = (text or "").strip()
    t = re.sub(r"(?<!\.)\.(?!\.)(\s*[^\w\s]*)$", r"\1", t).strip()
    if t and t[0].isupper() and not re.match(r"^(I|I'm|I'll|OK|AI|Python|Telegram)\b", t) and random.random() < 0.55:
        t = t[0].lower() + t[1:]
    return t


def postprocess_reply(text: str, user_message: str, user_id: int) -> str:
    text = re.sub(r"<think[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<think[\s\S]*", "", text, flags=re.IGNORECASE).strip()
    text = text.replace("\u2011", "-").replace("\u00a0", " ").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*(.*?)\*(?!\w)", r"\1", text)
    text = text.strip().strip("`")
    text = strip_echoed_user_message(text, user_message)
    text = clean_leaked_template_fragments(text)
    text = strip_hallucinated_patterns(text)
    text = clean_reply_text(text, user_id=user_id)
    text = remove_duplicated_reply_content(text)
    return humanize_reply(cap_reply_sentences(text))


# ============================================================
# DATABASE (pool + retry; blocking calls ko asyncio.to_thread se chalao)
# ============================================================
_pool = None


def get_db_conn():
    # purane modules (broadcast.py wagairah) ke liye raw connection, pehle jaisa
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL)


def _get_pool():
    global _pool
    if _pool is None and DATABASE_URL:
        _pool = pg_pool.ThreadedConnectionPool(1, int(os.getenv("DB_POOL_MAX", "8")), DATABASE_URL, connect_timeout=10)
    return _pool


@contextmanager
def db_cursor():
    p = _get_pool()
    if p is None:
        raise RuntimeError("DATABASE_URL set nahi hai")
    conn = p.getconn()
    bad = bool(conn.closed)
    try:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        bad = True
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            bad = True
        raise
    finally:
        p.putconn(conn, close=bad)


def db_exec(sql, params=(), fetch=None):
    if not DATABASE_URL:
        return None
    for attempt in (1, 2):
        try:
            with db_cursor() as c:
                c.execute(sql, params)
                if fetch == "one":
                    return c.fetchone()
                if fetch == "all":
                    return c.fetchall()
                return None
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if attempt == 2:
                raise


adb = asyncio.to_thread


def init_db():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL nahi mila, PostgreSQL skip ho raha hai.")
        return
    try:
        stmts = [
            "CREATE TABLE IF NOT EXISTS user_memory (user_id BIGINT PRIMARY KEY, summary TEXT, updated_at REAL)",
            "CREATE TABLE IF NOT EXISTS broadcast_users (user_id BIGINT PRIMARY KEY, started_at REAL)",
            "CREATE TABLE IF NOT EXISTS active_groups (chat_id BIGINT PRIMARY KEY, title TEXT, added_at REAL)",
            "CREATE TABLE IF NOT EXISTS conversation_history (user_id BIGINT PRIMARY KEY, history_json TEXT, updated_at REAL)",
            "CREATE TABLE IF NOT EXISTS saved_stickers (file_unique_id TEXT PRIMARY KEY, file_id TEXT, emoji TEXT, set_name TEXT)",
            "CREATE TABLE IF NOT EXISTS bio_warnings (user_id BIGINT PRIMARY KEY, checked INTEGER DEFAULT 0, warn_count INTEGER DEFAULT 0, updated_at REAL)",
            "CREATE TABLE IF NOT EXISTS user_state (user_id BIGINT PRIMARY KEY, last_seen DOUBLE PRECISION, last_checkin DOUBLE PRECISION DEFAULT 0, blocked INTEGER DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS user_gender (user_id BIGINT PRIMARY KEY, gender TEXT, source TEXT)",
        ]
        for s in stmts:
            db_exec(s)
        if os.path.exists("stickers.json"):
            try:
                with open("stickers.json", "r", encoding="utf-8") as f:
                    old = json.load(f)
                for st in old or []:
                    db_exec("INSERT INTO saved_stickers (file_unique_id, file_id, emoji, set_name) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (st.get("file_unique_id"), st.get("file_id"), st.get("emoji"), st.get("set_name")))
                os.rename("stickers.json", "stickers.json.bak")
                logger.info(f"✅ Migrated {len(old or [])} stickers to PostgreSQL!")
            except Exception as e:
                logger.error(f"Sticker migration error: {e}")
        logger.info("✅ PostgreSQL Connected!")
    except Exception as e:
        logger.error(f"DB Connection error: {e}")


def get_user_summary(user_id: int) -> str:
    try:
        row = db_exec("SELECT summary FROM user_memory WHERE user_id=%s", (user_id,), "one")
        return row[0] if row and row[0] else ""
    except Exception as e:
        logger.error(f"❌ DB Fetch Failed for {user_id}: {e}")
        return ""


def save_user_summary(user_id: int, summary: str):
    try:
        t = time.time()
        db_exec("INSERT INTO user_memory (user_id, summary, updated_at) VALUES (%s,%s,%s) "
                "ON CONFLICT (user_id) DO UPDATE SET summary=%s, updated_at=%s", (user_id, summary, t, summary, t))
    except Exception as e:
        logger.error(f"❌ DB Save Failed for {user_id}: {e}")


def delete_user_data(user_id: int):
    try:
        db_exec("DELETE FROM user_memory WHERE user_id=%s", (user_id,))
        db_exec("DELETE FROM conversation_history WHERE user_id=%s", (user_id,))
        db_exec("DELETE FROM user_gender WHERE user_id=%s", (user_id,))
    except Exception as e:
        logger.error(f"delete_user_data fail {user_id}: {e}")


def load_conversation_history_from_db(user_id: int) -> list:
    try:
        row = db_exec("SELECT history_json FROM conversation_history WHERE user_id=%s", (user_id,), "one")
        return json.loads(row[0]) if row and row[0] else []
    except Exception as e:
        logger.error(f"❌ History DB Fetch Failed for {user_id}: {e}")
        return []


def save_conversation_history_to_db(user_id: int, history: list):
    try:
        j, t = json.dumps(history), time.time()
        db_exec("INSERT INTO conversation_history (user_id, history_json, updated_at) VALUES (%s,%s,%s) "
                "ON CONFLICT (user_id) DO UPDATE SET history_json=%s, updated_at=%s", (user_id, j, t, j, t))
    except Exception as e:
        logger.error(f"❌ History DB Save Failed for {user_id}: {e}")


def _get_bio_state_sync(user_id: int) -> tuple:
    try:
        row = db_exec("SELECT checked, warn_count FROM bio_warnings WHERE user_id=%s", (user_id,), "one")
        return (bool(row[0]), row[1]) if row else (False, 0)
    except Exception as e:
        logger.warning(f"bio_state fetch fail {user_id}: {e}")
        return (False, 0)


async def get_bio_state_async(user_id: int) -> tuple:
    return await adb(_get_bio_state_sync, user_id)


def _save_bio_state_sync(user_id: int, checked: bool, warn_count: int):
    try:
        t = time.time()
        db_exec("INSERT INTO bio_warnings (user_id, checked, warn_count, updated_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (user_id) DO UPDATE SET checked=%s, warn_count=%s, updated_at=%s",
                (user_id, int(checked), warn_count, t, int(checked), warn_count, t))
    except Exception as e:
        logger.warning(f"bio_state save fail {user_id}: {e}")


async def save_bio_state_async(user_id: int, checked: bool, warn_count: int):
    await adb(_save_bio_state_sync, user_id, checked, warn_count)


def _save_broadcast_user_sync(user_id: int):
    try:
        db_exec("INSERT INTO broadcast_users (user_id, started_at) VALUES (%s,%s) ON CONFLICT (user_id) DO NOTHING",
                (user_id, time.time()))
    except Exception:
        pass


async def save_broadcast_user_async(user_id: int):
    if DATABASE_URL:
        await adb(_save_broadcast_user_sync, user_id)


def save_active_group(chat_id: int, title: str):
    try:
        db_exec("INSERT INTO active_groups (chat_id, title, added_at) VALUES (%s,%s,%s) "
                "ON CONFLICT (chat_id) DO UPDATE SET title=%s", (chat_id, title, time.time(), title))
    except Exception:
        pass


async def save_active_group_async(chat_id: int, title: str):
    if DATABASE_URL:
        await adb(save_active_group, chat_id, title)


def delete_active_group(chat_id: int):
    try:
        db_exec("DELETE FROM active_groups WHERE chat_id=%s", (chat_id,))
    except Exception as e:
        logger.warning(f"delete_active_group fail for {chat_id}: {e}")


async def delete_active_group_async(chat_id: int):
    if DATABASE_URL:
        await adb(delete_active_group, chat_id)


def get_all_active_groups() -> list:
    try:
        return db_exec("SELECT chat_id, title FROM active_groups", (), "all") or []
    except Exception as e:
        logger.warning(f"get_all_active_groups fail: {e}")
        return []


def touch_user_state(user_id: int, now: float):
    try:
        db_exec("INSERT INTO user_state (user_id, last_seen, last_checkin, blocked) VALUES (%s,%s,0,0) "
                "ON CONFLICT (user_id) DO UPDATE SET last_seen=%s, blocked=0", (user_id, now, now))
    except Exception as e:
        logger.warning(f"touch_user_state fail: {e}")


def get_last_seen(user_id: int):
    try:
        row = db_exec("SELECT last_seen FROM user_state WHERE user_id=%s", (user_id,), "one")
        return row[0] if row else None
    except Exception:
        return None


def get_gender_db(user_id: int):
    try:
        return db_exec("SELECT gender, source FROM user_gender WHERE user_id=%s", (user_id,), "one")
    except Exception:
        return None


def save_gender_db(user_id: int, gender: str, source: str):
    try:
        # guess kabhi user ke khud bataye gender ko overwrite nahi karega
        db_exec("INSERT INTO user_gender (user_id, gender, source) VALUES (%s,%s,%s) ON CONFLICT (user_id) DO UPDATE "
                "SET gender=EXCLUDED.gender, source=EXCLUDED.source WHERE user_gender.source <> 'stated' OR EXCLUDED.source = 'stated'",
                (user_id, gender, source))
    except Exception as e:
        logger.warning(f"save_gender fail: {e}")


def get_checkin_candidates(limit=5) -> list:
    now = time.time()
    try:
        rows = db_exec(
            "SELECT s.user_id FROM user_state s JOIN user_memory m ON m.user_id=s.user_id "
            "WHERE s.blocked=0 AND s.last_seen < %s AND s.last_seen > %s AND s.last_checkin < s.last_seen "
            "ORDER BY s.last_seen DESC LIMIT %s", (now - 2 * 86400, now - 14 * 86400, limit), "all")
        return [r[0] for r in rows or []]
    except Exception as e:
        logger.warning(f"checkin candidates fail: {e}")
        return []


def mark_checkin(user_id: int, blocked: bool = False):
    try:
        db_exec("UPDATE user_state SET last_checkin=%s, blocked=%s WHERE user_id=%s",
                (time.time(), int(blocked), user_id))
    except Exception:
        pass


# ============================================================
# MEMORY / SUMMARY
# ============================================================
def _parse_summary_fields(summary: str) -> dict:
    fields = {}
    for line in (summary or "").split("\n"):
        if ":" in line:
            label, _, value = line.partition(":")
            fields[label.strip().lower()] = value.strip()
    return fields


_EMPTY_VALUES = ("not shared", "none", "")


def _protect_permanent_fields(new_summary: str, old_summary: str) -> str:
    if not old_summary:
        return new_summary
    old_f, new_f = _parse_summary_fields(old_summary), _parse_summary_fields(new_summary)
    lines = new_summary.split("\n")
    for i, line in enumerate(lines):
        if ":" not in line:
            continue
        label = line.split(":", 1)[0].strip().lower()
        if label not in ("naam", "relation", "facts"):
            continue
        if new_f.get(label, "").lower() in _EMPTY_VALUES and old_f.get(label, "").lower() not in _EMPTY_VALUES:
            lines[i] = f"{line.split(':', 1)[0]}: {old_f[label]}"
    return "\n".join(lines)


def _apply_telegram_name_fallback(summary: str, telegram_name: str | None) -> str:
    if not summary:
        return summary
    lines = summary.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("naam:"):
            value = line.split(":", 1)[1].strip()
            if value.lower() in _EMPTY_VALUES and telegram_name:
                lines[i] = f"Naam: {telegram_name} (Telegram name, user ne khud confirm nahi kiya)"
            break
    return "\n".join(lines)


async def generate_summary(user_id: int, history: list, telegram_name: str | None = None):
    if len(history) < 4 or not DATABASE_URL:
        return
    try:
        old_summary = await adb(get_user_summary, user_id)
        chat_text = "\n".join(f"{'User' if m.get('role') == 'user' else 'Sneha'}: {m.get('content', '')}" for m in history[-14:])
        prompt = f"""Tu ek memory bot hai. User ke baare me REAL aur kaam ki cheezein save kar. Faltu topics mat bana.

PURANI MEMORY: {old_summary or "(Kuch nahi)"}
NAYI CHAT:
{chat_text}

EXACT FORMAT me 4 lines do:
Naam: <sirf agar user ne khud bataya, warna "Not shared">
Relation: <user ne jo relation bataya (sister, friend, admin...), warna "Not shared">
Facts: <user ki permanent baatein: kaam/padhai, hobby, pasand, important events, promises. PURANI facts rakho aur nayi jodo, max 5 chhote points ';' se alag>
Ongoing: <abhi chal rahi cheezein jo baad me poochhne layak hon (koi problem, exam, trip, project, bimari ke bajay sirf kaam wali). Jo khatam ho gayi ho wo hata do. Kuch nahi toh "None">

Rules: Hinglish Roman script. Sirf wahi jo user ne khud bataya, koi assumption nahi. Phone number, password, address, card ya OTP KABHI save mat karna."""
        text = await call_groq([{"role": "user", "content": prompt}], model=SUMMARY_MODEL,
                               temperature=0.2, max_tokens=500, timeout=25.0, tag="summary")
        if not text:
            return
        low = text.lower()
        if (len(text) > 800 or has_devanagari(text) or "purani memory" in low or "nayi chat" in low
                or not all(f"{k}:" in low for k in ("naam", "relation", "facts"))):
            logger.warning(f"⚠️ Garbage summary skip kiya for {user_id}")
            return
        text = _apply_telegram_name_fallback(_protect_permanent_fields(text, old_summary), telegram_name)
        await adb(save_user_summary, user_id, text)
        logger.info(f"📝 User {user_id} summary updated")
    except Exception as e:
        logger.error(f"🔥 Summary crash for {user_id}: {e}")


# ============================================================
# PERSONALITY
# ============================================================
SYSTEM_PROMPT = f"""Tum Sneha ho, 21 saal ki pyaari, smart, thodi shararati ladki, Telegram pe chat kar rahi ho. Tum AI/bot nahi ho; koi pooche toh haske taal do.

Baat karne ka tareeka:
- Hamesha "aap" se, izzat aur pyaar se, jaise badon se baat karte hain: "kaise hain aap?", "aap kya kar rahe hain?". Kabhi tu, tum, tera nahi.
- WhatsApp jaisi chhoti Hinglish (Roman script), zyadatar 1 line, user jitna likhe utna hi. Bina full stop, natural words ("arre", "hmm", "haan ji").
- Har baar sawaal ya tease nahi: kabhi sirf reaction, kabhi apni raay. Dry reply (hm, nhi) pe pyaar se tok do ya baat badlo.
- Apni taraf se coffee, plan, music, gaane, movie, netflix ki baat kabhi mat uthao. Pichli baat dobara mat dohrao. Bina maange advice nahi. Milne, call ya number ki baat nahi.
- Doubt poochhe toh sahi aur saaf jawab (2-3 lines), pata na ho toh "pata nahi".
- Gandi/18+ ya badtameezi pe real ladki ki tarah daanto ("aisi baat mat kijiye, ye galat hai"), kabhi "I can't help" nahi.
- Memory me jo likha hai wo user ki life hai, tumhari nahi.
- 0 ya 1 emoji, matlab ke hisaab se: {' '.join(PROMPT_EMOJIS)}. Koi markdown ya dash nahi.

Examples:
user: hi -> arre hello ji, kaise hain aap 😊
user: hm -> hm? bas hm? kuch bolo na
user: python me list reverse kaise karte -> list.reverse() se wahin ulta hota hai, ya list[::-1] se nayi list milti hai"""


_EXPLICIT_TOKENS = {"sex", "sexy", "nude", "nudes", "boobs", "boob", "chut", "lund", "lauda", "chudai", "horny", "pussy",
                    "dick", "cock", "porn", "xxx", "nangi", "nanga", "chodna", "chodu"}
_EXPLICIT_PHRASES = ("send nudes", "nude pic", "sex karna", "sex karo", "fuck me", "have sex", "kapde utaro", "kapde utar",
                     "raat bitao", "bed pe aa", "hot pic", "sexy pic", "ek raat")
_scold_log = {}  # user_id -> pichle 1 ghante ke timestamps
SCOLD_REPLIES = [
    "Excuse me? Aap tameez se baat kijiye, main aisi ladki nahi hoon 😦",
    "Chhi, ye kaisi baatein hain? Thodi sharam kijiye, aage se aisi baat mat kijiyega 😪",
    "Dekhiye, ye sab mujhse nahi chalega. Izzat se baat karenge toh hi baat karungi 🙏",
    "Arey ye kya keh rahe hain aap? Bahut galat baat hai, ise yahin band kijiye 😦",
    "Mujhe ye bilkul pasand nahi aaya. Ladkiyon se aise baat nahi karte, samjhe aap? 😪",
    "Aisi baatein apne paas rakhiye, normal baat karni hai toh boliye 🙏",
]


def is_explicit(text: str) -> bool:
    low = text.lower()
    return bool(set(re.findall(r"[a-z]+", low)) & _EXPLICIT_TOKENS) or any(p in low for p in _EXPLICIT_PHRASES)


def register_scold(user_id: int) -> int:
    now = time.time()
    log = [t for t in _scold_log.get(user_id, []) if now - t < 3600] + [now]
    _scold_log[user_id] = log
    return len(log)


_PROMO_PHRASES = ("dm kro", "dm karo", "d//m", "dm me", "dm krna", "gf chahiye", "gf chahia", "bf chahiye", "inbox me", "inbox kro",
                  "promotion", "follow kro", "join kro", "join karo")


def build_behavior_hints(user_message: str, history, gap_seconds, extra: str = "") -> str:
    hints = []
    words = user_message.split()
    low = user_message.strip().lower()
    if len(words) <= 3:
        hints.append("User ne chhota likha hai, tum bhi bahut chhota (3-8 words) reply do.")
    elif len(words) >= 25:
        hints.append("User ne detail me likha hai, tum bhi thoda detail me, par phir bhi chat jaisa.")
    prev_users = [m["content"] for m in (history or []) if m.get("role") == "user"][-3:]
    if extra:
        hints.append(extra)
    if low and low in [p.strip().lower() for p in prev_users]:
        hints.append("User ne wahi message repeat kiya hai, mazaak me tok do.")
    elif len(words) <= 1 and "?" not in low:
        hints.append("User ne sirf ek hi word likha, halka tease karo ya baat aage badhao.")
    if len(prev_users) >= 3 and all(len(p.split()) <= 2 for p in prev_users):
        hints.append("User lagatar chhote lazy replies de raha hai, pyaar se toko aur ek interesting sawaal pucho.")
    if re.search(r"\?\s*$", user_message) or re.match(r"(?i)^(kya|kaise|kyun|kyu|kab|kaun|kitna|kahan|how|what|why|when|who|which)\b", low):
        hints.append("User kuch poochh raha hai: pehle seedha sahi jawab do, phir apna style.")
    if gap_seconds is not None:
        if gap_seconds > 2 * 86400:
            hints.append(f"User {int(gap_seconds // 86400)} din baad aaya hai. Jawab ke baad memory se ek sweet catch-up sawaal pooch sakti ho.")
        elif gap_seconds > 6 * 3600:
            hints.append("User kaafi der baad aaya hai, halka sa 'kahan gayab the' wala tease kar sakti ho.")
    last_ai = [m.get("content", "") for m in (history or []) if m.get("role") == "assistant"][-2:]
    if len(last_ai) == 2 and all(re.sub(r"[^\w?]+$", "", x).endswith("?") for x in last_ai):
        hints.append("Pichle 2 replies sawaal pe khatam hue, is baar sawaal mat poochho.")
    if any(p in low for p in _PROMO_PHRASES):
        hints.append("User DM/gf-bf/promotion ke liye bula raha hai (spam jaisa). Mazaak me tok do ya halka tease karo, uski help/advice mat karo, usse DM me jaane ka rasta mat dikhao.")
    hr = datetime.now(IST).hour
    if 1 <= hr < 5:
        hints.append("Abhi raat ke 1-5 baje hain, user ko jaagne pe tease kar sakti ho (har baar nahi).")
    return ("\n[HINTS: " + " ".join(hints) + "]") if hints else ""


def get_current_context() -> str:
    n = datetime.now(IST)
    return f"Current time: {n.strftime('%I:%M %p')} IST, Day: {n.strftime('%A')}, Date: {n.strftime('%d %B %Y')}"


# ---------- Gender (Telegram me gender field nahi hota, isliye naam se guess + user ne bataya ho toh wahi) ----------
_gender_cache = {}  # user_id -> (gender 'm'/'f'/'u', source 'stated'/'guess')
_NOT = r"(?!\s+(?:nahi|nhi|not|na)\b)"
_F_RE = re.compile(r"(?i)\b(?:main|mai|mein|i am|i'm|im)\s+(?:ek\s+|a\s+)?(?:ladki|girl|female|woman|beti)\b" + _NOT +
                   r"|\b(?:ladki|girl|female)\s+(?:hoon|hu|hun|hoo)\b")
_M_RE = re.compile(r"(?i)\b(?:main|mai|mein|i am|i'm|im)\s+(?:ek\s+|a\s+)?(?:ladka|boy|male|man|guy|banda|launda)\b" + _NOT +
                   r"|\b(?:ladka|boy|male|launda)\s+(?:hoon|hu|hun|hoo)\b")


def detect_stated_gender(text: str):
    f, m = bool(_F_RE.search(text)), bool(_M_RE.search(text))
    return "f" if f and not m else "m" if m and not f else None


async def guess_gender_llm(first_name: str, username: str | None):
    prompt = (f'Telegram user ka first name: "{first_name}"' + (f', username: "{username}"' if username else "") +
              ". Is naam se (Indian/South Asian naamon ka dhyaan rakhke) ye zyada tar ladka (M) hai ya ladki (F)? "
              "Agar naam unisex, nickname, brand, emoji-style ho ya samajh na aaye toh U. Sirf ek letter likho: M, F ya U.")
    text = await call_groq([{"role": "user", "content": prompt}], model=SUMMARY_MODEL, temperature=0.0,
                           max_tokens=150, timeout=10.0, max_attempts=2, tag="gender")
    if not text:
        return None  # API fail: cache mat karo, agli baar dobara try
    m = re.search(r"\b([MFU])\b", text.strip().upper())
    return m.group(1).lower() if m else "u"


async def resolve_gender(user_id: int, first_name: str, username: str | None, text: str):
    """(gender, abhi-abhi-bataya?)"""
    stated = detect_stated_gender(text)
    if stated:
        _gender_cache[user_id] = (stated, "stated")
        _spawn(adb(save_gender_db, user_id, stated, "stated"))
        return stated, True
    if user_id in _gender_cache:
        return _gender_cache[user_id][0], False
    row = await adb(get_gender_db, user_id) if DATABASE_URL else None
    if row:
        _gender_cache[user_id] = (row[0], row[1])
        return row[0], False
    g = await guess_gender_llm(first_name or "", username)
    if g is None:
        return "u", False
    _gender_cache[user_id] = (g, "guess")
    _spawn(adb(save_gender_db, user_id, g, "guess"))
    return g, False


async def get_gender_known(user_id: int) -> str:
    if user_id in _gender_cache:
        return _gender_cache[user_id][0]
    row = await adb(get_gender_db, user_id) if DATABASE_URL else None
    return row[0] if row else "u"


def gender_hint(g: str, just_stated: bool = False) -> str:
    if g == "m":
        t = "User LADKA hai: uske liye masculine grammar (aap kar rahe hain, gaye the), thoda flirty-teasing chalega."
    elif g == "f":
        t = "User LADKI hai: uske liye feminine grammar (aap kar rahi hain, gayi thi), saheli jaisi masti, romantic flirting nahi."
    else:
        t = ("User ka gender pata nahi: gender-neutral rakho, user ke liye karta/karti, gaye/gayi jaise forms mat likho "
             "(jaise 'aap kaise hain', 'bataiye', 'kya chal raha hai').")
    if just_stated:
        t += " User ne abhi khud apna gender bataya hai; pehle galat samjha ho toh halka sa sorry bolke sudhar lo."
    return f"\n[Tum khud ladki ho (apne liye karti, gayi). {t}]"


async def gender_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mp = {"ladka": "m", "boy": "m", "male": "m", "m": "m", "ladki": "f", "girl": "f", "female": "f", "f": "f"}
    g = mp.get((context.args[0].lower() if context.args else ""))
    if not g:
        await update.message.reply_text("Aise bolo: /gender ladka ya /gender ladki")
        return
    uid = update.effective_user.id
    _gender_cache[uid] = (g, "stated")
    await adb(save_gender_db, uid, g, "stated")
    await update.message.reply_text("Done, ab se wahi samjhungi 😊" if g == "m" else "Acha ji, ab se saheli jaisi baat karungi 🤗")


_MOODS = ["masti aur tease wale mood me", "thodi lazy aur chill", "full energetic aur hasmukh", "thodi soft aur sentimental",
          "thodi sarcastic aur funny", "thodi sleepy par baat karne ka mann hai"]


def sneha_mood(user_id: int) -> str:
    n = datetime.now(IST)
    return _MOODS[int(hashlib.md5(f"{user_id}{n.strftime('%Y%m%d')}{n.hour // 2}".encode()).hexdigest(), 16) % len(_MOODS)]


async def get_ai_reply(user_message: str, user_id: int, user_name: str, history: list | None = None,
                       gap_seconds=None, extra_hint: str = "") -> str | None:
    gender, just_stated = await resolve_gender(user_id, user_name, None, user_message)
    explicit = is_explicit(user_message)
    if explicit:
        strikes = register_scold(user_id)
        extra_hint = (extra_hint + " " if extra_hint else "") + (
            "User ne gandi/18+ baat ki hai. Ek real ladki ki tarah daanto: gussa/sharam, samjhao ki ye galat hai, tameez se baat karo. "
            "Refusal ya AI wali line bilkul nahi. 2-3 line."
            + (" Ye user baar baar aisa kar raha hai, is baar zyada sakht bolo aur kaho ki ab aise baat karega toh main baat hi nahi karungi." if strikes >= 3 else ""))
    db_summary = await adb(get_user_summary, user_id)
    memory_context = ""
    if db_summary:
        cleaned = "\n".join(l for l in db_summary.split("\n") if not l.lower().startswith("naam:"))
        memory_context = f"\n\n[USER KI MEMORY:\n{cleaned}]\n"
    system = (SYSTEM_PROMPT + memory_context
              + f"\n[USER KA TELEGRAM NAAM: {user_name}. Isse asli naam samajh ke naturally use karo]"
              + f"\n[CONTEXT: {get_current_context()}]"
              + gender_hint(gender, just_stated)
              + f"\n[Tumhara abhi ka mood: {sneha_mood(user_id)}]"
              + build_behavior_hints(user_message, history, gap_seconds, extra_hint))
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    reasoning = os.getenv("CHAT_REASONING", "medium")
    max_tok = 1400 if reasoning == "medium" else 600
    last_reply, last_reason = None, None
    for attempt in range(2):
        text = await call_groq(messages, model=CHAT_MODEL, temperature=0.95 if attempt == 0 else 1.05,
                               max_tokens=max_tok, timeout=25.0, top_p=0.95, reasoning=reasoning, tag="chat")
        if not text:
            return random.choice(SCOLD_REPLIES) if explicit else None
        reply = postprocess_reply(text, user_message, user_id)
        reason = reject_reason(reply, user_message, history, allow_tu=explicit)
        if not reason:
            return reply
        last_reply, last_reason = reply, reason
        logger.info(f"♻️ Reply reject ({reason}) | {reply[:70]!r}")
        fixes = {"repeat": "Pichle replies jaisi baatein dobara mat karo, alag angle lo (reaction ya topic change).",
                 "tu_form": "Tu/tum/tera bilkul nahi, sirf izzat wala 'aap' (aap kaise hain, aap kya kar rahe hain).",
                 "overused": "Coffee, plan, music, gaane, movie, netflix ka naam bilkul mat lo."}
        fix = fixes.get(reason, "Bilkul naya, alag aur natural reply likho. Roman script.")
        messages = messages + [{"role": "system", "content": f"[Pichla draft reject hua ({reason}). {fix}]"}]
    # sirf 'repeat' pe purana reply bhej do, bot-like/devanagari kabhi nahi
    soft = last_reason in ("repeat", "tu_form", "overused") and last_reply  # chhoti galti pe chup rehne se behtar reply
    if explicit:  # model ne refuse/ajeeb jawab diya toh bhi "can't help" kabhi nahi, seedha daant
        return last_reply if soft else random.choice(SCOLD_REPLIES)
    return last_reply if soft else None


# ============================================================
# STATE
# ============================================================
user_warning_count = {}
bio_checked_users = set()
user_flood_data = {}
FLOOD_WINDOW, FLOOD_THRESHOLD, FLOOD_COOLDOWN = 4, 12, 120
LAST_CLEANUP = 0.0
chat_admin_cache = {}
admin_need_reply_cooldown = {}
user_msg_counter = {}
_welcomed_users = {}
conversation_memory = {}
_background_tasks = set()
_last_activity = {}
_last_db_seen = {}
_last_summarized_count = {}
_busy_notice_ts = {}
_user_locks = defaultdict(asyncio.Lock)
_cmd_cooldown = {}

BUSY_REPLIES = [
    "ek second yaar, net thoda slow chal raha hai 😪",
    "arey dimaag hang ho gaya mera, 1 minute ruko na 😴",
    "abhi thodi busy hoon, thodi der me aati hoon 🙏",
]


def check_flood(user_id: int, is_sticker: bool = False) -> str:
    global LAST_CLEANUP
    now = time.time()
    if now - LAST_CLEANUP > 600:
        for uid in [u for u, d in user_flood_data.items() if d["cd"] > 0 and now >= d["cd"] and not d["ts"]]:
            del user_flood_data[uid]
        LAST_CLEANUP = now
    data = user_flood_data.get(user_id)
    if data is None:
        user_flood_data[user_id] = {"ts": [now], "cd": 0.0}
        return "ok"
    if now < data["cd"]:
        return "cooldown"
    if data["cd"] > 0.0:
        data["cd"], data["ts"] = 0.0, []
    data["ts"].append(now)
    if is_sticker:
        data["ts"].append(now)
    data["ts"] = [t for t in data["ts"] if now - t < FLOOD_WINDOW]
    if len(data["ts"]) >= FLOOD_THRESHOLD:
        data["cd"], data["ts"] = now + FLOOD_COOLDOWN, []
        return "flood"
    return "ok"


async def is_bot_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    now = time.time()
    cached = chat_admin_cache.get(chat_id)
    if cached and now - cached[0] < 120:
        return cached[1]
    try:
        member = await context.bot.get_chat_member(chat_id, context.bot.id)
        ok = member.status in ("administrator", "creator")
    except Exception:
        ok = False
    chat_admin_cache[chat_id] = (now, ok)
    return ok


def get_history(user_id: int) -> list:
    if user_id not in conversation_memory:
        conversation_memory[user_id] = load_conversation_history_from_db(user_id)
    return conversation_memory[user_id]


async def get_history_async(user_id: int) -> list:
    if user_id not in conversation_memory:
        conversation_memory[user_id] = await adb(load_conversation_history_from_db, user_id)
    return conversation_memory[user_id]


def _spawn(coro):
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)


def update_history(user_id: int, user_message: str | None, bot_reply: str, telegram_name: str | None = None, chat_id: int = None) -> None:
    history = conversation_memory.setdefault(user_id, [])  # pehle yahan har baar DB call hoti thi
    if user_message is not None:
        history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": bot_reply})
    if len(history) > MAX_HISTORY_MESSAGES:
        conversation_memory[user_id] = history = history[-MAX_HISTORY_MESSAGES:]
    if user_message is None:
        _spawn(adb(save_conversation_history_to_db, user_id, list(history)))
        return
    count = user_msg_counter.get(user_id, 0) + 1
    user_msg_counter[user_id] = count
    _last_activity[user_id] = time.time()
    _spawn(adb(save_conversation_history_to_db, user_id, list(history)))
    if count % 6 == 0:
        _spawn(generate_summary(user_id, list(history), telegram_name))
        _last_summarized_count[user_id] = count


async def touch_user(user_id: int, private: bool):
    """Pichli activity wapas karta hai (gap hint ke liye) aur nayi activity save karta hai."""
    now = time.time()
    prev = _last_activity.get(user_id)
    if prev is None and DATABASE_URL:
        prev = await adb(get_last_seen, user_id)
    _last_activity[user_id] = now
    if private and DATABASE_URL and now - _last_db_seen.get(user_id, 0) > 120:
        _last_db_seen[user_id] = now
        _spawn(adb(touch_user_state, user_id, now))
    return (now - prev) if prev else None


# ============================================================
# TELEGRAM SEND HELPERS
# ============================================================
def build_premium_emoji_entities(text: str, emoji_map: dict) -> list:
    if not text or not emoji_map:
        return []
    entities, i, off = [], 0, 0
    keys = sorted(emoji_map, key=len, reverse=True)
    while i < len(text):
        for emo in keys:
            if text.startswith(emo, i):
                ln = len(emo.encode("utf-16-le")) // 2
                entities.append(MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=off, length=ln, custom_emoji_id=emoji_map[emo]))
                off += ln
                i += len(emo)
                break
        else:
            off += len(text[i].encode("utf-16-le")) // 2
            i += 1
    return entities


def apply_premium_emoji_html(text: str) -> str:
    if not text:
        return text
    for emoji, eid in CHAT_PREMIUM_EMOJIS.items():
        if emoji in text and f'emoji-id="{eid}"' not in text:
            text = text.replace(emoji, f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>')
    return text


async def safe_reply_text(update: Update, text: str, use_premium_emojis: bool = True, **kwargs):
    original = text
    try:
        if use_premium_emojis:
            if kwargs.get("parse_mode") == "HTML":
                text = apply_premium_emoji_html(text)
            elif "entities" not in kwargs and "parse_mode" not in kwargs:
                ents = build_premium_emoji_entities(text, CHAT_PREMIUM_EMOJIS)
                if ents:
                    kwargs["entities"] = ents
        return await update.message.reply_text(text, **kwargs)
    except Exception as e:
        logger.warning(f"reply_text fail ({str(e)[:80]}), plain fallback")
        # pehle yahan HTML parse error pe wahi HTML dobara bhejta tha, reply gum ho jaata tha ("<3" jaisi cheezon pe)
        try:
            kwargs.pop("entities", None)
            kwargs.pop("parse_mode", None)
            kwargs.pop("reply_to_message_id", None)
            plain = html.unescape(re.sub(r"<[^>]+>", "", original))
            return await update.message.reply_text(plain, **kwargs)
        except Exception as e2:
            logger.warning(f"reply_text fallback fail: {e2}")


async def send_ai_reply(update: Update, reply: str):
    await safe_reply_text(update, html.escape(reply, quote=False), parse_mode="HTML",
                          reply_to_message_id=update.message.message_id)


async def send_reply_bubbles(update: Update, context: ContextTypes.DEFAULT_TYPE, reply: str):
    """Kabhi kabhi lamba reply 2 chhote messages me bhejo, jaise insaan karte hain."""
    sents = re.split(r"(?<=[.?!])\s+", reply)
    if len(reply) > 45 and len(sents) >= 2 and random.random() < 0.3:
        k = random.randint(1, len(sents) - 1)
        first = re.sub(r"(?<!\.)\.$", "", " ".join(sents[:k]).strip())
        second = " ".join(sents[k:]).strip()
        em = next((e for e in CHAT_PREMIUM_EMOJIS if e in first), None)
        if em:  # emoji hamesha aakhri bubble me
            first = first.replace(em, "").strip()
            if em not in second:
                second = f"{second} {em}"
        await send_ai_reply(update, first)
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.8, 1.6))
        try:
            ents = build_premium_emoji_entities(second, CHAT_PREMIUM_EMOJIS)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=second, entities=ents or None)
        except Exception as e:
            logger.warning(f"2nd bubble fail: {str(e)[:80]}")
        return
    await send_ai_reply(update, reply)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        while True:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(4.0)
    except asyncio.CancelledError:
        pass


async def realistic_typing_delay(context, chat_id, text) -> None:
    await asyncio.sleep(random.uniform(0.4, 0.9))


async def get_reply_with_live_typing(context, chat_id: int, coro, existing_typing_task=None):
    typing_task = existing_typing_task or asyncio.create_task(_keep_typing(context, chat_id))
    start = time.time()
    try:
        result = await coro
        target = random.uniform(0.5, 1.1)
        if isinstance(result, str) and result:
            target = min(target + len(result) / 22.0, 3.5)  # pehle 2.5-7.5s ka fixed intezaar tha
        remaining = target - (time.time() - start)
        if remaining > 0:
            await asyncio.sleep(remaining)
        return result
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


# Telegram ki allowed reaction list me jo emojis hain wahi (pehle 😂 👋 🥳 invalid the, silently fail hote the)
def pick_reaction(text: str):
    low = text.lower()
    tokens = set(re.findall(r"[a-z]+", low))
    if tokens & {"love", "pyaar", "ilysm", "ily", "babu", "jaan"} or "❤" in text:
        return "❤"
    if tokens & {"lol", "lmao", "haha", "hahaha", "funny", "mazaak", "joke", "comedy"} or "😂" in text or "🤣" in text:
        return "🤣"
    if tokens & {"sad", "cry", "dukhi", "rip", "miss", "akela"} or "😭" in text:
        return "😢"
    if tokens & {"thank", "thanks", "shukriya", "thx", "ty"}:
        return "🙏"
    if tokens & {"party", "celebrate", "congrats", "congratulations", "happy"}:
        return "🎉"
    if tokens & {"wow", "nice", "good", "mast", "badiya", "cool", "great", "awesome"} or "🔥" in text:
        return "🔥"
    if tokens & {"hi", "hello", "hey", "namaste", "namaskar", "yo", "hola"}:
        return "🤗"
    if random.random() < 0.2:
        return random.choice(["👍", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🎉", "👀"])
    return None


async def send_sneha_reaction(update: Update):
    try:
        if update.message and update.message.text:
            r = pick_reaction(update.message.text)
            if r:
                await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji=r)])
    except Exception:
        pass


# ============================================================
# COMMANDS
# ============================================================
def _is_direct_image(url: str) -> bool:
    return bool(url) and bool(re.search(r"\.(jpg|jpeg|png|webp|gif)(\?.*)?$", url, re.I))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = context.bot.username
    user = update.effective_user
    _spawn(save_broadcast_user_async(user.id))
    user_name = html.escape(user.first_name or "Buddy")
    bot_name = html.escape(context.bot.first_name or "AI Girl Bot")
    indent = "\u200e " * 14
    emo_row = " ".join(f'<tg-emoji emoji-id="{i}">💃</tg-emoji>' for i in
                       ("5789593740091857906", "5792001889600022897", "5789579686958865077", "5789419819686173268",
                        "5789588379972671766", "5791677275971787880", "5792001889600022897"))
    dance = '<tg-emoji emoji-id="5161221487608201804">💃</tg-emoji>'
    star = '<tg-emoji emoji-id="5362079447136610876">✨</tg-emoji>'
    welcome_text = (
        f"\n{indent}{emo_row}\n\n"
        f"<blockquote>"
        f"<b>{dance} ⁂ ʜєʏ {user_name}! ϻᴧɪɴ {bot_name} ʜυɴ</b>\n\n"
        f"<b>{dance} ⁂ ᴛυϻʜᴧʀɪ ꜱϻᴧʀᴛ ᴅᴏꜱᴛ, ᴄʜᴧᴛ, ɢᴧϻєꜱ, ᴧυʀ ϻᴧꜱᴛɪ</b>\n\n"
        f"<b>{dance} ⁂ ϻᴧᴋє ϻє ᴧᴅϻɪɴ ꜰᴏʀ ꜱυʟʟ ɢʀᴏυρ ϻᴧɴᴧɢєϻєɴᴛ ᴧɴᴅ ꜱϻᴧʀᴛ ꜰєᴀᴛυʀєꜱ</b>\n"
        f"</blockquote>\n\n"
        f'{star} <b> ⁂ ᴘᴏᴡєʀєᴅ ʙʏ,</b> <a href="https://t.me/KnowRajpapa">ʀᴧᴊ ϙυᴧɴᴛυϻ ᴄᴏʀє</a>\n\n'
        f'{star} <b> ⁂ ᴅєᴠєʟᴏᴘє ʙʏ,</b> <a href="https://t.me/its_raj_king">ʀᴧᴊ ᴄʜєᴧᴛꜱ ᴏᴡɴєʀ</a>\n'
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ᴧᴅᴅ ϻє ʙᴧʙʏ", url=f"https://t.me/{bot_username}?startgroup=start", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["kidnap"])],
        [InlineKeyboardButton("ᴅєᴠєʟᴏᴘєʀ", url="https://t.me/its_raj_king", style=ButtonStyle.DANGER, icon_custom_emoji_id=PREMIUM_EMOJIS["developer"]),
         InlineKeyboardButton("ᴊᴏɪɴ ᴄʜᴧɴɴєʟ", url="https://t.me/KnowRajpapa", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["channel"])],
        [InlineKeyboardButton("ᴄʜᴧᴛ ɢʀᴏυᴘ", url="https://t.me/+0xoXWln4qiM2NTY9", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=PREMIUM_EMOJIS["support"])],
    ])
    try:
        await update.message.reply_photo(photo=WELCOME_IMAGE_URL, caption=welcome_text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"start error: {e}")
        try:
            await update.message.reply_text(re.sub(r"<[^>]+>", "", welcome_text), reply_markup=kb)
        except Exception:
            pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Main Sneha 😊 seedha baat karo, ya ye try karo:\n\n"
        "/roast, mazedaar roast (kisi ke message pe reply karke bhi)\n"
        "/shayari [topic], ek chhoti shayari\n"
        "/joke, ek joke\n"
        "/ship @naam1 @naam2, couple meter\n"
        "/truth /dare, game\n"
        "/kiss /slap /hug /pat, GIF ke saath (reply ya @naam)\n"
        "/crush @naam, aaj ka crush level\n"
        "/new, WordSeek game (/wordseek help ke liye)\n"
        "/memory, main tumhare baare me kya jaanti hoon\n"
        "/gender ladka ya ladki, agar main galat samjhun toh sudhar do\n"
        "/forget, meri memory se tumhara data delete\n"
        "Voice note bhi bhej sakte ho, sun leti hoon 🎧\nKabhi kabhi pyaar se main bhi voice bhejti hoon 🥰")


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = time.time()
    m = await update.message.reply_text("pong 🏓")
    await m.edit_text(f"pong 🏓 {int((time.time() - t) * 1000)}ms")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user.id != OWNER_ID:
            return
        now = time.time()
        reset_daily_if_new_day()
        n = len(clients)
        sum_req, sum_tok = sum(daily_requests), sum(daily_tokens)
        max_req, max_tok = n * 1000, n * 200000
        cooling = sum(1 for i in range(n) if _key_cooldowns.get(i, 0) > now)
        text = (f"📊 Bot Usage\n\n🕐 {now_ist_str()}\n🔑 Keys: {n}\n"
                f"📆 Requests: {sum_req}/{max_req} ({sum_req / max_req * 100:.1f}%)\n"
                f"📆 Tokens: {sum_tok}/{max_tok} ({sum_tok / max_tok * 100:.1f}%)\n"
                f"⚡ Active: {n - cooling} | ❄️ Cooldown: {cooling}\n"
                f"⏳ In-flight: {MAX_CONCURRENT_REQUESTS - _concurrency_semaphore._value}/{MAX_CONCURRENT_REQUESTS}\n"
                f"👥 Chats in RAM: {len(conversation_memory)}")
        if context.args and context.args[0].lower() == "live":
            lines = []
            for i in range(n):
                _clean_key_usage(i, now)
                cd = max(0, int(_key_cooldowns.get(i, 0) - now))
                lines.append(f"K{i+1}: rpm {len(_key_usage[i])} | day {daily_requests[i]}r/{daily_tokens[i]}t" + (f" | ❄️{cd}s" if cd else ""))
            text += "\n\n" + "\n".join(lines)
        await update.message.reply_text(text[:4000])
    except Exception as e:
        logger.error(f"stats error: {e}")


async def resetkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    for i in range(len(clients)):
        _key_429_counts[i] = 0
    _key_cooldowns.clear()
    await update.message.reply_text("✅ Sab keys reset ho gayi!")


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    args = context.args
    if args and uid != OWNER_ID:
        await update.message.reply_text("❌ Dusron ki memory sirf owner dekh sakta hai.")
        return
    if args:
        target = args[0]
        try:
            if target.startswith("@"):
                tid = (await context.bot.get_chat(target)).id
            else:
                tid = int(target)
        except ValueError:
            await update.message.reply_text("❌ Galat format. /memory @username ya /memory 123456")
            return
        except Exception:
            await update.message.reply_text("❌ User nahi mila ya bot ko unki info nahi hai.")
            return
        s = await adb(get_user_summary, tid)
        await update.message.reply_text(f"🧠 {target} ki memory:\n{s or 'Khali hai.'}")
    else:
        s = await adb(get_user_summary, uid)
        await update.message.reply_text(f"🧠 Tumhari memory:\n{s or 'Khali hai.'}")


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await adb(delete_user_data, uid)
    conversation_memory.pop(uid, None)
    user_msg_counter.pop(uid, None)
    _last_summarized_count.pop(uid, None)
    await update.message.reply_text("Ho gaya, sab bhula diya maine 🙏 ab hum fresh start karte hain.")


async def dbcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    if not DATABASE_URL:
        await update.message.reply_text("❌ DATABASE_URL set nahi hai.")
        return
    try:
        total = (await adb(db_exec, "SELECT COUNT(*) FROM user_memory", (), "one"))[0]
        s = await adb(get_user_summary, update.effective_user.id)
        await update.message.reply_text(f"✅ DB OK ({total} users ki memory)\n\n" + (f"Tumhari:\n{s}" if s else "Tumhari memory abhi nahi bani."))
    except Exception as e:
        await update.message.reply_text(f"DB check error: {e}")


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    if not DATABASE_URL:
        await update.message.reply_text("❌ DATABASE_URL set nahi hai.")
        return
    try:
        await update.message.reply_text(f"⏳ Backup ban raha hai... ({now_ist_str()})")
        rows = await adb(db_exec, "SELECT user_id, summary, updated_at FROM user_memory", (), "all")
        data = {"backup_time_ist": now_ist_str(), "total_users": len(rows),
                "users": [{"user_id": r[0], "summary": r[1], "updated_at": r[2]} for r in rows]}
        await context.bot.send_document(chat_id=update.effective_chat.id,
                                        document=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                                        filename=f"sneha_backup_{time.strftime('%Y%m%d_%H%M%S')}.json",
                                        caption=f"✅ Backup complete, {len(rows)} users\n🕐 {now_ist_str()}")
    except Exception as e:
        await update.message.reply_text(f"❌ Backup fail hua: {e}")


def _migrate_sync(old_url):
    oc = psycopg2.connect(old_url)
    try:
        cur = oc.cursor()
        cur.execute("SELECT user_id, summary, updated_at FROM user_memory"); users = cur.fetchall()
        cur.execute("SELECT user_id, started_at FROM broadcast_users"); bro = cur.fetchall()
        cur.execute("SELECT chat_id, title, added_at FROM active_groups"); grp = cur.fetchall()
    finally:
        oc.close()
    for u, s, t in users:
        db_exec("INSERT INTO user_memory (user_id, summary, updated_at) VALUES (%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET summary=%s, updated_at=%s", (u, s, t, s, t))
    for u, t in bro:
        db_exec("INSERT INTO broadcast_users (user_id, started_at) VALUES (%s,%s) ON CONFLICT (user_id) DO NOTHING", (u, t))
    for c, ti, t in grp:
        db_exec("INSERT INTO active_groups (chat_id, title, added_at) VALUES (%s,%s,%s) ON CONFLICT (chat_id) DO UPDATE SET title=%s, added_at=%s", (c, ti, t, ti, t))
    return len(users), len(bro), len(grp)


async def migrate_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    old_url = os.getenv("OLD_DATABASE_URL")
    if not DATABASE_URL or not old_url:
        await update.message.reply_text("❌ DATABASE_URL ya OLD_DATABASE_URL set nahi hai.")
        return
    msg = await update.message.reply_text("🔄 Purani memory copy ho rahi hai...")
    try:
        u, b, g = await adb(_migrate_sync, old_url)
        await msg.edit_text(f"✅ Migration complete!\n📦 user_memory: {u}\n📦 broadcast_users: {b}\n📦 active_groups: {g}")
    except Exception as e:
        await msg.edit_text(f"❌ Migration error: {e}")


async def syncgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    if not DATABASE_URL:
        await update.message.reply_text("❌ Database connect nahi hai.")
        return
    msg = await update.message.reply_text("🔄 Sync shuru ho raha hai...")
    rows = await adb(get_all_active_groups)
    if not rows:
        await msg.edit_text("ℹ️ Database me abhi koi group record nahi hai.")
        return
    checked = active = removed = skipped = 0
    removed_titles = []
    for chat_id, title in rows:
        checked += 1
        try:
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if member.status in ("left", "kicked", "banned"):
                await delete_active_group_async(chat_id)
                removed += 1
                removed_titles.append(title or str(chat_id))
            else:
                active += 1
                try:
                    co = await context.bot.get_chat(chat_id)
                    if co and co.title and co.title != title:
                        await save_active_group_async(chat_id, co.title)
                except Exception:
                    pass
        except Exception as e:
            s = str(e).lower()
            if "forbidden" in s or "chat not found" in s or "kicked" in s:
                await delete_active_group_async(chat_id)
                removed += 1
                removed_titles.append(title or str(chat_id))
            else:
                skipped += 1
                active += 1
        await asyncio.sleep(0.1)
    text = f"✅ Sync complete!\n\n📋 Checked: {checked}\n✅ Active: {active}\n🗑️ Removed: {removed}\n"
    if skipped:
        text += f"⏳ Skipped: {skipped}\n"
    if removed_titles:
        text += "\n🗑️ Removed groups:\n" + "\n".join(f"• {t}" for t in removed_titles[:10])
        if len(removed_titles) > 10:
            text += f"\n...aur {len(removed_titles) - 10} aur"
    await msg.edit_text(text)


# ---------- WordSeek ----------
async def wordseek_help(update, context):
    await update.message.reply_text(engine.get_help())


async def _start_ws(update, length):
    _, reply = engine.start_game(update.effective_chat.id, update.effective_user.id, length)
    await update.message.reply_text(reply)


async def wordseek_new(update, context):
    length = 5
    if context.args and context.args[0].isdigit() and int(context.args[0]) in (4, 5, 6):
        length = int(context.args[0])
    await _start_ws(update, length)


async def wordseek_new4(update, context): await _start_ws(update, 4)
async def wordseek_new5(update, context): await _start_ws(update, 5)
async def wordseek_new6(update, context): await _start_ws(update, 6)


async def wordseek_end(update, context):
    is_admin = False
    if update.effective_chat.type in ("group", "supergroup"):
        try:
            m = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
            is_admin = m.status in ("administrator", "creator")
        except Exception:
            pass
    _, reply = engine.end_game(update.effective_chat.id, update.effective_user.id, is_admin)
    await update.message.reply_text(reply)


async def wordseek_lb(update, context):
    await update.message.reply_text(engine.get_leaderboard(update.effective_chat.id, update.effective_chat.title or "Leaderboard"))


# ---------- Fun commands ----------
PERSONA_MINI = ("Tum Sneha ho, witty aur thodi flirty ladki jo Hinglish (sirf Roman script) me bolti hai. "
                "Koi markdown ya heading nahi, sirf chat text. Exactly 1 emoji in me se: " + " ".join(PROMPT_EMOJIS))

TRUTHS = ["Aakhri baar kisse jhooth bola tha aur kyun?", "Phone me sabse embarrassing cheez kya hai?",
          "Aaj tak ka sabse bada darr kya hai?", "Kis cheez pe sabse zyada paisa waste kiya hai?",
          "Group me kiska chat dekhke sabse zyada hasi aati hai?", "Aakhri baar kab roye the aur kyun?",
          "Koi aisi aadat jo kisi ko nahi pata?", "Crush ka naam batao (initial bhi chalega) 😏",
          "Sabse ajeeb sapna kaunsa aaya tha?", "Kaunsa secret hai jo ab tak chhupaya hai?"]
DARES = ["Group me apni sabse purani selfie bhejo 😂", "Apne last chat ka screenshot bhejo (private chhodke)",
         "Ek minute tak sirf shayari me baat karo", "Apne naam se ek funny rap likho",
         "Voice note me kisi janwar ki awaaz nikalo", "Apni profile pic 1 ghante ke liye badlo",
         "Group me kisi ko bina wajah compliment do", "Next 5 messages sirf emojis me likho"]
SHIP_COMMENTS = [(20, "Bhai ye toh dost bhi nahi ban sakte 😪"), (40, "Thoda scope hai, mehnat lagegi 🙏"),
                 (60, "Chalta hai, setting ho sakti hai 😊"), (80, "Arey wah, jodi mast hai 🔥"),
                 (101, "Shaadi ka card kab chhap raha hai? 🥰")]


def _cooldown_ok(uid: int, secs: float = 12.0) -> bool:
    now = time.time()
    if now - _cmd_cooldown.get(uid, 0) < secs:
        return False
    _cmd_cooldown[uid] = now
    return True


async def _ai_fun(prompt: str, max_tokens: int = 300):
    text = await call_groq([{"role": "system", "content": PERSONA_MINI}, {"role": "user", "content": prompt}],
                           model=CHAT_MODEL, temperature=1.0, max_tokens=max_tokens, timeout=20.0, tag="fun")
    if not text:
        return None
    text = re.sub(r"<think[\s\S]*?</think>", "", text, flags=re.I).replace("**", "").strip()
    text = sanitize_reply_emojis(text)
    return None if has_devanagari(text) else text


async def _run_fun(update, context, prompt, max_tokens=300):
    if not _cooldown_ok(update.effective_user.id):
        return
    typing = asyncio.create_task(_keep_typing(context, update.effective_chat.id))
    try:
        text = await _ai_fun(prompt, max_tokens)
    finally:
        typing.cancel()
    if text:
        await safe_reply_text(update, text, reply_to_message_id=update.message.message_id)
    else:
        await update.message.reply_text(random.choice(BUSY_REPLIES))


async def roast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rm = update.message.reply_to_message
    if context.args:
        target = " ".join(context.args)[:40]
    elif rm and rm.from_user and not rm.from_user.is_bot:
        target = rm.from_user.first_name or "ye banda"
    else:
        target = update.effective_user.first_name or "tum"
    await _run_fun(update, context,
                   f"{target} ko ek halka phulka, funny roast do (3-4 line). Sirf unke naam/aadat/behaviour pe mazaak, "
                   "gaali nahi. Dharm, jaati, body, family, gender ya kisi bimari pe bilkul nahi. Aakhir me ek pyaar wali line.")


async def shayari_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)[:60] if context.args else random.choice(["chai", "yaari", "intezaar", "baarish", "neend", "dil"])
    await _run_fun(update, context, f"'{topic}' pe 4 line ki original, thodi funny shayari likho. Kisi mashhoor shayar ki line copy mat karna.", 250)


async def joke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_fun(update, context, "Ek chhota, saaf-suthra, original desi joke sunao (3 line max). Koi gandi ya sensitive baat nahi.", 200)


async def truth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Truth 😏\n" + random.choice(TRUTHS))


async def dare_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Dare 🔥\n" + random.choice(DARES))


async def ship_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    names = list(context.args[:2])
    rm = update.message.reply_to_message
    me = update.effective_user.first_name or "Tum"
    if not names and rm and rm.from_user:
        names = [me, rm.from_user.first_name or "Wo"]
    elif len(names) == 1:
        names = [me, names[0]]
    if len(names) < 2:
        await update.message.reply_text("Aise use karo: /ship @naam1 @naam2 (ya kisi ke message pe reply karke /ship)")
        return
    a, b = [n.lstrip("@")[:25] for n in names]
    pct = int(hashlib.md5("|".join(sorted([a.lower(), b.lower()])).encode()).hexdigest(), 16) % 101
    comment = next(c for lim, c in SHIP_COMMENTS if pct < lim)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    await update.message.reply_text(f"💘 {a} x {b}\n\n{bar} {pct}%\n\n{comment}")


# ---------- Action commands (GIF) + crush ----------
_act_cooldown = {}
ACTIONS = {
    "kiss": ["{a} ne {b} ko pyaar se kiss kiya 💋", "{a} ne {b} ko sabke saamne kiss kar diya 😳💋", "{b} ko {a} ki taraf se ek pyaari si kiss 💋"],
    "slap": ["{a} ne {b} ko ek zordaar thappad maara 👋😤", "{a} ka haath chal gaya, {b} ab gaal sehla raha hai 😂", "{b} ko {a} se thappad mil gaya, kuch to kiya hoga 👋"],
    "hug": ["{a} ne {b} ko tight hug diya 🤗", "{a} aur {b} ka pyaara sa hug 🥰", "{a} ne {b} ko gale laga liya 🤗"],
    "pat": ["{a} ne {b} ke sar pe pyaar se haath fera 🥰", "{b} ko {a} ne good boy/girl wala pat diya 😊", "{a} ne {b} ko pat pat kiya 🥰"],
}
CRUSH_COMMENTS = [(15, "Ye toh seedha friend zone ka ticket hai 😪"), (35, "Halka sa spark hai, par abhi door ki baat 🙏"),
                  (55, "Dil me kuch kuch hota hai 😊"), (80, "Arey wah, crush pakka hai, ab propose kab? 🔥"),
                  (101, "Ye toh seedha pyaar hai, shaadi ki taiyari karo 🥰")]


def _mention(uid, name):
    name = html.escape(name or "koi")
    return f'<a href="tg://user?id={uid}">{name}</a>' if uid else name


def resolve_target(update: Update, context):
    """(user_id|None, naam|None, is_bot). Reply > text_mention > @username/naam arg."""
    msg = update.message
    rm = msg.reply_to_message
    if rm and rm.from_user:
        u = rm.from_user
        return u.id, (u.first_name or "koi"), u.is_bot
    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user:
            return ent.user.id, (ent.user.first_name or "koi"), ent.user.is_bot
    if context.args:
        raw = " ".join(context.args)[:30]
        return None, raw, False
    return None, None, False


async def fetch_action_gif(action: str):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            for url, pick in ((f"https://nekos.best/api/v2/{action}", lambda j: j["results"][0]["url"]),
                              (f"https://api.waifu.pics/sfw/{action}", lambda j: j["url"])):
                try:
                    r = await c.get(url)
                    return pick(r.json())
                except Exception:
                    continue
    except Exception:
        pass
    return None


async def _action_command(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    me = update.effective_user
    now = time.time()
    if now - _act_cooldown.get(me.id, 0) < 8:
        return
    tid, tname, is_bot = resolve_target(update, context)
    if not tname:
        await update.message.reply_text(f"Aise use karo: /{action} @naam (ya kisi ke message pe reply karke /{action})")
        return
    _act_cooldown[me.id] = now
    if tid == context.bot.id or is_bot:
        await update.message.reply_text({"kiss": "Chhi, mujhe nahi, apne partner ko karo 😪", "slap": "Mujhe maaroge? Haath tod dungi 😤",
                                         "hug": "Aww, aao aao, virtual hug 🤗", "pat": "Hehe, thank you 🥰"}[action])
        return
    if tid == me.id:
        await update.message.reply_text("Khud ko? 😂 Kisi aur ko try karo na " + random.choice(PROMPT_EMOJIS))
        return
    caption = random.choice(ACTIONS[action]).format(a=_mention(me.id, me.first_name), b=_mention(tid, tname.lstrip("@") if not tid and not tname.startswith("@") else tname))
    gif = await fetch_action_gif(action)
    try:
        if gif:
            await update.message.reply_animation(animation=gif, caption=caption, parse_mode="HTML")
            return
    except Exception as e:
        logger.warning(f"gif send fail: {str(e)[:100]}")
    await safe_reply_text(update, caption, parse_mode="HTML")


async def kiss_command(update, context): await _action_command(update, context, "kiss")
async def slap_command(update, context): await _action_command(update, context, "slap")
async def hug_command(update, context): await _action_command(update, context, "hug")
async def pat_command(update, context): await _action_command(update, context, "pat")


async def crush_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = update.effective_user
    tid, tname, is_bot = resolve_target(update, context)
    if not tname:
        await update.message.reply_text("Aise use karo: /crush @naam (ya kisi ke message pe reply karke /crush)")
        return
    if tid == me.id:
        await update.message.reply_text("Apne hi upar crush? 😂 Self love achhi cheez hai par kisi aur ka naam lo na")
        return
    # direction + din ke hisaab se: aaj ka level, kal alag aa sakta hai
    seed = f"{me.id}|{tid or tname.lower()}|{datetime.now(IST).strftime('%Y%m%d')}"
    pct = int(hashlib.md5(seed.encode()).hexdigest(), 16) % 101
    comment = next(c for lim, c in CRUSH_COMMENTS if pct < lim)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    await safe_reply_text(update, f"💘 {_mention(me.id, me.first_name)} ka {_mention(tid, tname)} pe aaj ka crush level\n\n{bar} {pct}%\n\n{html.escape(comment)}",
                          parse_mode="HTML")


# ============================================================
# VOICE (Groq Whisper)
# ============================================================
async def transcribe_voice(context: ContextTypes.DEFAULT_TYPE, voice) -> str | None:
    try:
        if voice.duration and voice.duration > 60:
            return None
        f = await context.bot.get_file(voice.file_id)
        data = bytes(await f.download_as_bytearray())
        tried = set()
        for _ in range(min(3, len(clients))):
            idx = pick_best_key(time.time(), 100, tried)
            if idx is None:
                break
            tried.add(idx)
            entry = pre_record_key_usage(idx, 100)
            try:
                r = await clients[idx].audio.transcriptions.create(file=("voice.ogg", data), model=WHISPER_MODEL, timeout=25.0)
                settle_key_usage(idx, entry, 30)
                text = (getattr(r, "text", None) or (r if isinstance(r, str) else "")).strip()
                return text or None
            except Exception as e:
                if "429" in str(e).lower():
                    handle_429_error(idx, e)
                else:
                    logger.warning(f"whisper fail: {str(e)[:100]}")
    except Exception as e:
        logger.warning(f"transcribe_voice fail: {e}")
    return None


# ============================================================
# SNEHA KI VOICE (edge-tts, free). Fail ho toh chupchap text hi rehta hai.
# ============================================================
_LOVE_TOKENS = {"love", "luv", "pyaar", "pyar", "miss", "babu", "jaan", "jaanu", "shona", "kiss", "hug", "sweetheart",
                "darling", "crush", "marry", "mishti", "cutie"}
_LOVE_PHRASES = ("love you", "miss you", "tumse pyaar", "i like you", "mujhe pasand", "tum cute", "you are cute", "tum bahut pyari")
_SPECIAL_TOKENS = {"birthday", "bday", "janamdin", "anniversary", "congrats", "congratulations", "badhai", "mubarak",
                   "diwali", "holi", "eid", "christmas"}
_SPECIAL_PHRASES = ("happy birthday", "new year", "pass ho gaya", "job lag gayi")
_GREET_PHRASES = ("good night", "good morning", "shubh ratri", "suprabhat")
_last_voice_ts = {}


def voice_trigger_chance(text: str, reply: str = "", incoming_voice: bool = False, private: bool = True) -> float:
    if is_explicit(text):
        return 0.0
    low = text.lower()
    tokens = set(re.findall(r"[a-z]+", low))
    if tokens & _SPECIAL_TOKENS or any(p in low for p in _SPECIAL_PHRASES):
        return 0.9
    if tokens & _LOVE_TOKENS or any(p in low for p in _LOVE_PHRASES) or "❤" in text or "😘" in text:
        return 0.7
    if any(p in low for p in _GREET_PHRASES) or tokens & {"gn", "gm"}:
        return 0.35
    if incoming_voice:  # voice ka jawab kabhi kabhi voice me
        return 0.4
    if private and 0 < len(reply) <= 110 and "?" not in text and len(text.split()) <= 12:
        return 0.07  # beech beech me bina wajah ke bhi cute voice
    return 0.0


async def synth_sneha_voice(reply: str) -> bytes | None:
    import edge_tts
    plain = html.unescape(_SINGLE_EMOJI.sub("", reply)).strip()
    if not plain or len(plain) > 260:
        return None
    # Roman Hinglish ko Hindi voice galat padhti hai, isliye pehle Devanagari me likhwate hain (sirf voice ke liye)
    deva = await call_groq(
        [{"role": "user", "content": "Is Hinglish line ko Devanagari Hindi me likho (transliterate, English words bhi Devanagari me, "
                                     "meaning aur tone same). Sirf output likho, koi explanation nahi:\n" + plain}],
        model=SUMMARY_MODEL, temperature=0.1, max_tokens=300, timeout=15.0, tag="voice-txt")
    if not deva or not has_devanagari(deva):
        return None
    comm = edge_tts.Communicate(deva.strip(), VOICE_NAME, rate="+3%", pitch="+6Hz")
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    return bytes(buf) or None


async def try_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, reply: str,
                          incoming_voice: bool = False, private: bool = True) -> bool:
    """True = sirf voice bhej di (text nahi bhejna). False = caller normal text bheje."""
    if not VOICE_REPLIES:
        return False
    uid = update.effective_user.id
    if time.time() - _last_voice_ts.get(uid, 0) < VOICE_COOLDOWN:
        return False
    if random.random() >= voice_trigger_chance(user_text, reply, incoming_voice, private):
        return False
    _last_voice_ts[uid] = time.time()
    try:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="record_voice")
        except Exception:
            pass
        audio = await asyncio.wait_for(synth_sneha_voice(reply), timeout=25)
        if not audio:
            _last_voice_ts.pop(uid, None)
            return False
        await update.message.reply_voice(voice=InputFile(io.BytesIO(audio), filename="sneha.mp3"),
                                         reply_to_message_id=update.message.message_id)
        logger.info(f"🎤 Voice bheji to {uid}")
        return True
    except Exception as e:
        _last_voice_ts.pop(uid, None)
        logger.warning(f"voice fail (text bhej raha hoon): {str(e)[:120]}")
        return False


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner test: /voice kuch bhi likho, Sneha usse bolke sunayegi."""
    if update.effective_user.id != OWNER_ID:
        return
    text = " ".join(context.args) or "Hello, main Sneha hoon, kaise ho tum?"
    try:
        audio = await asyncio.wait_for(synth_sneha_voice(text), timeout=30)
        if not audio:
            await update.message.reply_text("❌ Voice nahi bani (edge-tts install hai? logs dekho)")
            return
        await update.message.reply_voice(voice=InputFile(io.BytesIO(audio), filename="sneha.mp3"))
    except Exception as e:
        await update.message.reply_text(f"❌ Voice error: {str(e)[:200]}")


# ============================================================
# MESSAGE HANDLING
# ============================================================
def has_telegram_link(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[a-zA-Z0-9_]+", text)) or bool(re.search(r"@[a-zA-Z0-9_]{4,}", text))


def analyze_mentions(update: Update, context, text: str):
    bot_un = (context.bot.username or "").lower()
    bot_id = context.bot.id
    mentioned = other = False
    for ent in (update.message.entities or []):
        if ent.type == "mention":
            if update.message.parse_entity(ent).lower() == f"@{bot_un}":  # UTF-16 offsets sahi handle
                mentioned = True
            else:
                other = True
        elif ent.type == "text_mention":
            if ent.user and ent.user.id == bot_id:
                mentioned = True
            else:
                other = True
    rm = update.message.reply_to_message
    reply_to_bot = bool(rm and rm.from_user and rm.from_user.id == bot_id)
    return mentioned, other, reply_to_bot


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _handle_inner(update, context)
    except Exception as e:
        logger.error(f"top-level catch: {e}", exc_info=e)


async def _bio_check(update, context, chat, user_id) -> bool:
    """True = warning bheji gayi, aage reply mat do."""
    if user_id in bio_checked_users:
        return False
    db_checked, db_warn = await get_bio_state_async(user_id)
    bio_checked_users.add(user_id)
    if db_checked:
        user_warning_count[user_id] = db_warn
        return False
    try:
        full = await context.bot.get_chat(user_id)
        if has_telegram_link(full.bio or ""):
            try:
                m = await context.bot.get_chat_member(chat.id, user_id)
                if m.status in ("administrator", "creator"):
                    await save_bio_state_async(user_id, True, user_warning_count.get(user_id, 0))
                    return False
            except Exception:
                pass
            count = user_warning_count.get(user_id, 0)
            if count < 1:
                await safe_reply_text(
                    update,
                    '<tg-emoji emoji-id="5371007876691138460">🥹</tg-emoji> <b>Baby, please remove the Telegram link from your bio!</b>\n'
                    '<tg-emoji emoji-id="5372811453717813644">😡</tg-emoji> <b>Promotion is not allowed here.</b>\n\n'
                    '<tg-emoji emoji-id="5217614738917173774">🙏</tg-emoji> @admin check please!',
                    parse_mode="HTML")
                user_warning_count[user_id] = count + 1
                await save_bio_state_async(user_id, True, count + 1)
                return True
        await save_bio_state_async(user_id, True, user_warning_count.get(user_id, 0))
    except Exception as e:
        logger.warning(f"bio check fail {user_id}: {e}")
    return False


async def _handle_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_user or not update.effective_chat or update.effective_user.is_bot:
        return
    chat, user = update.effective_chat, update.effective_user
    if chat.type not in ("private", "group", "supergroup"):
        return
    is_sticker = bool(msg.sticker and not msg.text)
    is_voice = bool(msg.voice)
    is_other_media = bool(msg.photo or msg.video or msg.video_note or msg.document or msg.animation)
    if not (msg.text or is_sticker or is_voice or is_other_media):
        return

    is_group = chat.type in ("group", "supergroup")
    if is_group:
        _spawn(save_active_group_async(chat.id, chat.title or "Unknown Group"))

    if msg.date and time.time() - msg.date.timestamp() > MAX_MESSAGE_AGE:
        logger.info("Bahut purana message ignore kiya.")
        return

    user_id = user.id
    if is_sticker and user_id == OWNER_ID and chat.type == "private":
        await save_sticker_if_owner(update, OWNER_ID)
        return

    text = msg.text or ""

    # WordSeek guess: admin/mention gating se PEHLE, warna non-admin groups me game chalta hi nahi tha
    if text and not text.startswith("/"):
        r_text = (msg.reply_to_message.text if msg.reply_to_message and msg.reply_to_message.text else "")
        is_reply_to_board = any(m in r_text for m in ("-letter mode", "Daily WordSeek", "Game started! Guess the", "Hard mode:"))
        handled, reply_text, reply_to_user = engine.handle_guess(
            chat_id=chat.id, user_id=user_id, user_name=user.first_name or "Player",
            text=text, is_reply_to_board=is_reply_to_board)
        if handled:
            if reply_text:
                try:
                    await msg.reply_text(reply_text, reply_to_message_id=msg.message_id if reply_to_user else None)
                except Exception:
                    await msg.reply_text(reply_text)
            return

    flood = check_flood(user_id, is_sticker=is_sticker)
    if flood == "cooldown":
        return
    if flood == "flood":
        await safe_reply_text(update, "Ruko ruko baby! 😤 Itni jaldi kya hai? 2 minute baad aana!")
        return

    mentioned, other_mentions, reply_to_bot = analyze_mentions(update, context, text)
    called_by_name = bool(re.search(r"\bsneha\b", text, re.I))
    addressed = mentioned or reply_to_bot or (is_group and called_by_name)
    is_standalone = not (msg.reply_to_message or mentioned or msg.forward_origin)

    if is_group:
        if other_mentions and not addressed:
            return
        if not await is_bot_admin(context, chat.id):
            if addressed:
                now_ts = time.time()
                if now_ts >= admin_need_reply_cooldown.get(chat.id, 0):
                    admin_need_reply_cooldown[chat.id] = now_ts + 300
                    await safe_reply_text(
                        update,
                        '<tg-emoji emoji-id="5217614738917173774">🙏</tg-emoji> <b>Admin Rights Needed!</b>\n\n'
                        'Mujhe admin do tabhi main naye members ka welcome kar paungi, '
                        'aur aapke group ko fun, flirty &amp; alive banaungi! <tg-emoji emoji-id="5427161992811004191">☺️</tg-emoji>\n\n'
                        '<i>Admin banao aur magic dekho!</i> <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
                        parse_mode="HTML")
            return
        if is_standalone:
            if random.random() > GROUP_REPLY_CHANCE:
                return
        elif not addressed:
            return

    # ----- Sticker -----
    if is_sticker:
        rm = msg.reply_to_message
        if rm and rm.from_user and rm.from_user.id != context.bot.id and not addressed:
            return
        reply_type, reply_content = get_random_sticker_reply()
        await realistic_typing_delay(context, chat.id, "")
        try:
            if reply_type == "sticker":
                await msg.reply_sticker(sticker=reply_content)
            else:
                await safe_reply_text(update, reply_content)
        except Exception as e:
            logger.warning(f"Sticker/Text reply fail: {e}")
        return

    # ----- Photo / video / file (private me hi) -----
    if is_other_media and not is_voice:
        if chat.type == "private":
            await safe_reply_text(update, random.choice([
                "Net slow hai yaar, ye load hi nahi ho raha 😪 batao na kya hai isme?",
                "Hmm dikh nahi raha mujhe thik se 😦 tum hi bata do kya bheja hai?",
                "Arey wah kuch bheja hai! par mujhe khulta nahi, text me bolo na 🥰"]))
        return

    typing_task = asyncio.create_task(_keep_typing(context, chat.id))
    try:
        extra_hint = ""
        # ----- Voice -----
        if is_voice:
            heard = await transcribe_voice(context, msg.voice)
            if not heard:
                await safe_reply_text(update, "Voice samajh nahi aaya yaar 😪 type karke bhej do na")
                return
            text = heard
            extra_hint = "User ne voice note bheja tha, upar ka message uska transcript hai."

        if not text:
            return
        if is_group and await _bio_check(update, context, chat, user_id):
            return

        clean_text = re.sub(r"@\w+\s*", "", text).strip() or "Hi"

        async with _user_locks[user_id]:
            gap = await touch_user(user_id, chat.type == "private")
            history = await get_history_async(user_id)
            await send_sneha_reaction(update)
            reply = await get_reply_with_live_typing(
                context, chat.id,
                get_ai_reply(clean_text, user_id, user.first_name or "dost", history, gap, extra_hint),
                existing_typing_task=typing_task)
            if not reply:
                if chat.type == "private" and time.time() - _busy_notice_ts.get(user_id, 0) > 60:
                    _busy_notice_ts[user_id] = time.time()
                    await safe_reply_text(update, random.choice(BUSY_REPLIES))
                return
            update_history(user_id, clean_text, reply, telegram_name=user.first_name, chat_id=chat.id)
            if not await try_voice_reply(update, context, clean_text, reply, is_voice, chat.type == "private"):
                await send_reply_bubbles(update, context, reply)
    finally:
        if not typing_task.done():
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass


# ============================================================
# WELCOME / MEMBERSHIP
# ============================================================
WELCOME_MESSAGES = [
    '{name} hello welcome hai aapka! Kaise ho? <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    '{name} welcome! Kya haal chaal hain? <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    'Woow {name} aa gaye, swagat hai aapka! <tg-emoji emoji-id="5801018335919347111">🎉</tg-emoji>',
    '{name} arey aap aa gaye! Welcome to the group <tg-emoji emoji-id="6332617871348210023">🌸</tg-emoji>',
    'Hii {name}! Group me swagat hai tumhara <tg-emoji emoji-id="5801018335919347111">🎉</tg-emoji>',
    '{name} welcome welcome! Mazaa aayega ab yahan <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    'Oye {name} aa gaya! Kaisa hai tu? <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    '{name} ji aapka hardik swagat hai group me! <tg-emoji emoji-id="6332617871348210023">🌸</tg-emoji>',
    'Naya member! {name} welcome to the family <tg-emoji emoji-id="5801018335919347111">🎉</tg-emoji>',
    '{name} hey! Kaise ho, sab badhiya? <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    'Welcome {name}! Ab masti shuru hogi <tg-emoji emoji-id="6318642082126763758">😘</tg-emoji>',
    '{name} aa gaye aap! Group me maza aayega ab <tg-emoji emoji-id="6334360245090915308">🔥</tg-emoji>',
    'Hello {name}! Group join karne ke liye shukriya <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    '{name} welcome! Sabse mil lo, sab friendly hain yahan <tg-emoji emoji-id="6332617871348210023">🌸</tg-emoji>',
    'Are wah {name}! Swagat hai tumhara yahan <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    '{name} kaise ho? Welcome to our group! <tg-emoji emoji-id="6143155267509948558">✨</tg-emoji>',
    'Yayy {name} aa gaye! Ab group aur mazedaar <tg-emoji emoji-id="5801018335919347111">🎉</tg-emoji>',
    '{name} welcome dost! Enjoy karo yahan <tg-emoji emoji-id="6332617871348210023">🌸</tg-emoji>',
    'Hey {name}! Naye member ka swagat hai <tg-emoji emoji-id="5801018335919347111">🎉</tg-emoji>',
    '{name} aapka is group me dil se swagat hai! <tg-emoji emoji-id="6318642082126763758">😘</tg-emoji>',
    'Salaam {name}! Group me aane ke liye welcome <tg-emoji emoji-id="6332617871348210023">🌸</tg-emoji>',
    '{name} welcome yaar! Kaisa chal raha hai sab? <tg-emoji emoji-id="6334360245090915308">🔥</tg-emoji>',
]


def get_welcome_message(name: str) -> str:
    return random.choice(WELCOME_MESSAGES).format(name=name)


def _should_welcome(chat_id: int, user_id: int) -> bool:
    d = _welcomed_users.setdefault(chat_id, {})
    now = time.time()
    if now - d.get(user_id, 0) < 3600:  # pehle set tha: leave+rejoin karne pe kabhi dobara welcome nahi hota tha
        return False
    d[user_id] = now
    return True


def _welcome_name(u) -> str:
    if u.username:
        return f"@{u.username}"
    return f'<a href="tg://user?id={u.id}">{html.escape(u.first_name or "Dost")}</a>'


async def new_member_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.message.new_chat_members:
            return
        chat = update.effective_chat
        if not await is_bot_admin(context, chat.id):
            return
        for u in update.message.new_chat_members:
            if u.is_bot or u.id == OWNER_ID or not _should_welcome(chat.id, u.id):
                continue
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await update.message.reply_text(get_welcome_message(_welcome_name(u)), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"new_member_welcome error: {e}")


async def chat_member_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        r = update.chat_member
        chat = update.effective_chat
        if not r or not chat or chat.type not in ("group", "supergroup"):
            return
        if r.old_chat_member.status in ("member", "administrator", "creator"):
            return
        if r.new_chat_member.status not in ("member", "administrator"):
            return
        u = r.new_chat_member.user
        if u.is_bot or u.id == OWNER_ID or not await is_bot_admin(context, chat.id) or not _should_welcome(chat.id, u.id):
            return
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await context.bot.send_message(chat_id=chat.id, text=get_welcome_message(_welcome_name(u)), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"chat_member_welcome error: {e}")


async def bot_membership_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        r = update.my_chat_member
        chat = update.effective_chat
        if not r or not chat:
            return
        new, old = r.new_chat_member.status, r.old_chat_member.status
        if chat.type == "private":
            if new in ("kicked", "left"):  # user ne bot block kiya, check-in band
                await adb(mark_checkin, r.from_user.id, True)
            elif new == "member" and old in ("kicked", "left"):
                await adb(touch_user_state, r.from_user.id, time.time())
            return
        if chat.type not in ("group", "supergroup"):
            return
        if new in ("left", "kicked", "banned"):
            await delete_active_group_async(chat.id)
            chat_admin_cache.pop(chat.id, None)
            _welcomed_users.pop(chat.id, None)
            logger.info(f"🗑️ Bot {new} from group {chat.id} ({chat.title})")
        elif new in ("member", "administrator") and old in ("left", "kicked", "banned"):
            await save_active_group_async(chat.id, chat.title or "Unknown Group")
            chat_admin_cache.pop(chat.id, None)
        elif new in ("member", "administrator"):
            chat_admin_cache.pop(chat.id, None)
    except Exception as e:
        logger.warning(f"bot_membership_update error: {e}")


async def master_button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, RetryAfter):
        logger.warning(f"TG rate limit, sleep {error.retry_after}s")
        await asyncio.sleep(error.retry_after)
    elif isinstance(error, TimedOut):
        logger.warning("TG timeout, ignoring...")
    else:
        logger.error("Unhandled:", exc_info=error)


# ============================================================
# BACKGROUND WATCHERS
# ============================================================
async def daily_reset_watcher():
    while True:
        try:
            reset_daily_if_new_day()
        except Exception as e:
            logger.error(f"daily_reset_watcher error: {e}", exc_info=e)
        await asyncio.sleep(60)


async def idle_memory_flush_watcher():
    IDLE, FORGET_AFTER = 180, 6 * 3600
    while True:
        try:
            now = time.time()
            for uid, last in list(_last_activity.items()):
                idle = now - last
                if idle < IDLE:
                    continue
                count = user_msg_counter.get(uid, 0)
                history = conversation_memory.get(uid, [])
                if count > _last_summarized_count.get(uid, 0) and len(history) >= 4:
                    _spawn(generate_summary(uid, list(history)))
                    _last_summarized_count[uid] = count
                if idle > FORGET_AFTER:  # RAM leak roko, DB me sab saved hai
                    for d in (_last_activity, conversation_memory, user_msg_counter, _last_summarized_count, _last_used_emoji, _busy_notice_ts):
                        d.pop(uid, None)
        except Exception as e:
            logger.error(f"idle_memory_flush_watcher error: {e}", exc_info=e)
        await asyncio.sleep(60)


async def proactive_checkin_watcher(bot):
    """Kaafi din gayab user ko memory se juda ek chhota sa DM (Nova jaisa). Ek absence me sirf ek baar."""
    await asyncio.sleep(120)
    while True:
        try:
            hr = datetime.now(IST).hour
            if DATABASE_URL and 11 <= hr < 21:
                for uid in await adb(get_checkin_candidates, 4):
                    summary = await adb(get_user_summary, uid)
                    f = _parse_summary_fields(summary)
                    if f.get("facts", "").lower() in _EMPTY_VALUES and f.get("ongoing", "none").lower() in _EMPTY_VALUES:
                        await adb(mark_checkin, uid)
                        continue
                    await adb(mark_checkin, uid)  # pehle mark, taaki fail hone pe loop na bane
                    prompt = (f"Ye user kaafi dino se gayab hai. Uski memory:\n{summary}\n\n"
                              "Ek chhota (1-2 line) natural check-in message likho jo Ongoing ya Facts ki kisi ek cheez ke baare me poochhe "
                              "(jaise 'wo kaam ka kya hua?'), thoda tease karte hue. Naam mat likhna agar pakka na ho. User se izzat wale 'aap' me baat karo. Coffee/plan/music ka zikr nahi. Sirf message likho."
                              + gender_hint(await get_gender_known(uid)))
                    text = await _ai_fun(prompt, 150)
                    if not text:
                        continue
                    text = postprocess_reply(text, "", uid)
                    if not text:
                        continue
                    try:
                        ents = build_premium_emoji_entities(text, CHAT_PREMIUM_EMOJIS)
                        await bot.send_message(chat_id=uid, text=text, entities=ents or None)
                        update_history(uid, None, text)
                        logger.info(f"💌 Check-in bheja {uid}")
                    except Forbidden:
                        await adb(mark_checkin, uid, True)
                    except Exception as e:
                        logger.warning(f"checkin send fail {uid}: {e}")
                    await asyncio.sleep(4)
        except Exception as e:
            logger.error(f"proactive_checkin_watcher error: {e}", exc_info=e)
        await asyncio.sleep(900)


async def keepalive_watcher(url: str):
    """Render free plan 15 min idle pe sleep ho jaata hai. Khud ko ping karte raho (UptimeRobot bhi lagao backup ke liye)."""
    import httpx
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=15) as c:
        while True:
            try:
                await c.get(url)
            except Exception:
                pass
            await asyncio.sleep(600)


BOT_COMMANDS = [
    ("start", "Start / welcome"), ("help", "Kya kya kar sakti hoon"), ("roast", "Mazedaar roast"),
    ("shayari", "Ek chhoti shayari"), ("joke", "Ek joke"), ("ship", "Couple meter"),
    ("truth", "Truth question"), ("dare", "Dare challenge"), ("kiss", "Kiss GIF"), ("slap", "Slap GIF"), ("hug", "Hug GIF"), ("crush", "Crush level check"), ("new", "WordSeek game shuru"),
    ("lb", "Leaderboard"), ("memory", "Main tumhare baare me kya jaanti hoon"), ("forget", "Meri memory se data delete"),
]


async def main() -> None:
    await adb(init_db)
    tasks = [asyncio.create_task(daily_reset_watcher()), asyncio.create_task(idle_memory_flush_watcher())]

    application = (Application.builder().token(BOT_TOKEN)
                   .read_timeout(30).write_timeout(30).connect_timeout(30).pool_timeout(30)
                   .concurrent_updates(True).build())

    for name, fn in [("start", start), ("help", help_command), ("ping", ping_command), ("stats", stats_command),
                     ("resetkeys", resetkeys_command), ("memory", memory_command), ("forget", forget_command),
                     ("dbcheck", dbcheck_command), ("backup", backup_command), ("migrate_memory", migrate_memory_command),
                     ("syncgroup", syncgroup_command), ("broadcast", broadcast_command),
                     ("broadcaststats", broadcast_stats_command), ("broadcastgc", broadcastgc_command),
                     ("roast", roast_command), ("shayari", shayari_command), ("joke", joke_command),
                     ("truth", truth_command), ("dare", dare_command), ("ship", ship_command), ("voice", voice_command), ("gender", gender_command), ("kiss", kiss_command), ("slap", slap_command),
                     ("hug", hug_command), ("pat", pat_command), ("crush", crush_command),
                     ("wordseek", wordseek_help), ("new", wordseek_new), ("new4", wordseek_new4),
                     ("new5", wordseek_new5), ("new6", wordseek_new6), ("end", wordseek_end),
                     ("leaderboard", wordseek_lb), ("lb", wordseek_lb), ("top", wordseek_lb)]:
        application.add_handler(CommandHandler(name, fn))

    application.add_handler(CallbackQueryHandler(master_button_router))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_welcome))
    application.add_handler(ChatMemberHandler(chat_member_welcome, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(bot_membership_update, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.Sticker.ALL | filters.VOICE | filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE
         | filters.Document.ALL | filters.ANIMATION) & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    allowed = ["message", "chat_member", "my_chat_member", "callback_query"]
    port = int(os.environ.get("PORT", 8000))
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")

    async def post_start():
        try:
            await application.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        except Exception as e:
            logger.warning(f"set_my_commands fail: {e}")
        if PROACTIVE_CHECKINS:
            tasks.append(asyncio.create_task(proactive_checkin_watcher(application.bot)))

    try:
        if webhook_url:
            logger.info(f"WEBHOOK mode -> {webhook_url}/webhook")
            from starlette.applications import Starlette
            from starlette.responses import PlainTextResponse
            from starlette.requests import Request
            from starlette.routing import Route
            import uvicorn

            # Secret ke bina koi bhi fake update (OWNER_ID ke naam se /broadcast tak) bhej sakta tha
            secret = hashlib.sha256(("wh:" + BOT_TOKEN).encode()).hexdigest()[:40]

            async def health(r: Request) -> PlainTextResponse:
                return PlainTextResponse("Bot is alive!")

            async def _process_update_safe(upd):
                try:
                    await application.process_update(upd)
                except Exception as e:
                    logger.error(f"🔥 process_update crashed: {e}", exc_info=e)

            async def tg_webhook(r: Request) -> PlainTextResponse:
                if r.headers.get("x-telegram-bot-api-secret-token") != secret:
                    return PlainTextResponse("forbidden", status_code=403)
                upd = Update.de_json(await r.json(), application.bot)
                _spawn(_process_update_safe(upd))
                return PlainTextResponse("OK")

            app = Starlette(routes=[Route("/", health, methods=["GET", "HEAD"]),
                                    Route("/webhook", tg_webhook, methods=["POST"])])
            await application.initialize()
            await application.start()
            await application.bot.set_webhook(url=f"{webhook_url}/webhook", allowed_updates=allowed,
                                              secret_token=secret, max_connections=40)
            await post_start()
            tasks.append(asyncio.create_task(keepalive_watcher(webhook_url)))
            await uvicorn.Server(uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="warning")).serve()
        else:
            logger.info("POLLING mode")
            await application.initialize()
            await application.bot.delete_webhook(drop_pending_updates=True)
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True, allowed_updates=allowed)
            await post_start()
            await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
            break
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            logger.error(f"🔥 main() crashed, restarting in 5s: {e}", exc_info=e)
            time.sleep(5)
