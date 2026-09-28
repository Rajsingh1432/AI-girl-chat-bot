import os
import json
import time
import random
import psycopg2
import zlib
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from typing import Optional, Tuple, Dict, Any, List

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

class WordSeekEngine:
    def __init__(self, data_dir: Optional[str] = None):
        if data_dir is None:
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
        self.data_dir = data_dir
        self._dict_cache: Dict[int, Dict[str, Any]] = {}
        self._init_db()

    def _get_db(self):
        if not DATABASE_URL: return None
        return psycopg2.connect(DATABASE_URL)

    def _init_db(self):
        if not DATABASE_URL: return
        try:
            with self._get_db() as conn:
                c = conn.cursor()
                c.execute("""CREATE TABLE IF NOT EXISTS wordseek_games (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'normal',
                    word_length INTEGER NOT NULL DEFAULT 5,
                    target_word TEXT NOT NULL,
                    guesses_json TEXT DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    started_by BIGINT NOT NULL,
                    winner_id BIGINT DEFAULT NULL,
                    max_guesses INTEGER NOT NULL DEFAULT 30,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );""")
                c.execute("""CREATE TABLE IF NOT EXISTS wordseek_leaderboard (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    user_name TEXT DEFAULT '',
                    score INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    max_streak INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id)
                );""")
                conn.commit()
        except Exception as e:
            print(f"WordSeek DB Init Error: {e}")

    def get_words(self, length: int) -> Dict[str, Any]:
        if length in self._dict_cache:
            return self._dict_cache[length]

        dict_file = os.path.join(self.data_dir, f'words_{length}.json')
        target_file = os.path.join(self.data_dir, f'targets_{length}.json')

        dict_words: List[str] = []
        target_words: List[str] = []

        if os.path.isfile(dict_file):
            with open(dict_file, 'r', encoding='utf-8') as f:
                dict_words = json.load(f)

        if os.path.isfile(target_file):
            with open(target_file, 'r', encoding='utf-8') as f:
                target_words = json.load(f)

        if not target_words:
            target_words = dict_words[:]

        all_words_set = set(w.upper() for w in dict_words)
        for t in target_words:
            all_words_set.add(t.upper())

        targets_upper = [t.upper() for t in target_words]

        self._dict_cache[length] = {
            'dict': all_words_set,
            'targets': targets_upper
        }
        return self._dict_cache[length]

    @staticmethod
    def to_bold_sans(text: str) -> str:
        out = []
        for ch in text.upper():
            if 'A' <= ch <= 'Z':
                code = 0x1D5D4 + (ord(ch) - ord('A'))
                out.append(chr(code))
            else:
                out.append(ch)
        return ''.join(out)

    @staticmethod
    def evaluate_guess(target: str, guess: str) -> str:
        target_chars = list(target.upper())
        guess_chars = list(guess.upper())
        length = len(target_chars)

        hints = ['🟥'] * length
        matched = [False] * length

        for i in range(length):
            if guess_chars[i] == target_chars[i]:
                hints[i] = '🟩'
                matched[i] = True

        for i in range(length):
            if hints[i] == '🟩':
                continue
            for j in range(length):
                if not matched[j] and guess_chars[i] == target_chars[j]:
                    hints[i] = '🟨'
                    matched[j] = True
                    break

        return ' '.join(hints)

    @staticmethod
    def validate_hard_mode(target: str, previous_guesses: List[Dict[str, Any]], new_guess: str) -> Optional[str]:
        target_chars = list(target.upper())
        new_chars = list(new_guess.upper())
        length = len(target_chars)

        confirmed_greens: Dict[int, str] = {}
        required_letters: set = set()

        for g in previous_guesses:
            p_word = g.get('guess', '').upper()
            if len(p_word) != length:
                continue
            p_chars = list(p_word)
            matched = [False] * length

            for i in range(length):
                if p_chars[i] == target_chars[i]:
                    confirmed_greens[i] = p_chars[i]
                    matched[i] = True

            for i in range(length):
                if p_chars[i] == target_chars[i]:
                    continue
                for j in range(length):
                    if not matched[j] and p_chars[i] == target_chars[j]:
                        required_letters.add(p_chars[i])
                        matched[j] = True
                        break

        ordinal = {0: '1st', 1: '2nd', 2: '3rd', 3: '4th', 4: '5th', 5: '6th'}

        for pos, char in confirmed_greens.items():
            if pos >= len(new_chars) or new_chars[pos] != char:
                ord_name = ordinal.get(pos, f'{pos+1}th')
                return f"{ord_name} letter must be {char}"

        for char in required_letters:
            if char not in new_chars:
                return f"Guess must contain {char}"

        return None

    def start_game(self, chat_id: int, user_id: int, word_length: int = 5, mode: str = 'normal') -> Tuple[bool, str]:
        if word_length not in (4, 5, 6):
            word_length = 5

        with self._get_db() as conn:
            c = conn.cursor(cursor_factory=RealDictCursor)
            c.execute("SELECT * FROM wordseek_games WHERE chat_id = %s AND status = 'active' LIMIT 1", (chat_id,))
            active = c.fetchone()

            if active:
                if active['mode'] == 'daily':
                    return False, "⚠️ Daily WordSeek is currently active!\nUse /pausedaily to return to normal games."
                else:
                    return False, f"⚠️ A {active['word_length']}-letter game is already in progress!\nGuess the word or use /end to finish it."

            words = self.get_words(word_length)
            targets = words['targets']
            if not targets:
                return False, "❌ Word database error!"

            target_word = random.choice(targets).upper()
            max_guesses = 30

            c.execute("""
                INSERT INTO wordseek_games (chat_id, mode, word_length, target_word, guesses_json, status, started_by, max_guesses)
                VALUES (%s, %s, %s, %s, '[]', 'active', %s, %s)
            """, (chat_id, mode, word_length, target_word, user_id, max_guesses))
            conn.commit()

        return True, f"Game started! Guess the {word_length}-letter word!"

    def end_game(self, chat_id: int, user_id: int, is_admin: bool = False) -> Tuple[bool, str]:
        with self._get_db() as conn:
            c = conn.cursor(cursor_factory=RealDictCursor)
            c.execute("SELECT * FROM wordseek_games WHERE chat_id = %s AND status = 'active' LIMIT 1", (chat_id,))
            game = c.fetchone()

            if not game:
                return False, "⚠️ No active WordSeek game in this chat!\nStart one using /new, /new4, /new5, or /new6"

            if not is_admin and game['started_by'] != user_id:
                return False, "❌ Only group admins or the game starter can end the game!"

            c.execute("UPDATE wordseek_games SET status = 'lost' WHERE id = %s", (game['id'],))
            conn.commit()
            target = game['target_word'].lower()

        return True, f"🛑 Game ended!\nThe word was: {target}\n\nStart a new game anytime with /new"

    def handle_guess(self, chat_id: int, user_id: int, user_name: str, text: str, is_reply_to_board: bool = False) -> Tuple[bool, Optional[str], bool]:
        clean_text = text.strip()
        if not clean_text or clean_text.startswith('/') or ' ' in clean_text:
            return False, None, False

        guess = clean_text.upper()
        if not guess.isalpha() or len(guess) not in (4, 5, 6):
            return False, None, False

        with self._get_db() as conn:
            c = conn.cursor(cursor_factory=RealDictCursor)
            c.execute("SELECT * FROM wordseek_games WHERE chat_id = %s AND status = 'active' LIMIT 1", (chat_id,))
            game = c.fetchone()

            if not game:
                return False, None, False

            target_word = game['target_word'].upper()
            word_length = game['word_length']
            max_guesses = game['max_guesses']
            mode = game['mode']

            if len(guess) != word_length:
                if is_reply_to_board:
                    return True, f"⚠️ Guess must be exactly {word_length} letters!", True
                return False, None, False

            words_data = self.get_words(word_length)
            if guess not in words_data['dict']:
                if is_reply_to_board:
                    return True, f'⚠️ "{guess}" is not in the word list!', True
                return False, None, False

            guesses = json.loads(game['guesses_json']) if game['guesses_json'] else []

            for g in guesses:
                if g.get('guess', '').upper() == guess:
                    return True, f'⚠️ "{guess}" has already been guessed!', True

            hard_mode_err = self.validate_hard_mode(target_word, guesses, guess)
            if hard_mode_err:
                return True, f"⚠️ Hard mode: {hard_mode_err}.", True

            hint = self.evaluate_guess(target_word, guess)
            bold_word = self.to_bold_sans(guess)
            line = f"{hint} {bold_word}"

            guesses.append({
                'guess': guess,
                'line': line,
                'user': user_id
            })

            guess_count = len(guesses)
            is_correct = (guess == target_word)
            is_out_of_guesses = (guess_count >= max_guesses)

            board_output = '\n'.join([g['line'] for g in guesses])

            if is_correct:
                c.execute("UPDATE wordseek_games SET status = 'won', winner_id = %s, guesses_json = %s WHERE id = %s",
                            (user_id, json.dumps(guesses), game['id']))
                points = max(5, (max_guesses - guess_count + 1))
                if word_length == 6:
                    points = int(round(points * 1.2))
                elif word_length == 4:
                    points = int(round(points * 0.8))

                self._record_win(conn, chat_id, user_id, user_name, points)
                conn.commit()

                win_msg = (
                    f"Congrats! You guessed it correctly.\n"
                    f"Correct Word: {target_word.lower()}\n"
                    f"Added {points} to the leaderboard.\n"
                    f"Start with /new{word_length}"
                )
                return True, win_msg, True

            elif is_out_of_guesses:
                c.execute("UPDATE wordseek_games SET status = 'lost', guesses_json = %s WHERE id = %s",
                            (json.dumps(guesses), game['id']))
                conn.commit()

                loss_msg = (
                    f"{word_length}-letter mode · {guess_count}/{max_guesses}\n\n"
                    f"{board_output}\n\n"
                    f"😔 Game over! Out of guesses.\n"
                    f"The correct word was: {target_word.lower()}\n"
                    f"Start again with /new{word_length}"
                )
                return True, loss_msg, False

            else:
                c.execute("UPDATE wordseek_games SET guesses_json = %s WHERE id = %s",
                            (json.dumps(guesses), game['id']))
                conn.commit()

                mode_title = 'Daily WordSeek' if mode == 'daily' else f"{word_length}-letter mode"
                board_msg = f"{mode_title} · {guess_count}/{max_guesses}\n\n{board_output}"
                return True, board_msg, False

    def _record_win(self, conn, chat_id: int, user_id: int, user_name: str, points: int):
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM wordseek_leaderboard WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
        row = c.fetchone()

        if row:
            new_score = row['score'] + points
            new_wins = row['wins'] + 1
            new_games = row['games_played'] + 1
            new_streak = row['streak'] + 1
            max_streak = max(new_streak, row['max_streak'])
            c.execute("""
                UPDATE wordseek_leaderboard 
                SET score = %s, wins = %s, games_played = %s, streak = %s, max_streak = %s, user_name = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (new_score, new_wins, new_games, new_streak, max_streak, user_name, row['id']))
        else:
            c.execute("""
                INSERT INTO wordseek_leaderboard (chat_id, user_id, user_name, score, wins, games_played, streak, max_streak)
                VALUES (%s, %s, %s, %s, 1, 1, 1, 1)
            """, (chat_id, user_id, user_name, points))

    def get_leaderboard(self, chat_id: int, group_title: str = 'Group') -> str:
        with self._get_db() as conn:
            c = conn.cursor(cursor_factory=RealDictCursor)
            c.execute("""
                SELECT * FROM wordseek_leaderboard
                WHERE chat_id = %s
                ORDER BY score DESC, wins DESC
                LIMIT 10
            """, (chat_id,))
            rows = c.fetchall()

        if not rows:
            return (
                f"🏆 WordSeek Leaderboard · {group_title}\n\n"
                f"No scores recorded in this group yet!\n"
                f"Start a game with /new to be the first on the board! 🚀"
            )

        medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
        lines = []

        for i, row in enumerate(rows):
            rank = medals[i] if i < len(medals) else f"#{i+1}"
            name = row['user_name'] or f"User {row['user_id']}"
            score = f"{row['score']:,}"
            wins = row['wins']
            streak = row['streak']
            streak_txt = f" 🔥{streak}" if streak > 1 else ""
            lines.append(f"{rank} {name} — {score} pts ({wins} wins{streak_txt})")

        return f"🏆 WordSeek Leaderboard · {group_title}\n\n" + '\n'.join(lines) + "\n\nPlay more games with /new to climb the ranks!"

    @staticmethod
    def get_help() -> str:
        return (
            "▸ How to Play WordSeek\n\n"
            "1. Start a game using /new, /new4, /new5, or /new6\n"
            "2. Guess the hidden word\n"
            "3. After each guess, you'll get color hints:\n"
            "   🟩 Correct letter in the right spot\n"
            "   🟨 Correct letter in the wrong spot\n"
            "   🟥 Letter not in the word\n"
            "4. First person to guess correctly wins!\n"
            "5. Maximum 30 guesses per game\n\n"
            "Word Length Modes:\n"
            "• /new → Start default 5-letter game\n"
            "• /new 4 → Start specific length (4, 5, or 6)\n"
            "• /new4 → Start 4-letter game\n"
            "• /new5 → Start 5-letter game\n"
            "• /new6 → Start 6-letter game\n\n"
            "Basic Commands:\n"
            "• /new - Start a new game (default 5 letters)\n"
            "• /end - End current game (starter or admin only)\n"
            "• /leaderboard (or /lb, /top) - Top 10 players in this group\n\n"
        )
