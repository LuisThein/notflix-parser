import email
import imaplib
import logging
import os
import re
import signal
import sys
import time
from email.header import decode_header, make_header
from pathlib import Path

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

TARGET_SUBJECT = "Important: How to update your Netflix Household"
BUTTON_TEXT = "Yes, This Was Me"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("notflix")

attempts = {}


def create_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    service = Service(executable_path=CHROMEDRIVER_PATH) if CHROMEDRIVER_PATH else Service()
    return webdriver.Chrome(options=options, service=service)


def confirm_household(body):
    """
    Extracts the confirmation link from the HTML mail body and clicks the
    confirmation button on the Netflix page. Returns True on success.
    """
    soup = BeautifulSoup(body, "html.parser")
    button = soup.find("a", class_="h5", string=BUTTON_TEXT)
    if not button or not button.get("href"):
        log.warning("No '%s' link found in the mail", BUTTON_TEXT)
        return False

    # A fresh browser per mail: a crashed Chrome can't break later runs
    driver = create_driver()
    try:
        driver.get(button["href"])
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


def delete_unneeded_mails(mail):
    """
    Permanently deletes every mail in the inbox whose subject is not the
    household confirmation subject, read or unread.
    """
    status, data = mail.uid("SEARCH", None, "ALL")
    uids = data[0].split()
    if not uids:
        return

    status, data = mail.uid("FETCH", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    unneeded = []
    for part in data:
        if not isinstance(part, tuple):
            continue
        uid = re.search(rb"UID (\d+)", part[0]).group(1)
        subject = decode_subject(email.message_from_bytes(part[1])["Subject"])
        if subject != TARGET_SUBJECT:
            log.info("Deleting unneeded mail: %s", subject)
            unneeded.append(uid)

    if unneeded:
        mail.uid("STORE", b",".join(unneeded), "+FLAGS", "(\\Deleted)")
        mail.expunge()


def check_mailbox(mail):
    # NOOP makes the server report mails that arrived since the last check
    mail.noop()

    delete_unneeded_mails(mail)

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

        if confirm_household(get_html_body(msg)):
            mail.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            attempts.pop(message_id, None)
        elif attempts[message_id] >= MAX_ATTEMPTS_PER_MAIL:
            log.error("Giving up on %s after %d attempts", message_id, attempts[message_id])
            mail.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            attempts.pop(message_id, None)


def main():
    # Let `docker stop` end the loop right away instead of waiting for SIGKILL
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    log.info("Started, checking %s every %ds", IMAP_SERVER, POLL_INTERVAL)
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
            failures = 0
            HEARTBEAT_FILE.touch()
        except Exception:
            failures += 1
            log.exception("Mail check failed (%d/%d)", failures, MAX_CONSECUTIVE_FAILURES)
            if mail is not None:
                disconnect(mail)
                mail = None
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical("Too many failures in a row, exiting for restart")
                sys.exit(1)
            # Back off on errors so a broken login isn't retried every few seconds
            time.sleep(min(60 * failures, 600))
            continue
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
