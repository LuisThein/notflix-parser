# Notflix Parser

> **Status:** in use. Runs unattended in Docker and confirms Netflix household mails within seconds.

Netflix asks members to confirm their household by mail ("Important: How to update your Netflix Household"). This service watches a dedicated mailbox over IMAP, opens the link in such a mail with headless Chrome and clicks "Yes, This Was Me".

## Features

- Polls the inbox every 3 seconds and reuses the IMAP connection (renewed every 15 minutes); the household mail is found among unread mails, because GMX's subject search lags 10–27 seconds behind
- Confirms each household mail in a fresh headless Chrome, retries up to 3 times
- Moves other mails from Netflix to the folder `Netflix-Andere` and permanently deletes all remaining mails in the inbox (including "A new device is using your account"), so use a mailbox dedicated to this
- For a temporary access code mail, opens the "Get Code" page in headless Chrome and sends the code itself to the chat
- Optional Telegram notifications: household mail received, confirmed or failed (with the link to click yourself), other Netflix mails, and service problems
- Backs off after errors and exits after 10 failures in a row, so Docker restarts it
- Docker healthcheck based on a heartbeat file

## Getting started

Requirements: Docker with Compose.

```bash
cp .env.example .env    # fill in the mailbox address and password
docker compose up -d --build
docker compose ps       # should show "healthy" after a few seconds
docker compose logs -f
```

Settings (environment): `NOTFLIX_EMAIL`, `NOTFLIX_PASSWORT`, `IMAP_SERVER`, optional `POLL_INTERVAL` (seconds, default 3), `RECONNECT_AFTER` (seconds, default 900), `MAX_CONSECUTIVE_FAILURES` (default 10), `OTHER_FOLDER` (default `Netflix-Andere`).

### Telegram notifications

Create a bot with @BotFather and put its token into `TELEGRAM_BOT_TOKEN`. Add the bot to the chat that should get the messages, write `/start@<botname>` there and run `scripts/telegram-chat-id.sh` to get the chat id for `TELEGRAM_CHAT_ID`. Without both values the service runs silently. Link previews are turned off, so Telegram never opens a confirmation link itself. Messages are in German. After a deploy the chat gets an update note once instead of the start message; bump `UPDATE_NOTE_ID` and edit `UPDATE_NOTE` for that. The last sent id is kept in the Docker volume `state`.

## Development

Run the unit tests with `scripts/test.sh`. It builds the image and runs `unittest` inside it without network access, so no `.env` is needed.

Maintainers: `scripts/setup.sh` activates the Git hooks (Conventional Commits check, changelog in the project notes). Contributors do not need it.
