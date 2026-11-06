"""
Tests for the pure functions of notflix-parser.py (no network, no browser).

Run with `scripts/test.sh`, which uses the service image so the
dependencies match production.
"""

import importlib.util
import os
import unittest
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
