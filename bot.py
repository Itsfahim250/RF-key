"""
Telegram Bot - License Generator (DPMods_Security schema)
------------------------------------------------------------
Deploy target: Render.com (Web Service, webhook mode).

This matches the NEW admin panel's Firebase schema. Everything lives under
one root node:

    DPMods_Security/
        App_Status/              (managed only by index.html - untouched here)
        Keys/{keyId}              -> {Username, DeviceLimit, ExpiryDate,
                                       ExpiresAt, Devices, Banned}
        Bot_Access/{key}          -> {ExpiryDate, UsedBy}
                                      (generated from index.html's "Bot Access" tab)
        Bot_Sessions/{chat_id}    -> {Key, ExpiryDate, Pending?}
                                      (bot-internal only, not shown in the panel)

How it works
============
1. Admin generates a "Bot Access Key" from index.html's Bot Access tab
   (format: RF_XXXX_XXXX_XXXX, unchanged from before).
2. In Telegram, send /start to the bot, then paste that key to activate.
3. Once activated, tap "🔑 Generate License Key":
      a) Pick a duration: 1/2/3 Hours, 1/3/7/10/15 Days, or 1 Month.
      b) Bot asks how many devices the license should support - reply
         with a number (e.g. 1, 2, 5).
      c) Bot creates a new key in DPMods_Security/Keys using the SAME
         template the admin panel itself uses (DP-VIP-XXXXXX) and sends
         it back in a copyable format.
4. "🚪 Logout" ends the session; a valid access key is required again.

Access keys are single-use (locked to the first chat that claims them) and
are live-checked on every action, so deleting/releasing a key from the
admin panel immediately revokes bot access.

A NOTE ON HOUR-LEVEL DURATIONS: DPMods_Security/Keys stores ExpiryDate as
a plain YYYY-MM-DD date - that's the existing admin-panel template and it
was left unchanged, as requested. That means 1/2/3-hour keys are also
stored with today's date, so the admin panel itself (and anything that
only checks ExpiryDate) will treat them as valid until end of day. This
bot additionally writes an `ExpiresAt` field (full UTC timestamp) on every
key it creates, so anything that wants true hour-level precision (your
app's own validation logic) can check that field instead.

Environment variables (set these in Render's dashboard):
    BOT_TOKEN        - Telegram bot token from @BotFather (required)
    WEBHOOK_SECRET   - random string of your choosing, used in the webhook
                        URL path (required)
    FIREBASE_DB_URL  - the SAME Firebase Realtime DB URL you type into the
                        admin panel's login screen (required - the panel
                        no longer hardcodes one, so this must be set)
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
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ROOT = "DPMods_Security"

# code -> (button label, timedelta)
DURATIONS = {
    "1h": ("1 Hour", timedelta(hours=1)),
    "2h": ("2 Hours", timedelta(hours=2)),
    "3h": ("3 Hours", timedelta(hours=3)),
    "1d": ("1 Day", timedelta(days=1)),
    "3d": ("3 Days", timedelta(days=3)),
    "7d": ("7 Days", timedelta(days=7)),
    "10d": ("10 Days", timedelta(days=10)),
    "15d": ("15 Days", timedelta(days=15)),
    "1m": ("1 Month", timedelta(days=30)),
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
        {"text": label, "callback_data": f"dur:{code}"}
        for code, (label, _delta) in DURATIONS.items()
    ]
    # 3 buttons per row
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return {"inline_keyboard": rows}


# --------------------------------------------------------------------------
# Session / access-key logic  (Bot_Access + Bot_Sessions)
# --------------------------------------------------------------------------

def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def get_session(chat_id):
    """
    Return the session dict if this chat is validly activated AND the
    underlying access key it used still exists, is un-expired, and is
    still locked to this chat.

    This live-checks Firebase every time (not just the session's own
    ExpiryDate) so that if the admin deletes or releases the key from
    index.html, the bot immediately stops honouring the old session.
    """
    data = fb_get(f"{ROOT}/Bot_Sessions/{chat_id}")
    if not data:
        return None

    exp_date = _parse_date(data.get("ExpiryDate"))
    if not exp_date or exp_date < datetime.utcnow().date():
        delete_session(chat_id)
        return None

    key = data.get("Key")
    key_data = fb_get(f"{ROOT}/Bot_Access/{key}") if key else None
    if not key_data:
        # Key was deleted by the admin -> session is dead
        delete_session(chat_id)
        return None

    key_exp = _parse_date(key_data.get("ExpiryDate"))
    if not key_exp or key_exp < datetime.utcnow().date():
        delete_session(chat_id)
        return None

    if str(key_data.get("UsedBy", "")) != str(chat_id):
        # Key was released/reassigned to someone else by the admin
        delete_session(chat_id)
        return None

    return data


def create_session(chat_id, key, expire_date):
    fb_set(f"{ROOT}/Bot_Sessions/{chat_id}", {"Key": key, "ExpiryDate": expire_date})


def delete_session(chat_id):
    fb_delete(f"{ROOT}/Bot_Sessions/{chat_id}")


def set_pending(chat_id, pending):
    fb_patch(f"{ROOT}/Bot_Sessions/{chat_id}", {"Pending": pending})


def clear_pending(chat_id):
    fb_delete(f"{ROOT}/Bot_Sessions/{chat_id}/Pending")


def validate_access_key(key, chat_id):
    """
    Single-use keys: the first chat that successfully claims a key gets
    locked to it (UsedBy = chat_id). Any other chat trying the same key
    is rejected, even if the key hasn't expired yet.

    Returns (expire_date, status) where status is one of:
        "ok"            - key is valid and now belongs to this chat
        "not_found"     - key doesn't exist
        "expired"       - key exists but ExpiryDate has passed
        "already_used"  - key is locked to a different chat
    """
    key = (key or "").strip()
    if not key:
        return None, "not_found"

    data = fb_get(f"{ROOT}/Bot_Access/{key}")
    if not data:
        return None, "not_found"

    exp_date = _parse_date(data.get("ExpiryDate"))
    if not exp_date or exp_date < datetime.utcnow().date():
        return None, "expired"

    used_by = data.get("UsedBy")
    if used_by and str(used_by) != str(chat_id):
        return None, "already_used"

    if not used_by:
        fb_patch(f"{ROOT}/Bot_Access/{key}", {"UsedBy": chat_id})

    return data.get("ExpiryDate"), "ok"


# --------------------------------------------------------------------------
# License key generation (DPMods_Security/Keys)
# Template is UNCHANGED from the admin panel: DP-VIP-XXXXXX
# --------------------------------------------------------------------------

_BASE36 = string.digits + string.ascii_lowercase


def gen_license_key():
    rand = "".join(random.choice(_BASE36) for _ in range(6)).upper()
    return f"DP-VIP-{rand}"


def create_license(chat_id, from_user, duration_code, device_limit):
    label, delta = DURATIONS[duration_code]
    expires_at = datetime.utcnow() + delta

    key_id = gen_license_key()
    for _ in range(5):  # avoid extremely unlikely collisions
        if not fb_get(f"{ROOT}/Keys/{key_id}"):
            break
        key_id = gen_license_key()

    username = None
    if from_user:
        tg_username = from_user.get("username")
        username = f"@{tg_username}" if tg_username else (from_user.get("first_name") or f"tg_{chat_id}")

    payload = {
        "Username": username,
        "DeviceLimit": device_limit,
        "ExpiryDate": expires_at.strftime("%Y-%m-%d"),
        "ExpiresAt": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Devices": {"dummy": 0},
        "Banned": False,
    }
    fb_set(f"{ROOT}/Keys/{key_id}", payload)
    return key_id, label, expires_at


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
                "✅ *Welcome back!* You're already activated.\nUse the menu below.",
                MAIN_MENU_KB,
            )
        else:
            send_message(
                chat_id,
                "🔐 Please enter your *Access Key* to activate this bot:",
                REMOVE_KB,
            )
        return

    session = get_session(chat_id)

    if not session:
        # Not activated -> treat any text as an attempted access key
        expire, status = validate_access_key(text, chat_id)
        if status == "ok":
            create_session(chat_id, text, expire)
            send_message(
                chat_id,
                f"✅ *Activated!*\nThis session is valid until: `{expire}`",
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

    # Activated -> are we waiting for a device-count reply?
    pending = session.get("Pending") or {}
    if pending.get("Type") == "awaiting_devices":
        if not text.isdigit() or int(text) < 1:
            send_message(chat_id, "⚠️ Please send a valid number of devices (e.g. 1, 2, 5).")
            return

        device_limit = min(int(text), 100)
        duration_code = pending.get("DurationCode")
        if duration_code not in DURATIONS:
            clear_pending(chat_id)
            send_message(chat_id, "⚠️ Something went wrong - please tap 'Generate License Key' again.", MAIN_MENU_KB)
            return

        key_id, label, expires_at = create_license(chat_id, message.get("from"), duration_code, device_limit)
        clear_pending(chat_id)

        reply = (
            "✅ *License Created!*\n\n"
            f"🔑 Key: `{key_id}`\n"
            f"⏳ Duration: {label}\n"
            f"📅 Expires: `{expires_at.strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"📱 Devices: `{device_limit}`\n\n"
            "_Tap the key above to copy it._"
        )
        send_message(chat_id, reply, MAIN_MENU_KB)
        return

    # Activated, no pending action -> handle menu buttons
    if text == "🔑 Generate License Key":
        send_message(chat_id, "📅 *Select duration for the new license:*", duration_inline_kb())
    elif text == "🚪 Logout":
        delete_session(chat_id)
        send_message(
            chat_id,
            "👋 You've been logged out.\nSend /start and enter an access key to activate again.",
            REMOVE_KB,
        )
    else:
        send_message(chat_id, "Please use the menu buttons below.", MAIN_MENU_KB)


def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    data = callback.get("data", "")
    callback_id = callback["id"]

    if not data.startswith("dur:"):
        answer_callback(callback_id)
        return

    session = get_session(chat_id)
    if not session:
        edit_message(chat_id, message_id, "⚠️ Your access has expired or was revoked. Send /start to activate again.")
        answer_callback(callback_id)
        return

    code = data.split(":", 1)[1]
    if code not in DURATIONS:
        answer_callback(callback_id, "Unknown option.")
        return

    set_pending(chat_id, {"Type": "awaiting_devices", "DurationCode": code})

    label = DURATIONS[code][0]
    edit_message(
        chat_id,
        message_id,
        f"⏳ Duration set to *{label}*.\n\n📱 How many devices should this license support?\nReply with a number (e.g. 1, 2, 5):",
    )
    answer_callback(callback_id, f"{label} selected")


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
