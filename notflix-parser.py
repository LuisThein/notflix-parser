import email
import imaplib
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.request
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Account credentials
USERNAME = os.environ["NOTFLIX_EMAIL"]
PASSWORD = os.environ["NOTFLIX_PASSWORT"]
IMAP_SERVER = os.environ["IMAP_SERVER"]

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
# The IMAP connection is reused between polls and only renewed this often,
# so frequent polling doesn't mean frequent logins
RECONNECT_AFTER = int(os.environ.get("RECONNECT_AFTER", "900"))
# After this many failed polls in a row the process exits, so Docker restarts it
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "10"))
# A mail whose button could not be clicked stays unread and is retried this often
MAX_ATTEMPTS_PER_MAIL = 3
# Touched after every successful poll; the Docker healthcheck watches its age
HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/notflix-heartbeat"))
# Set in the Docker image; locally Selenium finds a driver on its own
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH")
# Telegram notifications (E-004); without these the service runs silently
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Netflix mails that are not the household mail are kept here (E-008)
OTHER_FOLDER = os.environ.get("OTHER_FOLDER", "Netflix-Andere")
# Failed polls in a row before the chat hears about it; single hiccups stay quiet
NOTIFY_AFTER_FAILURES = 3

TARGET_SUBJECT = "Important: How to update your Netflix Household"
BUTTON_TEXT = "Yes, This Was Me"
# Path of the button's link target on netflix.com
CONFIRM_PATH = "/account/update-primary-location"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("notflix")

attempts = {}

# Chat messages, in German for the household chat
MESSAGES = {
    "started": "Notflix Parser gestartet.",
    "household_arrived": "Household-Mail angekommen, ich bestätige den Haushalt …",
    "household_confirmed": "✅ Haushalt bestätigt.",
    "household_failed": "⚠️ Haushalt nach {attempts} Versuchen nicht bestätigt. Bitte selbst klicken:\n{link}",
    "household_failed_no_link": "⚠️ Haushalt nicht bestätigt: In der Mail war kein Bestätigungslink.",
    "other_netflix": "📩 Andere Netflix-Mail: {subject}\n{link}\n(abgelegt in {folder})",
    "other_netflix_no_link": "📩 Andere Netflix-Mail: {subject}\n(kein Link gefunden, abgelegt in {folder})",
    "check_failing": "⚠️ Mail-Abruf schlägt fehl ({failures}x in Folge): {error}",
    "check_recovered": "Mail-Abruf läuft wieder.",
    "exiting": "❌ Zu viele Fehler in Folge, der Dienst startet neu.",
}

# Link texts and hosts that are never the main link of a Netflix mail
FOOTER_TEXTS = ("help", "unsubscribe", "privacy", "terms", "notification", "preferences")
FOOTER_HOSTS = ("help.netflix.com",)


def message(key, **values):
    return MESSAGES[key].format(**values)


def notify(text):
    """
    Sends a message to the Telegram chat. Never raises: a failed notification
    must not stop the mail handling.
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        # A link preview would make Telegram open the confirmation link
        "link_preview_options": {"is_disabled": True},
    }
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception as e:
        # Only the error type and text: the URL contains the bot token
        log.warning("Telegram notification failed: %s: %s", type(e).__name__, e)


def is_netflix_sender(from_header):
    domain = parseaddr(from_header or "")[1].rpartition("@")[2].lower()
    return domain == "netflix.com" or domain.endswith(".netflix.com")


def classify(from_header, subject):
    """
    Returns "household" for the household mail, "netflix" for any other mail
    from Netflix and "other" for everything else.
    """
    if subject == TARGET_SUBJECT:
        return "household"
    if is_netflix_sender(from_header):
        return "netflix"
    return "other"


def find_main_link(body):
    """
    Guesses the main link of a Netflix mail: the first link to netflix.com
    with a text that is not a footer link. Kept simple on purpose until there
    are enough real examples (E-008).
    """
    soup = BeautifulSoup(body, "html.parser")
    for a in soup.find_all("a", href=True):
        host = (urlsplit(a["href"]).hostname or "").lower()
        if not (host == "netflix.com" or host.endswith(".netflix.com")):
            continue
        if host in FOOTER_HOSTS:
            continue
        text = a.get_text(" ", strip=True).lower()
        if not text or any(word in text for word in FOOTER_TEXTS):
            continue
        return a["href"]
    return None


def create_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    service = Service(executable_path=CHROMEDRIVER_PATH) if CHROMEDRIVER_PATH else Service()
    return webdriver.Chrome(options=options, service=service)


def find_confirmation_link(body):
    """
    Returns the URL of the "Yes, This Was Me" button in the HTML mail body,
    or None. Matches the link target first and the button text as a fallback,
    never the CSS class: Netflix changes the mail layout from time to time.
    """
    soup = BeautifulSoup(body, "html.parser")
    for a in soup.find_all("a", href=True):
        if CONFIRM_PATH in a["href"]:
            return a["href"]
    for a in soup.find_all("a", href=True):
        if a.get_text(" ", strip=True) == BUTTON_TEXT:
            return a["href"]
    return None


def confirm_household(link):
    """
    Opens the confirmation link and clicks the confirmation button on the
    Netflix page. Returns True on success.
    """
    if not link:
        log.warning("No '%s' link found in the mail", BUTTON_TEXT)
        return False

    # A fresh browser per mail: a crashed Chrome can't break later runs
    driver = create_driver()
    try:
        driver.get(link)
        confirm_button = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, 'button[data-uia="set-primary-location-action"]')
            )
        )
        confirm_button.click()
        log.info("Confirmation button clicked")
        return True
    except Exception:
        log.exception("Failed to locate or click the confirmation button")
        return False
    finally:
        driver.quit()


def get_html_body(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html" and "attachment" not in str(
            part.get("Content-Disposition")
        ):
            charset = part.get_content_charset() or "utf-8"
            return part.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def connect():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=30)
    mail.login(USERNAME, PASSWORD)
    ensure_folder(mail, OTHER_FOLDER)
    mail.select("inbox")
    log.info("Logged in to %s", IMAP_SERVER)
    return mail


def disconnect(mail):
    try:
        mail.logout()
    except Exception:
        pass


def decode_subject(raw):
    return str(make_header(decode_header(raw or "")))


def ensure_folder(mail, folder):
    status, data = mail.list('""', folder)
    if status == "OK" and any(data):
        return
    mail.create(folder)
    mail.subscribe(folder)
    log.info("Created folder %s", folder)


def fetch_message(mail, uid):
    status, data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
    for part in data or []:
        if isinstance(part, tuple):
            return email.message_from_bytes(part[1])
    return None


def move_mail(mail, uid, folder):
    if "MOVE" in mail.capabilities:
        mail.uid("MOVE", uid, folder)
    else:
        mail.uid("COPY", uid, folder)
        mail.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        mail.expunge()


def sort_other_mails(mail):
    """
    Keeps the inbox down to household mails: other Netflix mails are moved
    to OTHER_FOLDER and reported to the chat (E-008), everything else is
    permanently deleted, read or unread.
    """
    status, data = mail.uid("SEARCH", None, "ALL")
    uids = data[0].split()
    if not uids:
        return

    status, data = mail.uid(
        "FETCH", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])"
    )
    unneeded = []
    for part in data:
        if not isinstance(part, tuple):
            continue
        uid = re.search(rb"UID (\d+)", part[0]).group(1)
        headers = email.message_from_bytes(part[1])
        subject = decode_subject(headers["Subject"])
        kind = classify(headers["From"], subject)
        if kind == "netflix":
            keep_netflix_mail(mail, uid, subject)
        elif kind == "other":
            log.info("Deleting unneeded mail: %s", subject)
            unneeded.append(uid)

    if unneeded:
        mail.uid("STORE", b",".join(unneeded), "+FLAGS", "(\\Deleted)")
        mail.expunge()


def keep_netflix_mail(mail, uid, subject):
    msg = fetch_message(mail, uid)
    link = find_main_link(get_html_body(msg)) if msg else None
    move_mail(mail, uid, OTHER_FOLDER)
    log.info("Moved Netflix mail to %s: %s", OTHER_FOLDER, subject)
    # Notify only after the move, so a failed move can't repeat the message
    if link:
        notify(message("other_netflix", subject=subject, link=link, folder=OTHER_FOLDER))
    else:
        notify(message("other_netflix_no_link", subject=subject, folder=OTHER_FOLDER))


def check_mailbox(mail):
    # NOOP makes the server report mails that arrived since the last check
    mail.noop()

    sort_other_mails(mail)

    # UIDs stay valid across the expunge above, sequence numbers don't
    status, messages = mail.uid("SEARCH", None, "UNSEEN", "SUBJECT", '"Netflix Household"')
    for uid in messages[0].split():
        # PEEK keeps the mail unread until it has been handled
        status, msg_data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(msg_data[0][1])
        if decode_subject(msg["Subject"]) != TARGET_SUBJECT:
            continue

        message_id = msg["Message-ID"] or uid.decode()
        log.info("Target mail found: %s", message_id)
        attempts[message_id] = attempts.get(message_id, 0) + 1
        # At most two messages per mail, however often it is retried (E-007)
        if attempts[message_id] == 1:
            notify(message("household_arrived"))

        link = find_confirmation_link(get_html_body(msg))
        if confirm_household(link):
            mail.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            attempts.pop(message_id, None)
            notify(message("household_confirmed"))
        elif attempts[message_id] >= MAX_ATTEMPTS_PER_MAIL:
            log.error("Giving up on %s after %d attempts", message_id, attempts[message_id])
            mail.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            notify(household_failed_message(attempts.pop(message_id), link))


def household_failed_message(count, link):
    if link:
        return message("household_failed", attempts=count, link=link)
    return message("household_failed_no_link")


def main():
    # Let `docker stop` end the loop right away instead of waiting for SIGKILL
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    log.info("Started, checking %s every %ds", IMAP_SERVER, POLL_INTERVAL)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log.info("Telegram not configured, notifications are off")
    notify(message("started"))
    mail = None
    connected_at = 0.0
    failures = 0
    while True:
        try:
            if mail is not None and time.monotonic() - connected_at > RECONNECT_AFTER:
                disconnect(mail)
                mail = None
            if mail is None:
                mail = connect()
                connected_at = time.monotonic()
            check_mailbox(mail)
            if failures >= NOTIFY_AFTER_FAILURES:
                notify(message("check_recovered"))
            failures = 0
            HEARTBEAT_FILE.touch()
        except Exception as e:
            failures += 1
            log.exception("Mail check failed (%d/%d)", failures, MAX_CONSECUTIVE_FAILURES)
            if failures == NOTIFY_AFTER_FAILURES:
                notify(message("check_failing", failures=failures, error=type(e).__name__))
            if mail is not None:
                disconnect(mail)
                mail = None
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical("Too many failures in a row, exiting for restart")
                notify(message("exiting"))
                sys.exit(1)
            # Back off on errors so a broken login isn't retried every few seconds
            time.sleep(min(60 * failures, 600))
            continue
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
