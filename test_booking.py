"""Offline scheduler/ordering regressions; no venue or session-file access."""
from contextlib import redirect_stdout
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import book_badminton as booking


DATE = dt.date(2026, 11, 11)
SESSION = {"cookies": {"JSESSIONID": "offline-test-session"}}


def config(kinds=("842",), **overrides):
    result = {
        "venue_id": "test-venue", "venue_name": "test venue", "service_id": "1002",
        "field_type_priority": list(kinds), "start_time": "19:00", "end_time": "21:00",
        "http_timeout_seconds": 1, "max_wait_seconds": 5,
        "poll_interval_seconds": 1, "max_commit_attempts": 3, "candidate_ttl_seconds": 2,
    }
    result.update(overrides)
    return result


def payload(kind="842", field="1", segment=None):
    return {
        "venueId": "test-venue", "serviceId": "1002", "fieldType": kind,
        "day": DATE.strftime("%Y%m%d"), "fieldInfo": segment or f"segment-{field}",
        "fieldInfoList": [{"fieldId": field, "fieldNum": field,
                           "startSegment": "38", "endSegment": "42",
                           "venueName": f"court-{field}", "price": 100}],
    }


def candidates(client, kind="842", fields=("1", "2")):
    return [(kind, payload(kind, field), client, booking.ROOT + "/court/test") for field in fields]


def response(data, status=200):
    return status, json.dumps(data)


SUCCESS = response({"error": 0, "tradeId": "offline-order", "payFee": 10000,
                    "trade": {"payState": "0"}})
CONFLICT = response({"error": 1, "message": "场地已被预订"})


class FakeClient:
    def __init__(self, responses=(), cookies=None):
        self.responses = list(responses)
        self.cookies = dict(cookies or SESSION["cookies"])
        self.posts = []
        self.closed = threading.Event()
        self.before_post = None

    def request(self, path, payload=None, referer=None):
        if payload is None:
            raise AssertionError("Unexpected real-style GET in this fake")
        if self.closed.is_set():
            raise AssertionError("POST on closed client")
        if self.before_post:
            self.before_post()
        self.posts.append((payload, threading.get_ident()))
        if not self.responses:
            raise AssertionError("Unexpected extra POST")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed.set()


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(booking, "notify").start()
        # Fail closed if a test accidentally reaches the real transport.
        patch.object(booking.Client, "request", side_effect=AssertionError("Network forbidden in scheduler tests")).start()

    def prepared(self, clients, rules=None):
        return {"session": SESSION, "rules": rules or {}, "clients": clients}

    def run_book(self, cfg, clients, **kwargs):
        with redirect_stdout(self.output):
            return booking.book(cfg, self.runtime, DATE, prepared=self.prepared(clients), **kwargs)

    def journal(self, cfg):
        return json.loads(booking.state_path(self.runtime, cfg, DATE).read_text(encoding="utf-8"))

    def test_fast_type_submits_and_returns_while_preferred_query_is_blocked(self):
        cfg = config(("slow", "fast"))
        clients = {"slow": FakeClient(), "fast": FakeClient([SUCCESS])}
        slow_started, release_slow, finished = threading.Event(), threading.Event(), threading.Event()
        outcome = {}

        def scan(client, cfg, date, kind):
            if kind == "slow":
                slow_started.set()
                if not release_slow.wait(3):
                    raise AssertionError("Test did not release slow query")
                return []
            if not slow_started.wait(1):
                raise AssertionError("Slow query never started")
            return candidates(client, kind, ("2",))

        def run():
            try:
                outcome["result"] = self.run_book(cfg, clients)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                finished.set()

        with patch.object(booking, "scan_kind", side_effect=scan):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                returned_early = finished.wait(1)
                slow_closed_early = clients["slow"].closed.is_set()
            finally:
                release_slow.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(clients["slow"].closed.wait(1))
        self.assertTrue(returned_early, "A slow read held up the booking result")
        self.assertFalse(slow_closed_early, "Closed a client during its active read")
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["trade_id"], "offline-order")
        self.assertEqual(len(clients["fast"].posts), 1)
        self.assertFalse(clients["slow"].posts)

    def test_conflict_uses_cached_alternative_without_sleep_or_rescan(self):
        cfg, client = config(), FakeClient([CONFLICT, SUCCESS])
        def assert_write_ahead_record():
            saved = self.journal(cfg)
            self.assertEqual(saved["state"], "sending")
            self.assertEqual(len(saved["attempts"]), len(client.posts) + 1)
        client.before_post = assert_write_ahead_record
        with patch.object(booking, "scan_kind", return_value=candidates(client)) as scan, \
             patch.object(booking.time, "sleep", side_effect=AssertionError("Unnecessary sleep")):
            result = self.run_book(cfg, {"842": client})
        self.assertEqual(scan.call_count, 1)
        self.assertEqual([p[0]["fieldInfoList"][0]["fieldId"] for p in client.posts], ["1", "2"])
        self.assertEqual({thread for _, thread in client.posts}, {threading.get_ident()})
        self.assertEqual(result["trade_id"], "offline-order")
        journal = self.journal(cfg)
        self.assertEqual(journal["state"], "created_unpaid")
        for attempt in journal["attempts"]:
            self.assertIn("query", attempt["timing_ms"])
            self.assertIn("candidate_age", attempt["timing_ms"])
            self.assertIn("commit", attempt["timing_ms"])

    def test_unknown_results_never_submit_a_second_candidate(self):
        cases = [TimeoutError("offline timeout"), (503, "unavailable"), (200, "<html>failure</html>"),
                 response({"error": 0}), response({"error": 1, "tradeId": "maybe-created"}),
                 response({"unexpected": True})]
        for index, reply in enumerate(cases):
            with self.subTest(reply=reply):
                cfg = config(start_time=f"{10+index}:00", end_time=f"{11+index}:00")
                client = FakeClient([reply, SUCCESS])
                with patch.object(booking, "scan_kind", return_value=candidates(client)):
                    with self.assertRaises(booking.UnknownResult):
                        self.run_book(cfg, {"842": client})
                self.assertEqual(len(client.posts), 1)
                self.assertEqual(self.journal(cfg)["state"], "unknown")
                self.assertTrue(client.closed.is_set())

    def test_noninventory_rejections_do_not_trigger_fallback(self):
        for index, (data, error) in enumerate([
            ({"error": 404, "message": "login required"}, booking.SessionExpired),
            ({"error": 7, "message": "预约次数达到上限"}, booking.BookingError),
        ]):
            with self.subTest(data=data):
                cfg = config(start_time=f"{10+index}:00", end_time=f"{11+index}:00")
                client = FakeClient([response(data), SUCCESS])
                with patch.object(booking, "scan_kind", return_value=candidates(client)):
                    with self.assertRaises(error):
                        self.run_book(cfg, {"842": client})
                self.assertEqual(len(client.posts), 1)
                self.assertEqual(self.journal(cfg)["state"], "rejected")

    def test_commit_budget_stops_at_three_even_with_more_cached_choices(self):
        cfg, client = config(), FakeClient([CONFLICT] * 3 + [SUCCESS])
        with patch.object(booking, "scan_kind", return_value=candidates(client, fields=("1", "2", "3", "4"))):
            with self.assertRaisesRegex(booking.BookingError, "上限"):
                self.run_book(cfg, {"842": client})
        self.assertEqual(len(client.posts), 3)
        self.assertEqual(len(self.journal(cfg)["attempts"]), 3)

    def test_existing_order_record_prevents_query_and_submission(self):
        cfg, client = config(), FakeClient([SUCCESS])
        booking.atomic_json(booking.state_path(self.runtime, cfg, DATE), {"state": "unknown"})
        with patch.object(booking, "scan_kind") as scan:
            with self.assertRaises(booking.AlreadyAttempted):
                self.run_book(cfg, {"842": client})
        scan.assert_not_called()
        self.assertFalse(client.posts)
        self.assertTrue(client.closed.is_set())

    def test_deadline_after_mandatory_delay_prevents_new_submission(self):
        cfg, client, clock = config(), FakeClient([SUCCESS]), FakeClock()
        with patch.object(booking.time, "monotonic", clock), \
             patch.object(booking, "scan_kind", return_value=candidates(client)), \
             patch.object(booking, "honor_delay", side_effect=lambda rules: clock.advance(6)):
            with self.assertRaises(booking.NotAvailable):
                self.run_book(cfg, {"842": client})
        self.assertFalse(client.posts)
        self.assertFalse(booking.state_path(self.runtime, cfg, DATE).exists())

    def test_late_query_result_cannot_start_a_post_after_deadline(self):
        cfg, client, clock = config(), FakeClient([SUCCESS]), FakeClock()
        def late_scan(*args):
            clock.advance(6)
            return candidates(client)
        with patch.object(booking.time, "monotonic", clock), \
             patch.object(booking, "scan_kind", side_effect=late_scan):
            with self.assertRaises(booking.NotAvailable):
                self.run_book(cfg, {"842": client})
        self.assertFalse(client.posts)

    def test_slow_write_ahead_record_cannot_send_past_deadline(self):
        cfg, client, clock = config(), FakeClient([SUCCESS]), FakeClock()
        write_json = booking.atomic_json
        def slow_write(path, data):
            write_json(path, data)
            if data["state"] == "sending":
                clock.advance(6)
        with patch.object(booking.time, "monotonic", clock), \
             patch.object(booking, "scan_kind", return_value=candidates(client)), \
             patch.object(booking, "atomic_json", side_effect=slow_write):
            with self.assertRaisesRegex(booking.BookingError, "窗口"):
                self.run_book(cfg, {"842": client})
        self.assertFalse(client.posts)
        self.assertEqual(self.journal(cfg)["state"], "not_sent")

    def test_real_page_parser_and_fallback_use_the_same_updated_client(self):
        html = '''
            <input id="date" value="20261111">
            <input id="fieldType" value="842">
            <input id="venueId" value="test-venue">
            <input id="serviceId" value="1002">
            <dd date="20261111"></dd>
            <input name="field" value="842" data-book-hour="1">
            <div id="field-type-842" data-max-hour="8" data-total-hour="8"
                 data-min-serial-num="2" data-pieces-unit="0" data-max-field="1"></div>
        '''
        for field in ("1", "2"):
            for start, end in ((38, 40), (40, 42)):
                html += (f'<span field-id="{field}" field-num="{field}" state="0" price="50" '
                         f'start-time="{start}" end-time="{end}" '
                         f'field-segment-id="{field}-{start}" venue-name="court-{field}"></span>')

        class PageClient(FakeClient):
            def __init__(self):
                super().__init__([CONFLICT, SUCCESS])
                self.operations = []
                self.busy = threading.Lock()

            def request(self, path, payload=None, referer=None):
                if not self.busy.acquire(blocking=False):
                    raise AssertionError("GET and POST overlap on one client")
                try:
                    self.operations.append("GET" if payload is None else "POST")
                    if payload is None:
                        self.cookies["JSESSIONID"] = "updated-by-court-query"
                        return 200, html
                    if self.cookies["JSESSIONID"] != "updated-by-court-query":
                        raise AssertionError("Lost the query's cookie context")
                    return super().request(path, payload, referer)
                finally:
                    self.busy.release()

        client = PageClient()
        self.run_book(config(), {"842": client})
        self.assertEqual(client.operations, ["GET", "POST", "POST"])
        self.assertEqual(client.posts[0][0]["fieldInfo"], "1-38,1-40")
        self.assertEqual(client.posts[1][0]["fieldInfo"], "2-38,2-40")

    def test_expired_candidate_after_delay_is_refreshed_without_repeating_delay(self):
        cfg, client, clock = config(), FakeClient([SUCCESS]), FakeClock()
        with patch.object(booking.time, "monotonic", clock), \
             patch.object(booking, "scan_kind", return_value=candidates(client)) as scan, \
             patch.object(booking, "honor_delay", side_effect=lambda rules: clock.advance(3)) as delay:
            self.run_book(cfg, {"842": client})
        self.assertEqual(delay.call_count, 1)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(len(client.posts), 1)

    def test_expired_cache_is_refreshed_and_conflicted_physical_court_is_not_retried(self):
        cfg, client, clock = config(), FakeClient(), FakeClock()
        calls = []
        def scan(*args):
            calls.append(1)
            if len(calls) == 1:
                return candidates(client)
            changed = payload(field="1", segment="new-id-for-same-court")
            return [("842", changed, client, booking.ROOT + "/court/test")] + candidates(client, fields=("3",))
        with patch.object(booking.time, "monotonic", clock), patch.object(booking, "scan_kind", side_effect=scan):
            feed = booking.CandidateFeed({"842": client}, cfg, DATE, 110)
            try:
                first = feed.next_candidate(set())
                clock.advance(3)
                second = feed.next_candidate({first.key})
                self.assertEqual(second.payload["fieldInfoList"][0]["fieldId"], "3")
                self.assertEqual(len(calls), 2)
                self.assertTrue(feed.fresh(second))
            finally:
                feed.close()

    def test_query_authentication_failure_stops_without_post(self):
        cfg, client = config(), FakeClient([SUCCESS])
        with patch.object(booking, "scan_kind", side_effect=booking.SessionExpired("offline expired")):
            with self.assertRaises(booking.SessionExpired):
                self.run_book(cfg, {"842": client})
        self.assertFalse(client.posts)
        self.assertTrue(client.closed.is_set())

    def test_dry_run_lists_all_types_without_post_or_order_record(self):
        cfg = config(("842", "843"))
        made = []
        def factory(session, cfg):
            client = FakeClient()
            made.append(client)
            return client
        def scan(client, cfg, date, kind):
            return candidates(client, kind, ("1",))
        with patch.object(booking, "load_session", return_value=SESSION), \
             patch.object(booking, "Client", side_effect=factory), \
             patch.object(booking, "probe"), patch.object(booking, "scan_kind", side_effect=scan), \
             redirect_stdout(self.output):
            result = booking.book(cfg, self.runtime, DATE, dry=True)
        self.assertIsNone(result)
        summary = json.loads(self.output.getvalue())
        self.assertFalse(summary["submitted"])
        self.assertEqual([c["field_type"] for c in summary["candidates"]], ["842", "843"])
        self.assertFalse((self.runtime / "orders").exists())
        self.assertTrue(all(c.closed.is_set() and not c.posts for c in made))

    def test_prepare_keeps_warmed_clients_and_updated_cookie_context(self):
        cfg, made, initial_cookies = config(("842", "843", "2440")), [], []
        def factory(session, cfg):
            initial_cookies.append(dict(session["cookies"]))
            client = FakeClient(cookies=session["cookies"])
            made.append(client)
            return client
        def probe(client, cfg):
            client.cookies["read"] = "verified"
        def rules(client, cfg):
            client.cookies["JSESSIONID"] = "after-rules"
            return {"field_notice_countdown": "0"}
        with patch.object(booking, "load_session", return_value=SESSION), \
             patch.object(booking, "Client", side_effect=factory), \
             patch.object(booking, "probe", side_effect=probe) as probe_mock, \
             patch.object(booking, "booking_rules", side_effect=rules):
            prepared = booking.prepare(cfg, self.runtime)
        try:
            self.assertEqual(list(prepared["clients"].values()), made)
            self.assertEqual(probe_mock.call_count, 3)
            self.assertEqual(initial_cookies[1:], [{"JSESSIONID": "after-rules", "read": "verified"}] * 2)
            self.assertTrue(all(not c.closed.is_set() for c in made))
            made[0].cookies["new"] = "only-first-client"
            self.assertNotIn("new", made[1].cookies)
        finally:
            booking.close_clients(prepared["clients"])


if __name__ == "__main__":
    unittest.main()
