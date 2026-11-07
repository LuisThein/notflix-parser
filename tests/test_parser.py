"""
Tests for the pure functions of notflix-parser.py (no network, no browser).

Run with `scripts/test.sh`, which uses the service image so the
dependencies match production.
"""

import importlib.util
import json
import os
import unittest
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
            parser.notify("hello https://www.netflix.com/x")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bot123:secret/sendMessage")
        payload = json.loads(request.data)
        self.assertEqual(payload["chat_id"], "-100123")
        self.assertEqual(payload["text"], "hello https://www.netflix.com/x")
        # A preview would make Telegram open the confirmation link itself
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

    def test_errors_are_swallowed_and_token_not_logged(self):
        error = parser.urllib.error.URLError("no network")
        with mock.patch.object(parser.urllib.request, "urlopen", side_effect=error):
            with self.assertLogs("notflix", "WARNING") as logs:
                parser.notify("hello")
        self.assertNotIn("secret", "\n".join(logs.output))

    def test_silent_without_configuration(self):
        with mock.patch.object(parser, "TELEGRAM_BOT_TOKEN", ""):
            with mock.patch.object(parser.urllib.request, "urlopen") as urlopen:
                parser.notify("hello")
        urlopen.assert_not_called()


class MessagesTest(unittest.TestCase):
    def test_all_messages_format(self):
        values = dict(attempts=3, link="L", subject="S", folder="F", failures=3, error="E")
        for key in parser.MESSAGES:
            with self.subTest(key=key):
                self.assertTrue(parser.message(key, **values))

    def test_household_failed_contains_link(self):
        self.assertIn("https://x.invalid/l", parser.household_failed_message(3, "https://x.invalid/l"))

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
            found = [u for u in self.inbox if "UNSEEN" not in args or u not in self.seen]
            if "SUBJECT" in args:
                found = [u for u in found if b"Netflix Household" in self.inbox[u]]
            return "OK", [b" ".join(found)]
        if command == "FETCH":
            data = []
            for u in uids(args[0]):
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


@mock.patch.object(parser, "notify")
class SortOtherMailsTest(unittest.TestCase):
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

    def test_spam_is_deleted_silently(self, notify):
        mail = FakeImap([SPAM])
        parser.sort_other_mails(mail)
        self.assertEqual(mail.inbox, {})
        self.assertEqual(mail.moved, {})
        notify.assert_not_called()


@mock.patch.object(parser, "notify")
class CheckMailboxTest(unittest.TestCase):
    def setUp(self):
        parser.attempts.clear()

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


if __name__ == "__main__":
    unittest.main()
