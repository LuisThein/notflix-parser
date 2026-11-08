"""
Tests for the pure functions of notflix-parser.py (no network, no browser).

Run with `scripts/test.sh`, which uses the service image so the
dependencies match production.
"""

import email
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest import mock

# The module reads these at import time; dummy values keep it offline
os.environ.setdefault("NOTFLIX_EMAIL", "test@example.invalid")
os.environ.setdefault("NOTFLIX_PASSWORT", "unused")
os.environ.setdefault("IMAP_SERVER", "imap.example.invalid")

_path = Path(__file__).resolve().parent.parent / "notflix-parser.py"
_spec = importlib.util.spec_from_file_location("notflix_parser", _path)
parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parser)

CONFIRM_URL = "https://www.netflix.com/account/update-primary-location?nftoken=abc"

# Shortened from the household mail layout Netflix uses since November 2026
MAIL_2026_11 = f"""
<p>Did you request a code? If not, <a href="https://www.netflix.com/password?g=1">changing your password.</a></p>
<table><tr><td>
  <a class="body-standard-heavy" href="{CONFIRM_URL}" style="color:#fff">Yes, This Was Me</a>
</td></tr></table>
<a href="https://www.netflix.com/ManageAccountAccess?g=1">sign out of all devices that you don't recognize</a>
<a class="footer-link help-center" href="https://help.netflix.com/help?g=1">Help Center</a>
"""

# Shortened from the layout used until early November 2026
MAIL_2024 = f"""
<a href="https://www.netflix.com/password?g=1">change your password</a>
<a class="h5" href="{CONFIRM_URL}">Yes, This Was Me</a>
"""


class FindConfirmationLinkTest(unittest.TestCase):
    def test_current_layout(self):
        # Regression: the class changed from h5 to body-standard-heavy (2026-11-06)
        self.assertEqual(parser.find_confirmation_link(MAIL_2026_11), CONFIRM_URL)

    def test_old_layout(self):
        self.assertEqual(parser.find_confirmation_link(MAIL_2024), CONFIRM_URL)

    def test_text_fallback_when_target_changes(self):
        body = '<a class="x" href="https://www.netflix.com/new-path?t=1"> Yes, This Was Me </a>'
        self.assertEqual(parser.find_confirmation_link(body), "https://www.netflix.com/new-path?t=1")

    def test_target_wins_over_text(self):
        body = (
            '<a href="https://example.invalid/other">Yes, This Was Me</a>'
            f'<a href="{CONFIRM_URL}">Confirm</a>'
        )
        self.assertEqual(parser.find_confirmation_link(body), CONFIRM_URL)

    def test_text_split_across_tags(self):
        body = f'<a href="{CONFIRM_URL}"><span>Yes,</span> <b>This Was Me</b></a>'
        self.assertEqual(parser.find_confirmation_link(body), CONFIRM_URL)

    def test_no_link(self):
        body = '<a href="https://www.netflix.com/password">change your password</a>'
        self.assertIsNone(parser.find_confirmation_link(body))

    def test_link_without_href_is_ignored(self):
        self.assertIsNone(parser.find_confirmation_link("<a>Yes, This Was Me</a>"))

    def test_empty_body(self):
        self.assertIsNone(parser.find_confirmation_link(""))


class GetHtmlBodyTest(unittest.TestCase):
    def test_picks_html_part(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("plain text", "plain"))
        msg.attach(MIMEText("<p>Grüße</p>", "html", "utf-8"))
        self.assertEqual(parser.get_html_body(msg), "<p>Grüße</p>")

    def test_skips_html_attachment(self):
        msg = MIMEMultipart("mixed")
        attachment = MIMEText("<p>attached</p>", "html")
        attachment.add_header("Content-Disposition", "attachment", filename="a.html")
        msg.attach(attachment)
        msg.attach(MIMEText("<p>body</p>", "html"))
        self.assertEqual(parser.get_html_body(msg), "<p>body</p>")

    def test_latin1_charset(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("<p>Grüße</p>", "html", "iso-8859-1"))
        self.assertEqual(parser.get_html_body(msg), "<p>Grüße</p>")

    def test_no_html_part(self):
        self.assertEqual(parser.get_html_body(MIMEText("plain only", "plain")), "")


class DecodeSubjectTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parser.decode_subject(parser.TARGET_SUBJECT), parser.TARGET_SUBJECT)

    def test_encoded_word(self):
        raw = Header(parser.TARGET_SUBJECT, "utf-8").encode()
        self.assertNotEqual(raw, parser.TARGET_SUBJECT)
        self.assertEqual(parser.decode_subject(raw), parser.TARGET_SUBJECT)

    def test_missing_subject(self):
        self.assertEqual(parser.decode_subject(None), "")


class ClassifyTest(unittest.TestCase):
    def test_household_mail(self):
        self.assertEqual(
            parser.classify("Netflix <info@account.netflix.com>", parser.TARGET_SUBJECT), "household"
        )

    def test_other_netflix_mail(self):
        self.assertEqual(
            parser.classify("Netflix <info@mailer.netflix.com>", "Your sign-in code"), "netflix"
        )

    def test_netflix_main_domain(self):
        self.assertEqual(parser.classify("info@netflix.com", "News"), "netflix")

    def test_domain_case_is_ignored(self):
        self.assertEqual(parser.classify("Info@Account.NETFLIX.com", "News"), "netflix")

    def test_lookalike_domains_are_not_netflix(self):
        for sender in ("x@netflix.com.example.invalid", "x@notnetflix.com", "netflix.com <x@spam.invalid>"):
            with self.subTest(sender=sender):
                self.assertEqual(parser.classify(sender, "Netflix account"), "other")

    def test_missing_from(self):
        self.assertEqual(parser.classify(None, "Hello"), "other")

    def test_access_code_mail(self):
        self.assertEqual(
            parser.classify("Netflix <info@account.netflix.com>", parser.ACCESS_CODE_SUBJECT),
            "access_code",
        )

    def test_new_device_mail_is_deleted(self):
        # E-011: no message for this one
        self.assertEqual(
            parser.classify("Netflix <info@account.netflix.com>", "A new device is using your account"),
            "other",
        )

    def test_household_confirmed_mail_is_deleted(self):
        self.assertEqual(
            parser.classify("Netflix <info@account.netflix.com>", parser.HOUSEHOLD_CONFIRMED_SUBJECT),
            "other",
        )

    def test_access_code_subject_from_stranger_is_other(self):
        self.assertEqual(parser.classify("x@spam.invalid", parser.ACCESS_CODE_SUBJECT), "other")


CODE_URL = "https://www.netflix.com/account/travel/verify?nftoken=abc"

# Shortened from the access code mail of 2026-11-07, tokens and names removed
ACCESS_CODE_MAIL = f"""
<p>Hi Member,</p>
<p>We received a request for a temporary access code from the device below.</p>
<a href="{CODE_URL}">Get Code</a>
<p>* Link expires after 15 minutes.</p>
<a href="https://www.netflix.com/ManageAccountAccess?g=1">sign out of all devices that you don't recognize</a>
<a href="https://www.netflix.com/password?g=1">change your password</a>
<a href="https://help.netflix.com/help?g=1">Help Center</a>
"""


class FindLinkByPathTest(unittest.TestCase):
    def test_access_code_link(self):
        self.assertEqual(
            parser.find_link_by_path(ACCESS_CODE_MAIL, parser.ACCESS_CODE_PATH), CODE_URL
        )

    def test_path_must_match_exactly(self):
        body = '<a href="https://www.netflix.com/account/travel/verify-other">x</a>'
        self.assertIsNone(parser.find_link_by_path(body, parser.ACCESS_CODE_PATH))

    def test_main_link_of_access_code_mail(self):
        self.assertEqual(parser.find_main_link(ACCESS_CODE_MAIL), CODE_URL)


# Shortened from the German code page of 2026-11-07, code and profiles changed
ACCESS_CODE_PAGE_2026_11 = """
<div class="profile-name">Member</div>
<h1>Verwenden Sie diesen Code, um Netflix auf Ihrem Gerät zu nutzen</h1>
<p>Geben Sie diesen Code auf dem Gerät ein, über das Sie ihn angefordert haben.
Dann erhalten Sie vorübergehend Zugang.</p>
<div data-uia="travel-verification-otp">4821</div>
<p>Dieser Code läuft nach 15 Minuten ab.</p>
<a data-uia="footer-link" href="https://help.netflix.com/">Hilfe-Center</a>
"""


class ExtractAccessCodeTest(unittest.TestCase):
    def test_current_page(self):
        self.assertEqual(parser.extract_access_code(ACCESS_CODE_PAGE_2026_11), "4821")

    def test_data_uia_element(self):
        page = '<p>Footer 2026</p><div data-uia="travel-code"> 4711 </div>'
        self.assertEqual(parser.extract_access_code(page), "4711")

    def test_digits_in_separate_boxes(self):
        page = '<div data-uia="access-code"><span>1</span><span>2</span><span>3</span><span>4</span></div>'
        self.assertEqual(parser.extract_access_code(page), "1234")

    def test_text_after_code(self):
        page = "<h1>Your temporary access code</h1><p>Enter this code on your TV:</p><strong>8352</strong><p>© 2026 Netflix</p>"
        self.assertEqual(parser.extract_access_code(page), "8352")

    def test_ignores_scripts(self):
        page = '<script>var code = 99999;</script><p>Something went wrong</p>'
        self.assertIsNone(parser.extract_access_code(page))

    def test_no_code(self):
        page = "<h1>This link has expired</h1><p>Request a new code from your TV.</p>"
        self.assertIsNone(parser.extract_access_code(page))

    def test_ignores_longer_numbers(self):
        page = "<p>Your code request 1234567 was received.</p>"
        self.assertIsNone(parser.extract_access_code(page))


class FindMainLinkTest(unittest.TestCase):
    def test_first_netflix_link_with_text(self):
        body = (
            '<a href="https://www.netflix.com/browse?g=1"><img src="logo.png"></a>'
            '<a href="https://example.invalid/track">Ad</a>'
            '<a href="https://www.netflix.com/account/travel/verify?t=1">Get Code</a>'
            '<a href="https://www.netflix.com/browse?g=2">Watch now</a>'
        )
        self.assertEqual(
            parser.find_main_link(body), "https://www.netflix.com/account/travel/verify?t=1"
        )

    def test_skips_footer_links(self):
        body = (
            '<a href="https://help.netflix.com/help">Questions?</a>'
            '<a href="https://www.netflix.com/unsubscribe?t=1">Unsubscribe</a>'
            '<a href="https://www.netflix.com/privacy">Privacy Statement</a>'
            '<a href="https://www.netflix.com/account">Account</a>'
        )
        self.assertEqual(parser.find_main_link(body), "https://www.netflix.com/account")

    def test_no_netflix_link(self):
        self.assertIsNone(parser.find_main_link('<a href="https://example.invalid/">Go</a>'))

    def test_empty_body(self):
        self.assertIsNone(parser.find_main_link(""))


@mock.patch.object(parser, "TELEGRAM_CHAT_ID", "-100123")
@mock.patch.object(parser, "TELEGRAM_BOT_TOKEN", "123:secret")
class NotifyTest(unittest.TestCase):
    def test_sends_message_without_link_preview(self):
        with mock.patch.object(parser.urllib.request, "urlopen") as urlopen:
            parser.send_telegram("hello https://www.netflix.com/x")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bot123:secret/sendMessage")
        payload = json.loads(request.data)
        self.assertEqual(payload["chat_id"], "-100123")
        self.assertEqual(payload["text"], "hello https://www.netflix.com/x")
        self.assertEqual(payload["parse_mode"], "HTML")
        # A preview would make Telegram open the confirmation link itself
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

    def test_errors_are_swallowed_and_token_not_logged(self):
        error = parser.urllib.error.URLError("no network")
        with mock.patch.object(parser.urllib.request, "urlopen", side_effect=error):
            with self.assertLogs("notflix", "WARNING") as logs:
                parser.send_telegram("hello")
        self.assertNotIn("secret", "\n".join(logs.output))

    def test_rejected_html_is_sent_as_plain_text(self):
        error = parser.urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        with mock.patch.object(parser.urllib.request, "urlopen", side_effect=[error, mock.MagicMock()]) as urlopen:
            with self.assertLogs("notflix", "WARNING"):
                parser.send_telegram('<a href="https://x.invalid/?a=1&amp;b=2">Öffnen</a>')
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(payload["text"], "Öffnen: https://x.invalid/?a=1&b=2")

    def test_silent_without_configuration(self):
        with mock.patch.object(parser, "TELEGRAM_BOT_TOKEN", ""):
            with mock.patch.object(parser, "send_telegram") as send:
                parser.notify("hello")
                parser.flush_notifications(1)
        send.assert_not_called()

    def test_notify_returns_before_sending(self):
        # E-014: a slow Telegram must not delay the caller
        release = parser.threading.Event()
        sent = []

        def slow_send(text):
            release.wait(5)
            sent.append(text)

        with mock.patch.object(parser, "send_telegram", side_effect=slow_send):
            parser.notify("one")
            parser.notify("two")
            self.assertEqual(sent, [])
            release.set()
            parser.flush_notifications(5)
        self.assertEqual(sent, ["one", "two"])

    def test_failing_send_does_not_stop_the_queue(self):
        calls = []

        def send(text):
            calls.append(text)
            if text == "bad":
                raise RuntimeError("boom")

        with mock.patch.object(parser, "send_telegram", side_effect=send), self.assertLogs("notflix"):
            parser.notify("bad")
            parser.notify("good")
            parser.flush_notifications(5)
        self.assertEqual(calls, ["bad", "good"])


class MessagesTest(unittest.TestCase):
    def test_all_messages_format(self):
        values = dict(attempts=3, link="L", subject="S", folder="F", failures=3, error="E", code="C")
        for key in parser.MESSAGES:
            with self.subTest(key=key):
                self.assertTrue(parser.message(key, **values))

    def test_household_failed_contains_link(self):
        self.assertIn('<a href="https://x.invalid/l">', parser.household_failed_message(3, "https://x.invalid/l"))

    def test_values_are_escaped(self):
        # Subjects and links must not break the Telegram HTML
        text = parser.message(
            "other_netflix", subject="<b>Tom & Jerry</b>", link='https://x.invalid/?a=1&b="2"', folder="F"
        )
        self.assertIn("&lt;b&gt;Tom &amp; Jerry&lt;/b&gt;", text)
        self.assertIn('<a href="https://x.invalid/?a=1&amp;b=&quot;2&quot;">', text)

    def test_links_sit_behind_text(self):
        for key in ("household_failed", "access_code", "access_code_unread", "other_netflix"):
            with self.subTest(key=key):
                text = parser.message(key, attempts=3, link="https://x.invalid/l", subject="S", folder="F", code="1234")
                self.assertNotIn("\nhttps://", text)
                self.assertIn('<a href="https://x.invalid/l">', text)

    def test_access_code_can_be_copied(self):
        text = parser.message("access_code", code="4711", link="L")
        self.assertIn("<code>4711</code>", text)

    def test_plain_text(self):
        self.assertEqual(parser.plain_text('A &amp; <b>B</b> <a href="https://x.invalid/">C</a>'), "A & B C: https://x.invalid/")

    def test_household_failed_without_link(self):
        self.assertEqual(
            parser.household_failed_message(3, None), parser.MESSAGES["household_failed_no_link"]
        )


def make_mail(sender, subject, html="<p>no links</p>"):
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{abs(hash((sender, subject)))}@example.invalid>"
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg.as_bytes()


class FakeImap:
    """Just enough of imaplib.IMAP4 for sort_other_mails and check_mailbox."""

    def __init__(self, mails, move=True):
        self.inbox = {str(uid).encode(): raw for uid, raw in enumerate(mails, start=1)}
        self.seen = set()
        self.deleted = set()
        self.moved = {}
        self.fetched = []
        self.capabilities = ("IMAP4REV1", "MOVE") if move else ("IMAP4REV1",)

    def noop(self):
        return "OK", [b""]

    def expunge(self):
        for uid in self.deleted:
            self.inbox.pop(uid, None)
        self.deleted = set()
        return "OK", [b""]

    def uid(self, command, *args):
        uids = lambda arg: (arg if isinstance(arg, bytes) else arg.encode()).split(b",")
        if command == "SEARCH":
            # GMX's SUBJECT search lags behind new mails (E-017)
            assert "SUBJECT" not in args, "SUBJECT search is slow on GMX"
            found = [u for u in self.inbox if "UNSEEN" not in args or u not in self.seen]
            return "OK", [b" ".join(found)]
        if command == "FETCH":
            data = []
            for u in uids(args[0]):
                self.fetched.append(u)
                raw = self.inbox[u]
                if "HEADER.FIELDS" in args[1]:
                    raw = raw.split(b"\n\n")[0] + b"\n\n"
                data += [(b"1 (UID " + u + b" BODY[] {1}", raw), b")"]
            return "OK", data
        if command == "STORE":
            target = self.deleted if "Deleted" in args[2] else self.seen
            target.update(uids(args[0]))
            return "OK", [b""]
        if command in ("MOVE", "COPY"):
            for u in uids(args[0]):
                self.moved[u] = args[1]
                if command == "MOVE":
                    self.inbox.pop(u)
            return "OK", [b""]
        raise AssertionError(command)


HOUSEHOLD = make_mail(
    "Netflix <info@account.netflix.com>",
    parser.TARGET_SUBJECT,
    f'<a href="{CONFIRM_URL}">Yes, This Was Me</a>',
)
CODE_MAIL = make_mail(
    "Netflix <info@account.netflix.com>",
    "Your temporary access code",
    '<a href="https://www.netflix.com/account/travel/verify?t=1">Get Code</a>',
)
SPAM = make_mail("Shop <deals@shop.example.invalid>", "50% off")
ACCESS_CODE = make_mail(
    "Netflix <info@account.netflix.com>", parser.ACCESS_CODE_SUBJECT, ACCESS_CODE_MAIL
)
NEW_DEVICE = make_mail(
    "Netflix <info@account.netflix.com>",
    "A new device is using your account",
    '<a href="https://www.netflix.com/password?g=1">change your password</a>',
)


@mock.patch.object(parser, "notify")
class SortOtherMailsTest(unittest.TestCase):
    def setUp(self):
        parser.household_uids.clear()

    def test_moves_netflix_deletes_rest_keeps_household(self, notify):
        mail = FakeImap([HOUSEHOLD, CODE_MAIL, SPAM])
        parser.sort_other_mails(mail)
        self.assertEqual(list(mail.inbox), [b"1"])
        self.assertEqual(mail.moved, {b"2": parser.OTHER_FOLDER})
        notify.assert_called_once()
        text = notify.call_args.args[0]
        self.assertIn("Your temporary access code", text)
        self.assertIn("https://www.netflix.com/account/travel/verify?t=1", text)

    def test_copy_fallback_without_move(self, notify):
        mail = FakeImap([CODE_MAIL], move=False)
        parser.sort_other_mails(mail)
        self.assertEqual(mail.moved, {b"1": parser.OTHER_FOLDER})
        self.assertEqual(mail.inbox, {})

    def test_netflix_mail_without_link(self, notify):
        mail = FakeImap([make_mail("info@netflix.com", "News")])
        parser.sort_other_mails(mail)
        self.assertIn("kein Link", notify.call_args.args[0])

    def test_access_code_is_read_and_sent(self, notify):
        mail = FakeImap([ACCESS_CODE])
        with mock.patch.object(parser, "read_access_code", return_value="4711") as read:
            parser.sort_other_mails(mail)
        read.assert_called_once_with(CODE_URL)
        self.assertEqual(mail.moved, {b"1": parser.OTHER_FOLDER})
        text = notify.call_args.args[0]
        self.assertIn("4711", text)
        self.assertIn(CODE_URL, text)

    def test_access_code_unreadable_sends_link(self, notify):
        mail = FakeImap([ACCESS_CODE])
        with mock.patch.object(parser, "read_access_code", return_value=None):
            parser.sort_other_mails(mail)
        self.assertEqual(
            notify.call_args.args[0], parser.message("access_code_unread", link=CODE_URL)
        )

    def test_new_device_mail_deleted_silently(self, notify):
        mail = FakeImap([NEW_DEVICE])
        parser.sort_other_mails(mail)
        self.assertEqual(mail.inbox, {})
        self.assertEqual(mail.moved, {})
        notify.assert_not_called()

    def test_household_confirmed_mail_deleted_and_logged(self, notify):
        mail = FakeImap([make_mail("Netflix <info@account.netflix.com>", parser.HOUSEHOLD_CONFIRMED_SUBJECT)])
        with self.assertLogs("notflix", "INFO") as logs:
            parser.sort_other_mails(mail)
        self.assertEqual(mail.inbox, {})
        self.assertEqual(mail.moved, {})
        notify.assert_not_called()
        self.assertIn("Netflix confirmed the household update", "\n".join(logs.output))

    def test_spam_is_deleted_silently(self, notify):
        mail = FakeImap([SPAM])
        parser.sort_other_mails(mail)
        self.assertEqual(mail.inbox, {})
        self.assertEqual(mail.moved, {})
        notify.assert_not_called()


    def test_known_household_headers_are_not_fetched_again(self, notify):
        # E-013: only the first poll after login fetches all headers
        parser.household_uids.clear()
        mail = FakeImap([HOUSEHOLD, HOUSEHOLD])
        parser.sort_other_mails(mail)
        mail.fetched.clear()
        parser.sort_other_mails(mail)
        self.assertEqual(mail.fetched, [])
        parser.household_uids.clear()


class FakeButton:
    def __init__(self, driver):
        self.driver = driver

    def is_displayed(self):
        return not self.driver.confirmed

    def is_enabled(self):
        return True

    def click(self):
        self.driver.clicks += 1
        if self.driver.clicks >= self.driver.react_on_click:
            self.driver.confirmed = True


class FakeDriver:
    """Just enough of a Selenium WebDriver for confirm_household."""

    def __init__(self, react_on_click):
        self.react_on_click = react_on_click
        self.clicks = 0
        self.confirmed = False
        self.current_url = "about:blank"
        self.page_source = "<html></html>"
        self.quit = mock.Mock()

    def get(self, url):
        # Netflix redirects while loading; that alone is no reaction
        self.current_url = url + "&redirected=1"

    def find_element(self, by, value):
        return FakeButton(self)


@mock.patch.object(parser, "CLICK_REACTION_TIMEOUT", 0.2)
class ConfirmHouseholdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.page = mock.patch.object(parser, "CONFIRM_PAGE", Path(self.tmp.name) / "page.html")
        self.page.start()

    def tearDown(self):
        self.page.stop()
        self.tmp.cleanup()

    def confirm(self, driver):
        with mock.patch.object(parser, "create_driver", return_value=driver):
            return parser.confirm_household(CONFIRM_URL)

    def test_page_reacts_to_first_click(self):
        driver = FakeDriver(react_on_click=1)
        self.assertTrue(self.confirm(driver))
        self.assertEqual(driver.clicks, 1)
        driver.quit.assert_called_once()
        self.assertTrue(parser.CONFIRM_PAGE.exists())

    def test_clicks_again_when_page_does_not_react(self):
        # E-012: an early click before the JavaScript is ready may do nothing
        driver = FakeDriver(react_on_click=2)
        self.assertTrue(self.confirm(driver))
        self.assertEqual(driver.clicks, 2)

    def test_fails_when_page_never_reacts(self):
        driver = FakeDriver(react_on_click=99)
        with self.assertLogs("notflix", "WARNING"):
            self.assertFalse(self.confirm(driver))
        self.assertEqual(driver.clicks, parser.CLICK_TRIES)
        driver.quit.assert_called_once()

    def test_no_link(self):
        with mock.patch.object(parser, "create_driver") as create:
            with self.assertLogs("notflix", "WARNING"):
                self.assertFalse(parser.confirm_household(None))
        create.assert_not_called()


class ReceivedAtTest(unittest.TestCase):
    def test_topmost_received_header(self):
        msg = email.message_from_string(
            "Received: from mx.example.invalid by imap.example.invalid; Sat, 07 Nov 2026 10:43:38 +0100\n"
            "Received: from sender.example.invalid; Sat, 07 Nov 2026 10:43:30 +0100\n\nbody"
        )
        self.assertEqual(
            parser.received_at(msg), datetime(2026, 11, 7, 9, 43, 38, tzinfo=timezone.utc)
        )

    def test_missing_or_broken_header(self):
        self.assertIsNone(parser.received_at(email.message_from_string("Subject: x\n\nbody")))
        self.assertIsNone(parser.received_at(email.message_from_string("Received: nonsense\n\nbody")))


@mock.patch.object(parser, "notify")
class CheckMailboxTest(unittest.TestCase):
    def setUp(self):
        parser.attempts.clear()
        parser.household_uids.clear()

    def test_success_sends_two_messages(self, notify):
        mail = FakeImap([HOUSEHOLD])
        with mock.patch.object(parser, "confirm_household", return_value=True) as confirm:
            parser.check_mailbox(mail)
        confirm.assert_called_once_with(CONFIRM_URL)
        self.assertEqual(
            [c.args[0] for c in notify.call_args_list],
            [parser.MESSAGES["household_arrived"], parser.MESSAGES["household_confirmed"]],
        )
        self.assertIn(b"1", mail.seen)

    def test_retries_send_at_most_two_messages(self, notify):
        mail = FakeImap([HOUSEHOLD])
        with mock.patch.object(parser, "confirm_household", return_value=False):
            for _ in range(parser.MAX_ATTEMPTS_PER_MAIL + 1):
                parser.check_mailbox(mail)
        texts = [c.args[0] for c in notify.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertEqual(texts[0], parser.MESSAGES["household_arrived"])
        self.assertIn(CONFIRM_URL, texts[1])
        self.assertIn(b"1", mail.seen)

    def test_household_found_among_unread_mails(self, notify):
        # Regression: SUBJECT search found new mails 10-27 s late on GMX (E-017)
        mail = FakeImap([SPAM, HOUSEHOLD, CODE_MAIL])
        mail.seen.add(b"1")
        with mock.patch.object(parser, "confirm_household", return_value=True) as confirm:
            parser.confirm_household_mails(mail)
        confirm.assert_called_once_with(CONFIRM_URL)
        # Unread mails other than the household mail only had their headers read
        self.assertEqual(mail.fetched, [b"2", b"3", b"2"])

    def test_known_household_headers_are_not_fetched_again(self, notify):
        mail = FakeImap([HOUSEHOLD])
        with mock.patch.object(parser, "confirm_household", return_value=False):
            parser.check_mailbox(mail)
            mail.fetched.clear()
            parser.confirm_household_mails(mail)
        self.assertEqual(mail.fetched, [b"1"])


class StartMessageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "update-note-sent"
        patcher = mock.patch.object(parser, "UPDATE_NOTE_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_update_note_once_then_start_message(self):
        self.assertEqual(parser.start_message(), parser.UPDATE_NOTE)
        self.assertEqual(parser.start_message(), parser.MESSAGES["started"])

    def test_new_note_id_is_sent_again(self):
        parser.start_message()
        with mock.patch.object(parser, "UPDATE_NOTE_ID", "next"):
            self.assertEqual(parser.start_message(), parser.UPDATE_NOTE)

    def test_start_message_when_id_cannot_be_stored(self):
        with mock.patch.object(parser, "UPDATE_NOTE_FILE", self.file / "missing" / "x"):
            self.assertEqual(parser.start_message(), parser.MESSAGES["started"])


if __name__ == "__main__":
    unittest.main()
