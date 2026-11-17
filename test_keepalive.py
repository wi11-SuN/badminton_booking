"""Exercise the scheduler's keepalive and alert lifecycle without external traffic."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import book_badminton as booking
from test_booking import DATE, FakeClient, candidates, config, response


class KeepaliveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)
        self.cfg = {"session_alert_email": "example@example.test", "http_timeout_seconds": 1}
        self.upload("first")
        self.probe = self.patch("member_session_probe")
        self.mail = self.patch("send_expiration_email")

    def patch(self, name):
        p = patch.object(booking, name)
        self.addCleanup(p.stop)
        return p.start()

    def upload(self, value):
        booking.atomic_json(self.runtime/"session.json", {"cookies": {"JSESSIONID": value}})

    def tick(self):
        booking.keepalive_once(self.cfg, self.runtime)

    def test_expiration_notified_once_until_recovered(self):
        expired = booking.SessionExpired("expired")
        self.probe.side_effect = [expired, expired, None, expired]
        for _ in range(4):
            self.tick()
        self.assertEqual(self.mail.call_count, 2)
        self.assertEqual(self.mail.call_args.args[0], self.cfg["session_alert_email"])

    def test_retries_mail_delivery_after_failure(self):
        self.probe.side_effect = booking.SessionExpired("expired")
        self.mail.side_effect = [OSError("mail offline"), None]
        with self.assertRaises(OSError):
            self.tick()
        self.tick()
        self.tick()
        self.assertEqual(self.mail.call_count, 2)

    def test_network_failure_and_missing_file_do_not_send_expiry_notice(self):
        self.probe.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.tick()
        (self.runtime/"session.json").unlink()
        with self.assertRaises(booking.SessionExpired):
            self.tick()
        self.mail.assert_not_called()

    def test_new_upload_during_probe_supersedes_expired_result(self):
        def replace_then_expire(client):
            self.upload("new")
            raise booking.SessionExpired("old session")
        self.probe.side_effect = replace_then_expire
        self.tick()
        self.mail.assert_not_called()

    def test_updated_cookies_available_to_booking_and_scoped_to_upload(self):
        self.probe.side_effect = lambda client: client.cookies.update(JSESSIONID="rotated")
        self.tick()
        self.assertEqual(booking.load_session(self.runtime)["cookies"]["JSESSIONID"], "rotated")
        self.upload("new-upload")
        self.assertEqual(booking.load_session(self.runtime)["cookies"]["JSESSIONID"], "new-upload")

    def test_late_response_cannot_replace_a_new_uploads_rotated_cookies(self):
        def old_response(client):
            self.upload("new")
            source, _ = booking.session_snapshot(self.runtime)
            booking.atomic_json(self.runtime/"session-cookies.json",
                                {"source": source, "cookies": {"JSESSIONID": "new-rotated"}})
            client.cookies["JSESSIONID"] = "old-rotated"
        self.probe.side_effect = old_response
        self.tick()
        self.assertEqual(booking.load_session(self.runtime)["cookies"]["JSESSIONID"], "new-rotated")

    def test_guest_cookies_from_an_expired_response_are_discarded(self):
        def guest(client):
            client.cookies["JSESSIONID"] = "anonymous"
            raise booking.SessionExpired("expired")
        self.probe.side_effect = guest
        self.tick()
        self.assertEqual(booking.load_session(self.runtime)["cookies"]["JSESSIONID"], "first")

    def test_booking_login_rejection_alerts_even_if_order_page_remains_accessible(self):
        cfg = dict(config(), **self.cfg)
        client = FakeClient([response({"error": 404, "message": "会员登录失效"})])
        prepared = {"session": booking.load_session(self.runtime), "clients": {"842": client}, "rules": {}}
        with patch.object(booking, "scan_kind", return_value=candidates(client)):
            with self.assertRaises(booking.SessionExpired):
                booking.book(cfg, self.runtime, DATE, prepared=prepared)
        self.tick()
        self.tick()
        self.mail.assert_called_once()
        self.upload("new")
        self.tick()
        self.probe.assert_called_once()

    def test_schedule_starts_worker_and_stops_it_on_exit(self):
        import threading
        started = threading.Event()
        stopped = threading.Event()

        def worker(cfg, runtime, stop):
            started.set()
            if stop.wait(2):
                stopped.set()

        def weekly(cfg, runtime):
            self.assertTrue(started.wait(1))
            raise KeyboardInterrupt

        with patch.object(booking, "keepalive_loop", side_effect=worker), \
             patch.object(booking, "weekly_bookings", side_effect=weekly), \
             patch.object(booking, "validate_smtp_settings"):
            with self.assertRaises(KeyboardInterrupt):
                booking.schedule(self.cfg, self.runtime)
        self.assertTrue(stopped.is_set())

    def test_failure_does_not_stop_periodic_checks(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        self.probe.side_effect = [TimeoutError(), None]
        with patch.object(booking.time, "monotonic", side_effect=[10, 11, 190, 193]):
            booking.keepalive_loop(self.cfg, self.runtime, stop)
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [179, 177])
        self.mail.assert_not_called()
