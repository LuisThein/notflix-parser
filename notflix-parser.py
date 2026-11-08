import email
import html
import imaplib
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from email.header import decode_header, make_header
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
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
# Temporary access code mail: the code is only shown on the page behind
# the "Get Code" button (E-010)
ACCESS_CODE_SUBJECT = "Your Netflix temporary access code"
ACCESS_CODE_PATH = "/account/travel/verify"
# The last access code page is saved here, to sharpen the code detection
ACCESS_CODE_PAGE = Path("/tmp/notflix-access-code-page.html")
# The page after the confirmation click is saved here for analysis (E-012)
CONFIRM_PAGE = Path("/tmp/notflix-confirm-page.html")
CONFIRM_BUTTON = (By.CSS_SELECTOR, 'button[data-uia="set-primary-location-action"]')
# Clicks per browser session until the page reacts, and seconds to wait each time
CLICK_TRIES = 3
CLICK_REACTION_TIMEOUT = 5
# Netflix mails that are deleted without a message (E-011)
HOUSEHOLD_CONFIRMED_SUBJECT = "Confirmation: Your Netflix Household has been confirmed"
SILENT_DELETE_SUBJECTS = ("A new device is using your account", HOUSEHOLD_CONFIRMED_SUBJECT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("notflix")

attempts = {}
# UIDs known to be household mails, so their headers aren't fetched on every
# poll; cleared on every login because UIDs are only valid per session (E-013)
household_uids = set()

# Chat messages, in German for the household chat. Telegram HTML: links sit
# behind a short text, the access code in <code> can be copied with one tap.
# Values are escaped in message(), so the templates are the only markup.
MESSAGES = {
    "started": "Notflix Parser gestartet.",
    "household_arrived": "Household-Mail angekommen, ich bestätige den Haushalt …",
    "household_confirmed": "✅ Haushalt bestätigt.",
    "household_failed": '⚠️ Haushalt nach {attempts} Versuchen nicht bestätigt. <a href="{link}">Jetzt selbst bestätigen</a>',
    "household_failed_no_link": "⚠️ Haushalt nicht bestätigt: In der Mail war kein Bestätigungslink.",
    "access_code": '🔑 Netflix-Zugangscode: <code>{code}</code>\n<a href="{link}">Code-Seite öffnen</a>',
    "access_code_unread": '🔑 Netflix-Zugangscode angefordert, den Code konnte ich nicht lesen. <a href="{link}">Code-Seite öffnen</a> (15 Minuten gültig)',
    "access_code_no_link": "🔑 Netflix-Zugangscode angefordert, aber in der Mail war kein Link.",
    "other_netflix": '📩 Andere Netflix-Mail: {subject}\n<a href="{link}">Link öffnen</a> (abgelegt in {folder})',
    "other_netflix_no_link": "📩 Andere Netflix-Mail: {subject}\n(kein Link gefunden, abgelegt in {folder})",
    "check_failing": "⚠️ Mail-Abruf schlägt fehl ({failures}x in Folge): {error}",
    "check_recovered": "Mail-Abruf läuft wieder.",
    "exiting": "❌ Zu viele Fehler in Folge, der Dienst startet neu.",
}

# Link texts and hosts that are never the main link of a Netflix mail
FOOTER_TEXTS = ("help", "unsubscribe", "privacy", "terms", "notification", "preferences")
FOOTER_HOSTS = ("help.netflix.com",)


def message(key, **values):
    escaped = {name: html.escape(str(value)) for name, value in values.items()}
    return MESSAGES[key].format(**escaped)


def plain_text(text):
    """The message without markup, with each link written out after its text."""
    text = re.sub(r'<a href="([^"]*)">([^<]*)</a>', r"\2: \1", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


notifications = queue.Queue()
notifier_lock = threading.Lock()
notifier = None


def notify(text):
    """
    Queues a message for the Telegram chat and returns at once; a background
    thread sends it, so a slow Telegram never delays a confirmation (E-014).
    """
    global notifier
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    with notifier_lock:
        if notifier is None or not notifier.is_alive():
            notifier = threading.Thread(target=send_queued, name="notifier", daemon=True)
            notifier.start()
    notifications.put(text)


def send_queued():
    while True:
        text = notifications.get()
        try:
            send_telegram(text)
        except Exception:
            # send_telegram doesn't raise; this only keeps the thread alive
            log.exception("Notifier failed")
        finally:
            notifications.task_done()


def flush_notifications(timeout=10):
    """Waits until queued messages are sent, at most timeout seconds."""
    deadline = time.monotonic() + timeout
    while notifications.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.1)


def send_telegram(text):
    """
    Sends an HTML message to the Telegram chat. Never raises: a failed
    notification must not stop the mail handling. If Telegram rejects the
    markup, the message is sent again as plain text.
    """
    try:
        try:
            post_message(text, html_markup=True)
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            log.warning("Telegram rejected the HTML message, sending it as plain text")
            post_message(plain_text(text), html_markup=False)
    except Exception as e:
        # Only the error type and text: the URL contains the bot token
        log.warning("Telegram notification failed: %s: %s", type(e).__name__, e)


def post_message(text, html_markup):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        # A link preview would make Telegram open the confirmation link
        "link_preview_options": {"is_disabled": True},
    }
    if html_markup:
        payload["parse_mode"] = "HTML"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def is_netflix_sender(from_header):
    domain = parseaddr(from_header or "")[1].rpartition("@")[2].lower()
    return domain == "netflix.com" or domain.endswith(".netflix.com")


def classify(from_header, subject):
    """
    Returns "household" for the household mail, "access_code" for the
    temporary access code mail, "netflix" for any other mail from Netflix
    and "other" (deleted) for everything else.
    """
    if subject == TARGET_SUBJECT:
        return "household"
    if not is_netflix_sender(from_header) or subject in SILENT_DELETE_SUBJECTS:
        return "other"
    if subject == ACCESS_CODE_SUBJECT:
        return "access_code"
    return "netflix"


def find_link_by_path(body, path):
    soup = BeautifulSoup(body, "html.parser")
    for a in soup.find_all("a", href=True):
        if urlsplit(a["href"]).path == path:
            return a["href"]
    return None


# Four to six digits, optionally shown one per box, not part of a longer number
CODE_PATTERN = r"(?<![\d-])(\d(?:\s?\d){3,5})(?![\d-])"


def extract_access_code(page_html):
    """
    Returns the temporary access code shown on the Netflix page, or None.
    The page of 2026-11-07 shows it in data-uia="travel-verification-otp";
    any data-uia with "otp" or "code" counts, then digits shortly after the
    word "code" as a fallback (E-010).
    """
    soup = BeautifulSoup(page_html, "html.parser")
    for element in soup.find_all(attrs={"data-uia": re.compile("otp|code", re.I)}):
        match = re.fullmatch(r"\s*" + CODE_PATTERN + r"\s*", element.get_text(" ", strip=True))
        if match:
            return re.sub(r"\s", "", match.group(1))
    for script in soup(["script", "style"]):
        script.decompose()
    text = soup.get_text(" ", strip=True)
    match = re.search(r"code\D{0,80}?" + CODE_PATTERN, text, re.I)
    if match:
        return re.sub(r"\s", "", match.group(1))
    return None


def read_access_code(link):
    """
    Opens the "Get Code" page and returns the code shown there, or None.
    Saves the page for later analysis either way.
    """
    driver = create_driver()
    try:
        driver.get(link)
        try:
            return WebDriverWait(driver, 20).until(
                lambda d: extract_access_code(d.page_source)
            )
        except Exception:
            log.warning("No access code found on the page")
            return None
        finally:
            save_page(driver, ACCESS_CODE_PAGE)
    except Exception:
        log.exception("Failed to open the access code page")
        return None
    finally:
        driver.quit()


def access_code_message(link):
    if not link:
        return message("access_code_no_link")
    code = read_access_code(link)
    if code:
        return message("access_code", code=code, link=link)
    return message("access_code_unread", link=link)


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
    # Don't wait for images and trackers; callers wait for the element they need (E-012)
    options.page_load_strategy = "none"
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

    started = time.monotonic()
    # A fresh browser per mail: a crashed Chrome can't break later runs
    driver = create_driver()
    try:
        browser_ready = time.monotonic()
        driver.get(link)
        button = WebDriverWait(driver, 30).until(EC.element_to_be_clickable(CONFIRM_BUTTON))
        page_ready = time.monotonic()
        # Taken only now: redirects while loading must not count as a reaction
        url = driver.current_url
        for click in range(1, CLICK_TRIES + 1):
            button.click()
            if page_reacted(driver, button, url):
                log.info(
                    "Confirmation button clicked (click %d); browser %.1f s, page %.1f s, "
                    "click %.1f s",
                    click,
                    browser_ready - started,
                    page_ready - browser_ready,
                    time.monotonic() - page_ready,
                )
                return True
            log.warning("Page did not react to click %d", click)
            # The page may have been rendered again: find the button anew
            button = WebDriverWait(driver, 10).until(EC.element_to_be_clickable(CONFIRM_BUTTON))
        return False
    except Exception:
        log.exception("Failed to locate or click the confirmation button")
        return False
    finally:
        save_page(driver, CONFIRM_PAGE)
        driver.quit()


def page_reacted(driver, button, url):
    """
    True when the click took effect: the button is gone or the page moved on.
    A click before the page's JavaScript is ready may do nothing (E-012).
    """
    def reacted(d):
        if d.current_url != url:
            return True
        try:
            return not button.is_displayed()
        except StaleElementReferenceException:
            return True

    try:
        WebDriverWait(driver, CLICK_REACTION_TIMEOUT).until(reacted)
        return True
    except TimeoutException:
        return False


def save_page(driver, path):
    try:
        path.write_text(driver.page_source)
    except Exception as e:
        log.warning("Could not save the page to %s: %s", path, type(e).__name__)


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
    household_uids.clear()
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
    uids = [uid for uid in data[0].split() if uid not in household_uids]
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
        if kind == "household":
            household_uids.add(uid)
        elif kind in ("netflix", "access_code"):
            keep_netflix_mail(mail, uid, subject, kind)
        elif kind == "other":
            if subject == HOUSEHOLD_CONFIRMED_SUBJECT and is_netflix_sender(headers["From"]):
                # Netflix's own receipt: proof that the click took effect
                log.info("Netflix confirmed the household update")
            log.info("Deleting unneeded mail: %s", subject)
            unneeded.append(uid)

    if unneeded:
        mail.uid("STORE", b",".join(unneeded), "+FLAGS", "(\\Deleted)")
        mail.expunge()


def keep_netflix_mail(mail, uid, subject, kind="netflix"):
    msg = fetch_message(mail, uid)
    body = get_html_body(msg) if msg else ""
    if kind == "access_code":
        # The code page is read before the move: the link is valid for 15 minutes only
        text = access_code_message(find_link_by_path(body, ACCESS_CODE_PATH))
    else:
        link = find_main_link(body)
        if link:
            text = message("other_netflix", subject=subject, link=link, folder=OTHER_FOLDER)
        else:
            text = message("other_netflix_no_link", subject=subject, folder=OTHER_FOLDER)
    move_mail(mail, uid, OTHER_FOLDER)
    log.info("Moved Netflix mail to %s: %s", OTHER_FOLDER, subject)
    # Notify only after the move, so a failed move can't repeat the message
    notify(text)


def check_mailbox(mail):
    # NOOP makes the server report mails that arrived since the last check
    mail.noop()

    # Household mails first, tidying up can wait (E-013)
    confirm_household_mails(mail)
    sort_other_mails(mail)


def confirm_household_mails(mail):
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
            log_delay(msg)
        elif attempts[message_id] >= MAX_ATTEMPTS_PER_MAIL:
            log.error("Giving up on %s after %d attempts", message_id, attempts[message_id])
            mail.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            notify(household_failed_message(attempts.pop(message_id), link))


def received_at(msg):
    """Time the receiving mail server stamped into the topmost Received header."""
    received = msg.get_all("Received") or []
    try:
        return parsedate_to_datetime(received[0].rpartition(";")[2].strip())
    except (IndexError, TypeError, ValueError):
        return None


def log_delay(msg):
    arrived = received_at(msg)
    if arrived:
        delay = (datetime.now(timezone.utc) - arrived).total_seconds()
        log.info("Confirmed %.0f s after the mail server received the mail", delay)


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
                flush_notifications()
                sys.exit(1)
            # Back off on errors so a broken login isn't retried every few seconds
            time.sleep(min(60 * failures, 600))
            continue
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
