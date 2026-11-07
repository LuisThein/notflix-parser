# CLAUDE.md

## Project notes (source of truth)

The maintainer's project documentation lives outside this repo, in the Obsidian vault folder `Projekte/Notflix Parser`. The vault location is in the maintainer's global `~/.claude/CLAUDE.md`.

- Start with the current handover (linked under "Wo steht was" in `00 Übersicht.md`), then `04 Entscheidungen.md` and `07 Offene Fragen.md`.
- Notes are written in German. The folder is made available via `permissions.additionalDirectories` in `.claude/settings.local.json` (not committed).
- General rules: `~/.claude/CLAUDE.md` (repo `claude-standards`). This file only adds what is specific to this project.

## Project rules

- Changelog prefix: `notflix` (marker `%% notflix:commits %%`). After cloning: `scripts/setup.sh "<vault>" "Notflix Parser" notflix`.
- This repo is public: no absolute paths, host names or mail addresses in committed files (E-001).
- Never read or print `.env`; it holds the mail account credentials.
- The service moves other Netflix mails to the folder `Netflix-Andere` (E-008) and permanently deletes every other inbox mail that is not the household mail. The account exists only for this; never point it at another mailbox.
- `.env` also holds the Telegram bot token; `scripts/telegram-chat-id.sh` reads it inside the container, never print it.

## Layout

- `notflix-parser.py` – the whole service: IMAP polling loop, mail sorting, Selenium confirmation, Telegram notifications
- `Dockerfile`, `compose.yaml` – image with Debian Chromium, service `parser` in compose project `notflix`
- `.env.example` – template for `.env`
- `tests/` – unit tests for the parsing functions and, with a fake IMAP mailbox and mocked Telegram, for mail sorting and notifications; `scripts/test.sh` runs them
- `.githooks/`, `scripts/` – Conventional Commits check and changelog hooks; `scripts/telegram-chat-id.sh` lists the chats that wrote to the bot

## Commands

- Test: `scripts/test.sh` (unit tests with `unittest` inside the service image, offline). Plus the manual check before every commit: `docker compose up -d --build`, then `docker compose ps` shows `healthy` and `docker compose logs` shows a successful login without errors.
- Build and run: `docker compose up -d --build`
- Logs: `docker compose logs -f`

## Domain rules

- Use IMAP UIDs, not sequence numbers: the cleanup expunges mails and shifts sequence numbers.
- Read mails with `BODY.PEEK[]` so they stay unread until handled; mark them `\Seen` only after success or after `MAX_ATTEMPTS_PER_MAIL`.
- Each confirmation uses a fresh headless Chrome, so a crashed browser can't break later runs. Chrome loads with `page_load_strategy = "none"`; always wait for the needed element, and after the click check that the page reacted (E-012).
- Telegram messages go through a queue and a background thread (E-014); never send them synchronously in the mail path.
- The temporary access code is not in the mail: the service opens the "Get Code" link (valid 15 minutes) and reads `data-uia="travel-verification-otp"` (E-010). Mails "A new device is using your account" are deleted without a message (E-011).
