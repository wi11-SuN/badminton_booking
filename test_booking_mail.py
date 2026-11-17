"""Offline SMTP configuration and transport tests."""
from datetime import datetime, timezone
import os
import smtplib
import unittest
from unittest.mock import patch

import booking_mail as mail


class MailTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "BOOKING_SMTP_HOST": "smtp.example.test",
            "BOOKING_SMTP_USER": "sender@example.test",
            "BOOKING_SMTP_PASSWORD": "mock-password",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.recipient = "recipient@example.test"

    @patch("booking_mail.smtplib.SMTP_SSL")
    def test_expiration_message_over_tls(self, connect):
        mail.send_expiration_email(self.recipient, datetime(2026, 11, 17, 10, tzinfo=timezone.utc))
        self.assertEqual(connect.call_args.args, ("smtp.example.test", 465))
        self.assertIs(connect.call_args.kwargs["context"].check_hostname, True)
        smtp = connect.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("sender@example.test", "mock-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(str(message["To"]), self.recipient)
        self.assertEqual(str(message["From"]), "sender@example.test")
        self.assertIn("失效", str(message["Subject"]))
        self.assertIn("2026-11-17", message.get_content())
        self.assertIn("refresh-session", message.get_content())

    @patch("booking_mail.smtplib.SMTP")
    def test_starttls_before_login_and_explicit_test_notice(self, connect):
        with patch.dict(os.environ, {"BOOKING_SMTP_SECURITY": "starttls",
                                    "BOOKING_SMTP_FROM": "booking@example.test"}):
            mail.send_test_email(self.recipient)
        self.assertEqual(connect.call_args.args, ("smtp.example.test", 587))
        smtp = connect.return_value.__enter__.return_value
        self.assertEqual([call[0] for call in smtp.method_calls], ["starttls", "login", "send_message"])
        self.assertIs(smtp.starttls.call_args.kwargs["context"].check_hostname, True)
        message = smtp.send_message.call_args.args[0]
        self.assertIn("测试", str(message["Subject"]))
        self.assertEqual(str(message["From"]), "booking@example.test")

    @patch("booking_mail.smtplib.SMTP_SSL")
    def test_validate_has_no_network_side_effect(self, connect):
        mail.validate_smtp_settings(self.recipient)
        for field, value in [("BOOKING_SMTP_SECURITY", "plain"), ("BOOKING_SMTP_PORT", "70000"),
                             ("BOOKING_SMTP_PASSWORD", ""), ("BOOKING_SMTP_FROM", "name")]:
            with self.subTest(field=field), patch.dict(os.environ, {field: value}):
                with self.assertRaises(mail.MailError):
                    mail.validate_smtp_settings(self.recipient)
        with self.assertRaises(mail.MailError):
            mail.validate_smtp_settings("first@example.test\r\nBcc: other@example.test")
        connect.assert_not_called()

    @patch("booking_mail.smtplib.SMTP_SSL")
    def test_server_message_cannot_leak_credentials(self, connect):
        smtp = connect.return_value.__enter__.return_value
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"mock-password echoed")
        with self.assertRaises(mail.MailError) as caught:
            mail.send_test_email(self.recipient)
        self.assertIn("535", str(caught.exception))
        self.assertNotIn("mock-password", str(caught.exception))
        smtp.send_message.assert_not_called()
