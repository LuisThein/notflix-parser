# Notflix Parser

> **Status:** in use. Runs unattended in Docker and confirms Netflix household mails within seconds.

Netflix asks members to confirm their household by mail ("Important: How to update your Netflix Household"). This service watches a dedicated mailbox over IMAP, opens the link in such a mail with headless Chrome and clicks "Yes, This Was Me".

## Features

- Polls the inbox every 10 seconds and reuses the IMAP connection (renewed every 15 minutes)
- Confirms each household mail in a fresh headless Chrome, retries up to 3 times
- Permanently deletes all other mails in the inbox, so use a mailbox dedicated to this
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

Settings (environment): `NOTFLIX_EMAIL`, `NOTFLIX_PASSWORT`, `IMAP_SERVER`, optional `POLL_INTERVAL` (seconds, default 10), `RECONNECT_AFTER` (seconds, default 900), `MAX_CONSECUTIVE_FAILURES` (default 10).

## Development

Maintainers: `scripts/setup.sh` activates the Git hooks (Conventional Commits check, changelog in the project notes). Contributors do not need it.
