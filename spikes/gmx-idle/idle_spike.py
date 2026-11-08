"""
Spike: watch a GMX inbox with IMAP IDLE (or NOOP polling as a fallback) and
print sender and subject of every new mail. Read-only: the inbox is selected
with readonly=True and headers are fetched with BODY.PEEK.

Environment:
  GMX_USER, GMX_PASSWORD   credentials (never in code or repo)
  GMX_HOST                 default imap.gmx.net
  MODE                     "idle" (default) or "poll"
  IDLE_RENEW               seconds until IDLE is renewed, default 600
  POLL_INTERVAL            seconds between NOOPs in poll mode, default 60

Every log line starts with an ISO timestamp, so the log can be evaluated
later (latency, connection lifetime, disconnect reasons).
"""

import email
import logging
import os
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from imapclient import IMAPClient

HOST = os.environ.get("GMX_HOST", "imap.gmx.net")
USER = os.environ["GMX_USER"]
PASSWORD = os.environ["GMX_PASSWORD"]
MODE = os.environ.get("MODE", "idle")
IDLE_RENEW = int(os.environ.get("IDLE_RENEW", "600"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
# idle_check() returns at least this often, so renewals happen on time
CHECK_TIMEOUT = 30
BACKOFF = [30, 60, 120, 240, 480, 600]
# A connection that lived this long counts as healthy and resets the backoff
HEALTHY_AFTER = 300
HEADERS = "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE RECEIVED)]"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("idle-spike")


def on_new_mail(sender, subject):
    """Placeholder hook for the later action."""
    print(f"NEW MAIL from {sender}: {subject}", flush=True)


def decode(raw):
    return str(make_header(decode_header(raw or "")))


def received_at(msg):
    """Time the receiving GMX server stamped into the topmost Received header."""
    received = msg.get_all("Received") or []
    if not received:
        return None
    try:
        return parsedate_to_datetime(received[0].rpartition(";")[2].strip())
    except (TypeError, ValueError):
        return None


def connect():
    client = IMAPClient(HOST, ssl=True, port=993, timeout=60)
    client.login(USER, PASSWORD)
    capabilities = client.capabilities()
    log.info("Logged in, IDLE supported: %s", b"IDLE" in capabilities)
    client.select_folder("INBOX", readonly=True)
    return client


def highest_uid(client):
    uids = client.search(["ALL"])
    return max(uids) if uids else 0


def report_new(client, last_uid, notified_at):
    """Prints all mails above last_uid and returns the new highest UID."""
    uids = [u for u in client.search(["UID", f"{last_uid + 1}:*"]) if u > last_uid]
    if not uids:
        log.info("Notification without new UIDs (mail already gone?)")
        return last_uid
    for uid, data in client.fetch(uids, [HEADERS]).items():
        raw = next((v for k, v in data.items() if k.startswith(b"BODY")), b"")
        msg = email.message_from_bytes(raw)
        arrived = received_at(msg)
        latency = (notified_at - arrived).total_seconds() if arrived else None
        log.info(
            "UID %s received by GMX %s, noticed %s, latency %s s",
            uid,
            arrived.isoformat() if arrived else "?",
            notified_at.isoformat(timespec="seconds"),
            f"{latency:.0f}" if latency is not None else "?",
        )
        on_new_mail(decode(msg["From"]), decode(msg["Subject"]))
    return max(uids)


def watch_idle(client):
    last_uid = highest_uid(client)
    while True:
        client.idle()
        started = time.monotonic()
        notified_at = None
        while time.monotonic() - started < IDLE_RENEW:
            responses = client.idle_check(timeout=CHECK_TIMEOUT)
            if responses:
                log.info("IDLE responses: %s", responses)
            if any(len(r) > 1 and r[1] == b"EXISTS" for r in responses):
                notified_at = datetime.now(timezone.utc)
                break
        client.idle_done()
        if notified_at:
            last_uid = report_new(client, last_uid, notified_at)
        else:
            log.info("IDLE renewed after %.0f s", time.monotonic() - started)


def watch_poll(client):
    last_uid = highest_uid(client)
    while True:
        time.sleep(POLL_INTERVAL)
        responses = client.noop()
        if any(len(r) > 1 and r[1] == b"EXISTS" for r in responses[1]):
            last_uid = report_new(client, last_uid, datetime.now(timezone.utc))


def main():
    log.info("Mode %s, host %s, IDLE renewed every %d s", MODE, HOST, IDLE_RENEW)
    watch = watch_idle if MODE == "idle" else watch_poll
    failures = 0
    while True:
        connected = time.monotonic()
        client = None
        try:
            client = connect()
            watch(client)
        except Exception as e:
            lived = time.monotonic() - connected
            if lived >= HEALTHY_AFTER:
                failures = 0
            delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
            failures += 1
            log.warning(
                "Connection ended after %.0f s: %s: %s; reconnect in %d s",
                lived, type(e).__name__, e, delay,
            )
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            time.sleep(delay)


if __name__ == "__main__":
    main()
