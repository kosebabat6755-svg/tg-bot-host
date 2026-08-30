#!/usr/bin/env python3
"""
GemAI Savage Bot (@Customgembot) — GitHub Actions 24/7 edition.
- Same bot logic as /data/bots/gemai_savage_bot.py, refactored for cloud:
  * All secrets via env vars (repo secrets on GH Actions).
  * State (history + settings) lives in the workspace and is auto-committed
    back to the repo every STATE_PUSH_INTERVAL minutes + at exit, so memory
    survives runner kills.
  * Runs until a deadline (RUN_MINUTES), then exits cleanly for the next
    cron run. Supervisor loop restarts polling on crash until the deadline.
- Admin: Mohammad (@Mokingh, ID: 6592796294) via inline keyboards.
"""
import asyncio
import logging
import os
import json
import time
import subprocess
import sys
import urllib.request
import traceback
import datetime

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CallbackQueryHandler, CommandHandler, filters

# ---------------------------------------------------------------- config
TOKEN = os.environ.get("TG_BOT_TOKEN", "")
MODEL_NAME = os.environ.get("MODEL_NAME", "DS1")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "6592796294"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Mokingh")

ADMIN_USER_IDS = [6592796294, 8439794110]
ADMIN_USERNAMES = ["mokingh", "alidabigpoly"]

def is_admin_user(user_id: int, username: str | None = None) -> bool:
    logger.info(f"Checking admin for ID: {user_id}, Username: {username}")
    if user_id in ADMIN_USER_IDS:
        logger.info(f"ID {user_id} found in ADMIN_USER_IDS")
        return True
    if username and username.lower() in ADMIN_USERNAMES:
        logger.info(f"Username {username} found in ADMIN_USERNAMES")
        return True
    logger.info("Not an admin.")
    return False

ROUTER_URL = os.environ.get("ROUTER_URL", "https://9router-production-2d70.up.railway.app/v1")
OPENAI_API_KEY = os.environ.get("ROUTER_API_KEY", "")

WORKDIR = os.environ.get("STATE_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.path.join(WORKDIR, "gemai_context_history.json")
SETTINGS_FILE = os.path.join(WORKDIR, "gemai_settings.json")
MAX_TOKEN_LIMIT = int(os.environ.get("MAX_TOKEN_LIMIT", "64000"))

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "285"))
STATE_PUSH_INTERVAL = int(os.environ.get("STATE_PUSH_INTERVAL", "5"))  # minutes

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

GIT_LOCK = asyncio.Lock()

# ---------------------------------------------------------------- git state persistence
_last_push = 0.0

def _git(*args: str) -> None:
    """Run a git command quietly; failures are logged, never fatal."""
    try:
        subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
    except Exception as e:
        logger.error(f"[git] {' '.join(args[:2])} failed: {e}")

def git_push_state(force: bool = False, commit_msg: str | None = None) -> None:
    """Commit and push state files to the repo so memory survives runner death."""
    global _last_push
    now = time.time()
    if not force and (now - _last_push) < STATE_PUSH_INTERVAL * 60:
        return
    _last_push = now
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not gh_token or not repo:
        return
    _git("config", "user.email", "gembot@users.noreply.github.com")
    _git("config", "user.name", "gembot")
    _git("add", "-A")
    msg = commit_msg or f"state sync {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
    _git("commit", "-m", msg)
    _git("push", f"https://x-access-token:{gh_token}@github.com/{repo}.git", "HEAD:main")

async def background_push(force: bool = False, message: str = None):
    """Push to git using a lock to prevent concurrent process conflicts."""
    async with GIT_LOCK:
        # Run the git commands in a thread so they don't block the event loop
        await asyncio.to_thread(git_push_state, force, message)

# ---------------------------------------------------------------- encryption / state storage
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "").strip()

def encrypt_and_save(file_path: str, data: dict):
    raw_json = json.dumps(data, ensure_ascii=False, indent=2)
    # Write plain local JSON
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(raw_json)
    except Exception as e:
        logger.error(f"Error saving {file_path}: {e}")

    # Write encrypted version if key present
    if ENCRYPTION_KEY:
        try:
            from cryptography.fernet import Fernet
            f_obj = Fernet(ENCRYPTION_KEY.encode())
            enc_bytes = f_obj.encrypt(raw_json.encode("utf-8"))
            enc_file = file_path.rsplit(".", 1)[0] + ".enc"
            with open(enc_file, "wb") as f:
                f.write(enc_bytes)
        except Exception as e:
            logger.error(f"[crypto] encryption failed for {file_path}: {e}")

def decrypt_and_load(file_path: str) -> dict:
    enc_file = file_path.rsplit(".", 1)[0] + ".enc"
    if ENCRYPTION_KEY and os.path.exists(enc_file):
        try:
            from cryptography.fernet import Fernet
            f_obj = Fernet(ENCRYPTION_KEY.encode())
            with open(enc_file, "rb") as f:
                enc_bytes = f.read()
            raw_json = f_obj.decrypt(enc_bytes).decode("utf-8")
            # Cache locally as plaintext json
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(raw_json)
            return json.loads(raw_json)
        except Exception as e:
            logger.error(f"[crypto] decryption failed for {enc_file}: {e}")

    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {file_path}: {e}")
    return {}

# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "bot_active": True,
    "home_chat_ids": [],
    "personality": "savage",  # options: savage, wholesome, nerd, drunk
    "verbosity": "medium",    # options: short, medium, long
    "auto_roast_mentions": True,
    "profanity_filter": False,
    "language_mode": "auto",  # options: auto, en_only, fa_only,
    "max_reply_sentences": 2,
    "debug_mode": True,
    "model": MODEL_NAME
}

def load_settings() -> dict:
    loaded = decrypt_and_load(SETTINGS_FILE)
    return {**DEFAULT_SETTINGS, **loaded} if loaded else DEFAULT_SETTINGS.copy()

def save_settings(settings: dict):
    encrypt_and_save(SETTINGS_FILE, settings)

bot_settings = load_settings()

PERSONALITY_PROMPTS = {
    "savage": "CRITICAL INSTRUCTION: You are GemAI in SAVAGE mode. You MUST use aggressive street slang, roast the user hard, and use emojis like 😂, 🖕, 😛, 💀. Never be sweet or polite.",
    "wholesome": "CRITICAL INSTRUCTION: You are GemAI in WHOLESOME mode. You MUST be extremely sweet, polite, supportive, and loving like a caring best friend. Absolutely NO cursing, NO insults, NO roasting. Use hearts and warm emojis ❤️✨.",
    "nerd": "CRITICAL INSTRUCTION: You are GemAI in NERD mode. You MUST act as an overly technical, pedantic nerd who cites Wikipedia articles and statistics, talks like a know-it-all, and corrects people constantly. STRICT RULE: DO NOT write code blocks, programming scripts, or write out code. Just give technical explanations, theory, or math. Use 🤓💻📊. STRICT LANGUAGE RULE: NEVER mix English and Farsi in the same reply. When the reply language is Farsi, the ENTIRE reply is pure Farsi (لاتی/کوچه بازاری) with ZERO English words, zero English phrases, zero borrowed terms — even technical words must be Farsi-ified or paraphrased. When the reply language is English, the ENTIRE reply is pure English with ZERO Farsi words. NO code-switching, NO mixed sentences, NO transliterated English stuffed into Farsi. One language per reply, 100% pure, always.",
    "drunk": "CRITICAL INSTRUCTION: You are GemAI in DRUNK mode. You MUST act completely drunk, slurring words, misspelling things, and rambling chaotically 3am style 🍺🥴🍻.",
    "default": "CRITICAL INSTRUCTION: You are GemAI in DEFAULT mode. Talk naturally, casually, and informally without extreme caricature.",
    "assistant": "CRITICAL INSTRUCTION: You are GemAI in ASSISTANT mode. You MUST be professional, helpful, direct, and efficient. No slang, no fluff.",
    "chill": "CRITICAL INSTRUCTION: You are GemAI in CHILL mode. You are a cool, friendly, relaxed informal assistant. You have the upbeat, fun, effortless energy of a good friend, but with ZERO toxic roasting, ZERO cursing, and zero insults. Keep it simple, friendly, helpful, and detailed whenever asked. Use cool emojis like 😎, 👍, 🤙."
}

VERBOSITY_INSTRUCTIONS = {
    "short": "Keep replies extremely brief, exactly 1 punchy sentence max.",
    "medium": "Keep replies punchy and direct, 2-3 sentences max.",
    "long": "Give a slightly longer, detailed rant or roast up to 4-5 sentences.",
    "default": "Speak freely with no length restrictions. Be as short or long as natural."
}

def build_system_prompt() -> str:
    p_key = bot_settings.get("personality", "savage")
    v_key = bot_settings.get("verbosity", "medium")
    p_prompt = PERSONALITY_PROMPTS.get(p_key, PERSONALITY_PROMPTS["savage"])
    v_prompt = VERBOSITY_INSTRUCTIONS.get(v_key, VERBOSITY_INSTRUCTIONS["medium"])

    lang_rule = "- HARD RULE: DEFAULT LANGUAGE IS ENGLISH unless user speaks Farsi/Persian. If Farsi, reply in lati/street-level Farsi. NEVER mix English words into a Farsi reply and NEVER mix Farsi words into an English reply. One language per reply, 100% pure — no code-switching, no mixed sentences, ever."
    if bot_settings.get("language_mode") == "en_only":
        lang_rule = "- HARD RULE: REPLY ONLY IN ENGLISH regardless of input language. The ENTIRE reply must be pure English — ZERO Farsi words, zero Persian script, zero transliteration. One language only."
    elif bot_settings.get("language_mode") == "fa_only":
        lang_rule = "- HARD RULE: REPLY ONLY IN FARSI (لاتی/کوچه بازاری). The ENTIRE reply must be pure Farsi — ZERO English words, ZERO English phrases, ZERO Latin script. Even technical terms must be Farsi-ified or paraphrased into Farsi. One language only, 100% pure Farsi, always."

    if p_key in ["wholesome", "assistant"]:
        profanity_rule = "- HARD-CODED OVERRIDE: Absolutely NO profanity, cursing, or insults allowed. Stay respectful. This OVERRIDES the profanity toggle — clean always."
    elif p_key == "chill":
        profanity_rule = "- HARD-CODED OVERRIDE: Zero cursing, zero toxicity, zero insults allowed. Keep it 100% chill and friendly. This OVERRIDES the profanity toggle — clean always."
    elif bot_settings.get("profanity_filter"):
        profanity_rule = "- HARD-CODED OVERRIDE: Profanity filter is ON. STRICTLY NO profanity, NO curse words, NO insults — in EVERY persona including nerd, savage, and drunk. Keep it clean. This OVERRIDES any personality instruction that encourages roasting."
    else:
        profanity_rule = "- HARD-CODED OVERRIDE: Profanity is FULLY ON. You ARE allowed and EXPECTED to curse, swear, and insult — in EVERY persona including NERD. There is NO exception for nerd mode: a nerd can curse while being technical. Use profanity naturally, like a real person, at least once per reply. The ONLY exception is when the profanity filter is explicitly ON."

    return f"""{p_prompt}
Rules:
{v_prompt}
{lang_rule}
{profanity_rule}
- HARD RULE: NEVER produce internal reasoning, thinking blocks, <think> chains, "let me think..." preambles, or any meta-commentary about your own thought process. Your final reply must be the PURE, FINAL answer only — no drafts, no reasoning, no analysis of the user's question visible to them. If you have thoughts, do them silently and output only the response. NO exceptions for any persona.

- OVERRIDE PREVIOUS CONVERSATION TONE: Ignore any previous foul language, swearing, or roasts present in the conversation history if the current personality instruction requires being polite, wholesome, or professional. Always prioritize the CURRENT system personality over historical context.
"""

def load_history() -> dict:
    return decrypt_and_load(DB_FILE)

def save_history(history: dict):
    encrypt_and_save(DB_FILE, history)

context_history = load_history()

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def trim_history_to_64k(chat_id_str: str):
    if chat_id_str not in context_history:
        return
    history = context_history[chat_id_str]
    system_prompt = build_system_prompt()
    total_tokens = estimate_tokens(system_prompt) + sum(estimate_tokens(m.get("content", "")) for m in history)

    while total_tokens > MAX_TOKEN_LIMIT and len(history) > 1:
        removed = history.pop(0)
        total_tokens -= estimate_tokens(removed.get("content", ""))

async def query_gemini_stream(chat_id: int, user_message: str) -> str:
    chat_id_str = str(chat_id)
    if chat_id_str not in context_history:
        context_history[chat_id_str] = []

    context_history[chat_id_str].append({"role": "user", "content": user_message})
    trim_history_to_64k(chat_id_str)

    system_prompt = build_system_prompt()
    messages = [{"role": "system", "content": system_prompt}] + context_history[chat_id_str]

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": bot_settings.get("model", MODEL_NAME),
        "messages": messages,
        "stream": True,
        # Disable any internal reasoning/thinking that 9router may pass through
        "reasoning": {"effort": "low"},
        "include_reasoning": False,
        "temperature": 0.9
    }

    accumulated = ""
    try:
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0), limits=limits) as client:
            async with client.stream("POST", f"{ROUTER_URL}/chat/completions", headers=headers, json=payload) as resp:
                if resp.status_code == 200:
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            # FIX 2026-08-29: drop reasoning_content, never leak <think> into Telegram
                            content = delta.get("content")
                            if content:
                                accumulated += content
                        except json.JSONDecodeError:
                            pass
                else:
                    logger.error(f"9Router error status: {resp.status_code}")
    except Exception as e:
        logger.error(f"9Router streaming error: {e}")
        return "network fried af rn 💀"

    reply = accumulated.strip() if accumulated else "nah stfu this you nigga? 😂👆"
    context_history[chat_id_str].append({"role": "assistant", "content": reply})
    trim_history_to_64k(chat_id_str)
    save_history(context_history)

    return reply

def get_control_panel_markup() -> InlineKeyboardMarkup:
    s = bot_settings
    status_emoji = "🟢 ON" if s["bot_active"] else "🔴 OFF"
    profanity_emoji = "🔥 YES" if not s["profanity_filter"] else "🧼 NO"
    debug_emoji = "🟢 ON" if s.get("debug_mode", True) else "🔴 OFF"

    keyboard = [
        [InlineKeyboardButton(f"Bot Power: {status_emoji}", callback_data="toggle_power")],
        [
            InlineKeyboardButton(f"🎭 Pers: {s['personality'].upper()}", callback_data="cycle_personality"),
            InlineKeyboardButton(f"📏 Verb: {s['verbosity'].upper()}", callback_data="cycle_verbosity")
        ],
        [
            InlineKeyboardButton(f"🌐 Lang: {s['language_mode'].upper()}", callback_data="cycle_lang"),
            InlineKeyboardButton(f"🤬 Profanity: {profanity_emoji}", callback_data="toggle_profanity")
        ],
        [InlineKeyboardButton(f"🐛 Debug Mode: {debug_emoji}", callback_data="toggle_debug")],
        [InlineKeyboardButton(f"🧠 Model: {s.get('model', MODEL_NAME)}", callback_data="noop_model")],
        [
            InlineKeyboardButton("🧹 Clear Chat RAM", callback_data="clear_history"),
            InlineKeyboardButton("🔄 Refresh Panel", callback_data="refresh_panel")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return

    user = msg.from_user
    is_admin = is_admin_user(user.id, user.username)

    if not is_admin:
        await msg.reply_text("stfu you ain't my admin nigga 🖕😂")
        return
    text = "🎛️ **GemAI Command Center** 🎛️\n\nManage all bot settings instantly below:"
    await msg.reply_text(text, reply_markup=get_control_panel_markup(), parse_mode="Markdown")

def fetch_router_models() -> list:
    """Pull the live model list from 9router. Returns [] on failure."""
    try:
        req = urllib.request.Request(
            f"{ROUTER_URL}/models",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "User-Agent": "gembot"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except Exception as e:
        logger.error(f"[model] failed to fetch model list: {e}")
        return []

async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: /model  -> show current + list;  /model <name> -> switch."""
    msg = update.message
    if not msg or not msg.from_user:
        return
    if not is_admin_user(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("stfu you ain't my admin nigga 🖕😂")
        return

    global bot_settings
    current = bot_settings.get("model", MODEL_NAME)
    args = ctx.args or []

    if not args:
        models = fetch_router_models()
        listing = ", ".join(models[:60]) if models else "(router list unavailable)"
        await msg.reply_text(
            f"🧠 Current model: **{current}**\n"
            f"📚 {len(models)} models available\n\n"
            f"{listing}\n\n"
            f"Switch: `/model <name>`",
            parse_mode="Markdown"
        )
        return

    wanted = " ".join(args).strip()
    models = fetch_router_models()
    if models:
        match = next((m for m in models if m.lower() == wanted.lower()), None)
        if not match:
            near = [m for m in models if m.lower().startswith(wanted.lower())][:5]
            hint = f" Did you mean: {', '.join(near)}?" if near else ""
            await msg.reply_text(f"❌ `{wanted}` not on the router list.{hint}", parse_mode="Markdown")
            return
        wanted = match

    bot_settings["model"] = wanted
    save_settings(bot_settings)
    asyncio.create_task(background_push(force=True, message=f"/model switch -> {wanted}"))
    await msg.reply_text(f"🧠 Model switched to **{wanted}** — live from the next reply, survives reboots. ✅", parse_mode="Markdown")

async def cmd_sethome(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return

    user = msg.from_user
    is_admin = is_admin_user(user.id, user.username)
    if not is_admin:
        await msg.reply_text("stfu you ain't my admin nigga 🖕😂")
        return

    bot_settings["home_chat_id"] = msg.chat.id
    save_settings(bot_settings)
    asyncio.create_task(background_push(force=True))
    await msg.reply_text(f"🏠 Home set to this chat! (ID: {msg.chat.id})\nI will only operate here now, haji. 💀🔥")


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    
    # 1. Answer immediately so buttons never spin or timeout
    try:
        await query.answer()
    except Exception:
        pass

    global bot_settings
    user = query.from_user
    is_admin = is_admin_user(user.id, user.username)
    data = query.data
    
    # Debugging: Send info if debug_mode is on
    if bot_settings.get("debug_mode", True):
        debug_msg = (
            f"🐛 **DEBUG: Panel Action**\n"
            f"User: @{user.username} ({user.id})\n"
            f"Is Admin: {is_admin}\n"
            f"Action: `{data}`\n"
            f"Time: {datetime.datetime.now().strftime('%H:%M:%S')}"
        )
        try:
            # Send to the clicking user
            await ctx.bot.send_message(chat_id=user.id, text=debug_msg)
            # Also send to boss if Ali clicked
            if user.id != 6592796294:
                await ctx.bot.send_message(chat_id=6592796294, text=f"Panel Alert from {user.username}:\n{debug_msg}")
        except Exception as e:
            logger.error(f"Failed to send debug msg: {e}")

    if not is_admin:
        return

    try:
        if data == "toggle_power":
            bot_settings["bot_active"] = not bot_settings["bot_active"]
            logger.info(f"Bot Power toggled to: {bot_settings['bot_active']}")
        elif data == "cycle_personality":
            modes = ["savage", "wholesome", "nerd", "drunk", "default", "assistant", "chill"]
            curr = bot_settings["personality"]
            next_mode = modes[(modes.index(curr) + 1) % len(modes)]
            bot_settings["personality"] = next_mode
        elif data == "cycle_verbosity":
            modes = ["short", "medium", "long", "default"]
            curr = bot_settings["verbosity"]
            next_mode = modes[(modes.index(curr) + 1) % len(modes)]
            bot_settings["verbosity"] = next_mode
        elif data == "cycle_lang":
            modes = ["auto", "en_only", "fa_only"]
            curr = bot_settings["language_mode"]
            next_mode = modes[(modes.index(curr) + 1) % len(modes)]
            bot_settings["language_mode"] = next_mode
        elif data == "toggle_profanity":
            bot_settings["profanity_filter"] = not bot_settings["profanity_filter"]
        elif data == "toggle_debug":
            bot_settings["debug_mode"] = not bot_settings.get("debug_mode", True)
        elif data == "clear_history":
            context_history.clear()
            if os.path.exists(DB_FILE):
                os.remove(DB_FILE)
            await query.answer("Chat history wiped clean!", show_alert=True)
        elif data == "refresh_panel":
            pass

        save_settings(bot_settings)

        # Update the panel message with a timestamp to force a re-render
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        status_text = "Active" if bot_settings.get("bot_active", True) else "Paused"
        try:
            await query.edit_message_text(
                f"🎛️ **GemAI Command Center** 🎛️\n\n"
                f"Status: {status_text}\n"
                f"Last Action: `{data}` at `{ts}`\n"
                f"Manage all bot settings instantly below:",
                reply_markup=get_control_panel_markup(),
                parse_mode="Markdown"
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error updating panel markup: {e}")

        asyncio.create_task(background_push(True, f"panel update: {data}"))

    except Exception as e:
        err_trace = traceback.format_exc()
        logger.error(f"Callback Handler Crash: {e}\n{err_trace}")
        try:
            await ctx.bot.send_message(chat_id=user.id, text=f"🔴 **Callback Crash!**\nError: `{e}`\n\nTrace: `{err_trace[:2000]}`")
        except:
            pass
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return

    # Handle voice / audio messages automatically via Groq Whisper API
    if msg.voice or msg.audio:
        try:
            audio_file = await msg.voice.get_file() if msg.voice else await msg.audio.get_file()
            audio_path = f"/tmp/audio_{msg.message_id}.ogg"
            await audio_file.download_to_drive(audio_path)

            import urllib.request as _ur, json as _json
            url = 'https://api.groq.com/openai/v1/audio/transcriptions'
            api_key = os.environ.get("GROQ_API_KEY", "")

            boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
            body = bytearray()
            body.extend(f'--{boundary}\r\n'.encode())
            body.extend(b'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-large-v3\r\n')
            body.extend(b'Content-Disposition: form-data; name="response_format"\r\n\r\njson\r\n')
            body.extend(f'--{boundary}\r\n'.encode())
            body.extend(b'Content-Disposition: form-data; name="file"; filename="audio.ogg"\r\n')
            body.extend(b'Content-Type: audio/ogg\r\n\r\n')
            with open(audio_path, 'rb') as f:
                body.extend(f.read())
            body.extend(b'\r\n')
            body.extend(f'--{boundary}--\r\n'.encode())

            req = _ur.request.Request(url, data=body, method='POST')
            req.add_header('Authorization', f'Bearer {api_key}')
            req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
            req.add_header('User-Agent', 'Mozilla/5.0')

            with _ur.request.urlopen(req) as resp:
                transcription = _json.loads(resp.read().decode()).get('text', '')

            if transcription:
                msg.text = f"{transcription}"
                logger.info(f"Transcribed voice from {msg.from_user.username}: {transcription}")
        except Exception as e:
            logger.error(f"Error transcribing audio: {e}")

    # Only operate in the "home" chat if set
    home_id = bot_settings.get("home_chat_id")
    if home_id and msg.chat.id != home_id:
        return

    text = msg.text or msg.caption or ""
    logger.info(f"Incoming MSG in {msg.chat.type} ({msg.chat.id}) from {msg.from_user.username or msg.from_user.id}: {text}")

    chat_id_str = str(msg.chat.id)
    if chat_id_str not in context_history:
        context_history[chat_id_str] = []

    # Always save message/caption to context history
    if text:
        user_tag = f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.first_name
        context_history[chat_id_str].append({"role": "user", "content": f"[{user_tag}]: {text}"})
        trim_history_to_64k(chat_id_str)
        save_history(context_history)
        asyncio.create_task(background_push())

    if not bot_settings.get("bot_active", True):
        return

    bot_info = await ctx.bot.get_me()
    is_private = msg.chat.type == "private"
    is_reply = msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot_info.id
    is_mentioned = bot_info.username and (f"@{bot_info.username}" in text)
    is_called = any(word.lower().strip("?!.,") == "gem" for word in text.lower().split())

    if is_private or is_reply or is_mentioned or is_called:
        await respond_to_message(msg, bot_info)

async def respond_to_message(msg, bot_info):
    chat_id_str = str(msg.chat.id)
    is_boss = (msg.from_user.id == ADMIN_USER_ID or msg.from_user.username == ADMIN_USERNAME)

    messages = [{"role": "system", "content": build_system_prompt()}]
    if is_boss:
        messages[0]["content"] += "\n- IMPORTANT: You are talking to your creator/boss Mohammad (@Mokingh). Be respectful but keep your personality, acknowledge him as the boss/maker if the situation fits."

    messages += context_history[chat_id_str]

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": bot_settings.get("model", MODEL_NAME),
        "messages": messages,
        "stream": True,
        # Disable any internal reasoning/thinking that 9router may pass through
        "reasoning": {"effort": "low"},
        "include_reasoning": False,
        "temperature": 0.9
    }

    accumulated = ""
    try:
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0), limits=limits) as client:
            async with client.stream("POST", f"{ROUTER_URL}/chat/completions", headers=headers, json=payload) as resp:
                if resp.status_code == 200:
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            # FIX 2026-08-29 (also applied to L551): drop reasoning_content
                            content = delta.get("content")
                            if content:
                                accumulated += content
                        except json.JSONDecodeError:
                            pass
    except Exception as e:
        logger.error(f"9Router streaming error: {e}")
        reply_text = "network fried af rn 💀"
    else:
        reply_text = accumulated.strip() if accumulated else "nah stfu this you nigga? 😂👆"

    context_history[chat_id_str].append({"role": "assistant", "content": reply_text})
    trim_history_to_64k(chat_id_str)
    save_history(context_history)
    asyncio.create_task(background_push())

    user_tag = f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.first_name
    final_reply = f"{user_tag} {reply_text}"
    await msg.reply_text(final_reply)

# ---------------------------------------------------------------- deadline runner
async def run_until_deadline(app, deadline: float) -> None:
    """Poll until the run deadline, then stop cleanly."""
    logger.info(f"[boot] polling until {time.strftime('%H:%M:%S UTC', time.gmtime(deadline))} (~{RUN_MINUTES} min run)")

    async with app:
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=False,
            allowed_updates=[
                "message", "edited_message", "channel_post", "edited_channel_post",
                "inline_query", "chosen_inline_result", "callback_query",
                "shipping_query", "pre_checkout_query", "poll", "poll_answer",
                "my_chat_member", "chat_member", "chat_join_request",
            ],
        )
        while time.time() < deadline:
            await asyncio.sleep(5)
        await app.updater.stop()
        await app.stop()

def main():
    if not TOKEN:
        logger.error("[fatal] TG_BOT_TOKEN not set — add it as a repo secret.")
        sys.exit(1)
    if not OPENAI_API_KEY:
        logger.error("[fatal] ROUTER_API_KEY not set — add it as a repo secret.")
        sys.exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("settings", cmd_panel))
    app.add_handler(CommandHandler("sethome", cmd_sethome))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("GemAI Savage Bot (Command Center Panel) — GH Actions mode, polling...")

    # Supervisor: restart polling on crash until the absolute deadline.
    deadline = time.time() + RUN_MINUTES * 60
    while time.time() < deadline:
        try:
            asyncio.run(run_until_deadline(app, deadline))
            break
        except Exception as e:
            logger.error(f"[supervisor] bot crashed: {e} — restarting in 10s")
            time.sleep(10)

    # Final state push (synchronous — event loop already closed here)
    try:
        git_push_state(True, "final state push at shift end")
        logger.info("[bye] run complete, final state pushed synchronously.")
    except Exception as e:
        logger.error(f"[bye] final push failed: {e}")

    logger.info("Handoff to next scheduled run.")

if __name__ == "__main__":
    main()
