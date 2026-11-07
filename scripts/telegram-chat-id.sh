#!/bin/sh
# Prints the chats that recently wrote to the bot (id, type, title), so the
# group's id can go into TELEGRAM_CHAT_ID. Write /start@<botname> in the
# group first. Runs in the service image with the token from .env; the token
# is never printed.
set -eu

cd "$(dirname "$0")/.."

docker compose run --rm --no-deps -T parser python - <<'PY'
import json, os, urllib.request

token = os.environ.get("TELEGRAM_BOT_TOKEN")
if not token:
    raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10) as r:
    updates = json.load(r)["result"]
chats = {}
for update in updates:
    for key in ("message", "my_chat_member", "channel_post"):
        chat = update.get(key, {}).get("chat")
        if chat:
            chats[chat["id"]] = (chat["type"], chat.get("title") or chat.get("first_name", ""))
if not chats:
    print("No chats yet: write /start@<botname> in the group and run this again.")
for chat_id, (kind, title) in chats.items():
    print(chat_id, kind, title)
PY
