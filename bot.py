"""
Telegram License-Generator Bot
--------------------------------
Deploy target: Render.com (Web Service, webhook mode).

How it works
============
1. An admin generates a "Bot Access Key" from index.html (Bot Keys tab).
2. The admin sends /start to the Telegram bot, then pastes that access key.
3. If the key is valid and not expired, the bot creates a login session for
   that chat and shows a menu: "Generate License Key" and "Logout".
4. Tapping "Generate License Key" shows duration buttons (1 Day / 3 Days /
   7 Days / 1 Month). Whichever is tapped, the bot creates a new
   username + password + expire_date entry in the SAME Firebase node that
   index.html's "Users" tab reads from (panel_auth/users), and shows the
   result in a copyable, monospace format.
5. "Logout" clears the chat's session; a new access key is required to use
   the bot again.

Everything is stateless / stored in Firebase, so restarts on Render are
safe and don't lose login sessions or issued keys.

Access keys are single-use: the first Telegram chat that successfully
enters a key gets locked to it (used_by = chat_id in Firebase). No other
chat can use that same key afterwards, even before it expires. An admin
can "release" a key from index.html's Bot Keys tab to unlock it again
(e.g. if the original user switched devices/accounts).

Firebase layout used (Realtime Database, REST API, no auth required just
like the existing panel):
    panel_auth/users/{username}      -> {password, expire_date}          (existing)
    panel_auth/bot_access/{key}      -> {expire_date, used_by?}           (new)
    panel_auth/bot_sessions/{chat_id}-> {key, expire_date}                (new)

Environment variables (set these in Render's dashboard):
    BOT_TOKEN        - Telegram bot token from @BotFather (required)
    WEBHOOK_SECRET   - any random string you choose, used as part of the
                        webhook URL path so randoms can't hit your webhook
                        (required)
    FIREBASE_DB_URL  - optional, defaults to the same DB index.html uses
"""

import os
import random
import string
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
FIREBASE_DB_URL = os.environ.get(
    "FIREBASE_DB_URL",
    "https://device-fccef-default-rtdb.firebaseio.com",
).rstrip("/")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# code -> (button label, number of days)
DURATIONS = {
    "1d": ("1 Day", 1),
    "3d": ("3 Days", 3),
    "7d": ("7 Days", 7),
    "1m": ("1 Month", 30),
}

app = Flask(__name__)


# --------------------------------------------------------------------------
# Firebase REST helpers
# --------------------------------------------------------------------------

def fb_get(path):
    r = requests.get(f"{FIREBASE_DB_URL}/{path}.json", timeout=10)
    r.raise_for_status()
    return r.json()


def fb_set(path, data):
    r = requests.put(f"{FIREBASE_DB_URL}/{path}.json", json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def fb_delete(path):
    r = requests.delete(f"{FIREBASE_DB_URL}/{path}.json", timeout=10)
    r.raise_for_status()
    return r.json()


def fb_patch(path, data):
    """Partial update - only touches the given fields, leaves others as-is."""
    r = requests.patch(f"{FIREBASE_DB_URL}/{path}.json", json=data, timeout=10)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------

def tg_call(method, payload):
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=10)
        return r.json()
    except requests.RequestException as e:
        print(f"[telegram] {method} failed: {e}")
        return {}


def send_message(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", payload)


def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="Markdown"):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_call("editMessageText", payload)


def answer_callback(callback_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    return tg_call("answerCallbackQuery", payload)


MAIN_MENU_KB = {
    "keyboard": [
        [{"text": "🔑 Generate License Key"}],
        [{"text": "🚪 Logout"}],
    ],
    "resize_keyboard": True,
}

REMOVE_KB = {"remove_keyboard": True}


def duration_inline_kb():
    buttons = [
        {"text": label, "callback_data": f"gen:{code}"}
        for code, (label, _days) in DURATIONS.items()
    ]
    # 2 buttons per row
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


# --------------------------------------------------------------------------
# Business logic
# --------------------------------------------------------------------------

def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def get_session(chat_id):
    """
    Return the session dict if this chat has a valid, un-expired login
    session AND the underlying access key STILL exists, is un-expired,
    and is still locked to this chat.

    This live-checks Firebase every time (not just the session's own
    expire_date) so that if the admin deletes or releases the key from
    index.html, the bot immediately stops honouring the old session
    instead of trusting a stale local expiry date.
    """
    data = fb_get(f"panel_auth/bot_sessions/{chat_id}")
    if not data:
        return None

    exp_date = _parse_date(data.get("expire_date"))
    if not exp_date or exp_date < datetime.utcnow().date():
        delete_session(chat_id)
        return None

    key = data.get("key")
    key_data = fb_get(f"panel_auth/bot_access/{key}") if key else None
    if not key_data:
        # Key was deleted by the admin -> session is dead
        delete_session(chat_id)
        return None

    key_exp = _parse_date(key_data.get("expire_date"))
    if not key_exp or key_exp < datetime.utcnow().date():
        delete_session(chat_id)
        return None

    if str(key_data.get("used_by", "")) != str(chat_id):
        # Key was released/reassigned to someone else by the admin
        delete_session(chat_id)
        return None

    return data


def create_session(chat_id, key, expire_date):
    fb_set(f"panel_auth/bot_sessions/{chat_id}", {"key": key, "expire_date": expire_date})


def delete_session(chat_id):
    fb_delete(f"panel_auth/bot_sessions/{chat_id}")


def validate_access_key(key, chat_id):
    """
    Single-use keys: the first chat that successfully claims a key gets
    locked to it (used_by = chat_id). Any other chat trying the same key
    is rejected, even if the key hasn't expired yet. The same chat can
    re-enter its own key again later (e.g. after Logout) without issue.

    Returns (expire_date, status) where status is one of:
        "ok"            - key is valid and now belongs to this chat
        "not_found"     - key doesn't exist
        "expired"       - key exists but expire_date has passed
        "already_used"  - key is locked to a different chat
    """
    key = (key or "").strip()
    if not key:
        return None, "not_found"

    data = fb_get(f"panel_auth/bot_access/{key}")
    if not data:
        return None, "not_found"

    exp_date = _parse_date(data.get("expire_date"))
    if not exp_date or exp_date < datetime.utcnow().date():
        return None, "expired"

    used_by = data.get("used_by")
    if used_by and str(used_by) != str(chat_id):
        return None, "already_used"

    if not used_by:
        fb_patch(f"panel_auth/bot_access/{key}", {"used_by": chat_id})

    return data.get("expire_date"), "ok"


# ---- License (username/password) generation -------------------------------
# Edit these if you want a different username/password format.
# Username template: RF_USER_XXXX  (XXXX = 4 random unambiguous chars)

_KEY_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I - avoids confusion


def _rand_segment(length=4):
    return "".join(random.choice(_KEY_CHARS) for _ in range(length))


def gen_username():
    return f"RF_USER_{_rand_segment(4)}"


def gen_password():
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(8))


def create_license(days):
    username = gen_username()
    # avoid extremely unlikely collisions
    for _ in range(5):
        if not fb_get(f"panel_auth/users/{username}"):
            break
        username = gen_username()

    password = gen_password()
    expire_date = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
    fb_set(f"panel_auth/users/{username}", {"password": password, "expire_date": expire_date})
    return username, password, expire_date


# --------------------------------------------------------------------------
# Update handlers
# --------------------------------------------------------------------------

def handle_message(message):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if text == "/start":
        session = get_session(chat_id)
        if session:
            send_message(
                chat_id,
                "✅ *Welcome back!* You're already logged in.\nUse the menu below.",
                MAIN_MENU_KB,
            )
        else:
            send_message(
                chat_id,
                "🔐 Please enter your *Access Key* to unlock this bot:",
                REMOVE_KB,
            )
        return

    session = get_session(chat_id)

    if not session:
        # Not logged in -> treat any text as an attempted access key
        expire, status = validate_access_key(text, chat_id)
        if status == "ok":
            create_session(chat_id, text, expire)
            send_message(
                chat_id,
                f"✅ *Access granted!*\nThis session is valid until: `{expire}`",
                MAIN_MENU_KB,
            )
        elif status == "already_used":
            send_message(
                chat_id,
                "🚫 This key is already in use on another account.\nAsk the admin to release it or issue you a new one.",
            )
        elif status == "expired":
            send_message(chat_id, "⌛ This key has expired. Please request a new one.")
        else:
            send_message(chat_id, "❌ Invalid key. Please check and try again.")
        return

    # Logged in -> handle menu actions
    if text == "🔑 Generate License Key":
        send_message(chat_id, "📅 *Select duration for the new license:*", duration_inline_kb())
    elif text == "🚪 Logout":
        delete_session(chat_id)
        send_message(
            chat_id,
            "👋 You've been logged out.\nSend /start and enter an access key to log in again.",
            REMOVE_KB,
        )
    else:
        send_message(chat_id, "Please use the menu buttons below.", MAIN_MENU_KB)


def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    data = callback.get("data", "")
    callback_id = callback["id"]

    if not data.startswith("gen:"):
        answer_callback(callback_id)
        return

    session = get_session(chat_id)
    if not session:
        edit_message(chat_id, message_id, "⚠️ Your access has expired or was revoked. Send /start to log in again.")
        answer_callback(callback_id)
        return

    code = data.split(":", 1)[1]
    label, days = DURATIONS.get(code, (None, None))
    if days is None:
        answer_callback(callback_id, "Unknown option.")
        return

    username, password, expire_date = create_license(days)

    text = (
        "✅ *License Created!*\n\n"
        f"👤 Username: `{username}`\n"
        f"🔑 Password: `{password}`\n"
        f"📅 Expires: `{expire_date}` ({label})\n\n"
        "_Tap any value above to copy it._"
    )
    edit_message(chat_id, message_id, text)
    answer_callback(callback_id, "License created ✅")


# --------------------------------------------------------------------------
# Flask routes
# --------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return "Bot is running.", 200


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as e:  # keep the webhook alive even if something errors
        print(f"[webhook] error handling update: {e}")

    return jsonify(ok=True)


# Convenience route: visit this once (in a browser or with curl) after
# deploying to register the webhook with Telegram automatically. Protected
# by WEBHOOK_SECRET so randoms can't repoint your webhook.
@app.route(f"/register-webhook/{WEBHOOK_SECRET}", methods=["GET"])
def register_webhook():
    base_url = request.url_root.rstrip("/")
    target = f"{base_url}/webhook/{WEBHOOK_SECRET}"
    result = tg_call("setWebhook", {"url": target})
    return jsonify({"webhook_url": target, "telegram_response": result})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
