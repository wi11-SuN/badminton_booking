"""只用模拟选场页验证放票前预读和连接的生命周期。"""
from concurrent.futures import Future
import datetime as dt
import threading
import unittest
from unittest.mock import Mock, patch

import book_badminton as booking
from test_booking import FakeClient, candidates, config
from test_multi_court import PageClient, TEN, page


class AnchorPreviewTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(("2434", "842"), anchor_court={"field_type": "2434", "field_id": "20009846"})
        self.release = dt.datetime(2026, 11, 5, 8, tzinfo=booking.TZ)
        self.date = dt.date(2026, 11, 11)
        self.current = self.release-dt.timedelta(seconds=5)
        self.primary = FakeClient(cookies={"JSESSIONID": "after-warmup"})
        self.preview_client = FakeClient()
        self.prepared = {"session": {"cookies": {"JSESSIONID": "before-warmup"}},
                         "clients": {"2434": self.primary}}
        self.addCleanup(patch.stopall)
        self.factory = patch.object(booking, "Client", return_value=self.preview_client).start()
        patch.object(booking, "now", side_effect=lambda: self.current).start()
        patch.object(booking.time, "monotonic", side_effect=lambda: self.current.timestamp()).start()
        self.wait = patch.object(booking, "wait_until", side_effect=self.wait_until).start()

    def wait_until(self, moment):
        self.current = max(self.current, moment)

    def complete_immediately(self):
        def run(fn):
            result = Future()
            try:
                result.set_result(fn())
            except Exception as exc:
                result.set_exception(exc)
            return result
        pool = Mock()
        pool.submit.side_effect = run
        patch.object(booking.concurrent.futures, "ThreadPoolExecutor", return_value=pool).start()

    def preview(self):
        return booking.anchor_at_release(self.cfg, self.prepared, self.date, self.release)

    def test_schedule_supplies_fresh_target_date_anchor_with_independent_client(self):
        self.complete_immediately()
        choices = candidates(self.preview_client, "2434", ("20009845", "20009846"))
        def intercept_book(cfg, runtime, date, *, prepared, release, prepared_anchor):
            self.assertIs(prepared, self.prepared)
            self.assertEqual(date, self.date)
            self.assertEqual(self.current, release)
            self.assertEqual(prepared_anchor.key[2], "20009846")
            self.assertIs(prepared_anchor.client, self.preview_client)
            self.assertEqual(prepared_anchor.query_started, (release-dt.timedelta(seconds=1)).timestamp())
            self.assertEqual(prepared_anchor.query_finished, prepared_anchor.query_started)
            self.assertFalse(self.preview_client.closed.is_set())
            raise KeyboardInterrupt
        cfg = dict(self.cfg, warmup_seconds=20, target_days_after_release=6)
        with patch.object(booking, "scan_kind", return_value=choices) as scan, \
             patch.object(booking, "next_release", return_value=self.release), \
             patch.object(booking, "prepare", return_value=self.prepared), \
             patch.object(booking, "book", side_effect=intercept_book):
            with self.assertRaises(KeyboardInterrupt):
                booking.schedule(cfg, None)
        scan.assert_called_once_with(self.preview_client, cfg, self.date, "2434")
        self.factory.assert_called_once_with({"cookies": self.primary.cookies}, cfg)
        self.assertTrue(self.preview_client.closed.is_set())

    def test_unavailable_or_failed_preview_falls_back(self):
        self.complete_immediately()
        for scan_result in ([], candidates(self.preview_client, "2434", ("20009845",)),
                            booking.NotAvailable("目标日期未开放")):
            with self.subTest(scan_result=scan_result):
                self.current = self.release-dt.timedelta(seconds=2)
                with patch.object(booking, "scan_kind", side_effect=[scan_result]):
                    with self.preview() as candidate:
                        self.assertIsNone(candidate)

    def test_pair_response_taking_1125ms_can_supply_direct_release_order(self):
        self.cfg["court_count"] = 2
        self.complete_immediately()
        client = PageClient(page(TEN, [24, 25, 26]))
        self.factory.return_value = client
        def timed_request(*args, **kwargs):
            self.current += dt.timedelta(milliseconds=1125)
            return PageClient.request(client, *args, **kwargs)
        with patch.object(client, "request", side_effect=timed_request):
            with self.preview() as candidate:
                self.assertEqual(self.current, self.release)
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.payload["day"], self.date.strftime("%Y%m%d"))
                fields = candidate.payload["fieldInfoList"]
                self.assertEqual(tuple(f["fieldId"] for f in fields), booking.TRAINING_PAIR)
                self.assertEqual([(f["startSegment"], f["endSegment"]) for f in fields], [("38", "42")] * 2)
                self.assertEqual(len(candidate.payload["fieldInfo"].split(",")), 4)
                self.assertNotIn("pending_anchor", self.prepared)

    def test_late_pair_preview_is_shared_until_booking_finishes(self):
        self.cfg["court_count"] = 2
        for finish_before_exit in (True, False):
            with self.subTest(finish_before_exit=finish_before_exit):
                self.current = self.release-dt.timedelta(seconds=3)
                client = PageClient(page(TEN, [25, 26]))
                self.factory.return_value = client
                job, pool = Future(), Mock()
                job.set_running_or_notify_cancel()
                pool.submit.return_value = job
                with patch.object(booking.concurrent.futures, "ThreadPoolExecutor", return_value=pool):
                    with self.preview() as candidate:
                        self.assertIsNone(candidate)
                        self.assertEqual(self.current, self.release)
                        self.assertIs(self.prepared["pending_anchor"], job)
                        finish_preview = pool.submit.call_args.args[0]
                        if finish_before_exit:
                            job.set_result(finish_preview())
                            self.assertTrue(booking.matches_anchor(self.cfg, TEN, job.result().payload))
                        self.assertFalse(client.closed.is_set())
                self.assertNotIn("pending_anchor", self.prepared)
                self.assertEqual(client.closed.is_set(), finish_before_exit)
                if not finish_before_exit:
                    job.set_result(finish_preview())
                    self.assertTrue(client.closed.is_set())

    def test_stale_preview_is_discarded(self):
        self.complete_immediately()
        def delayed_wakeup(moment):
            self.current = moment + (dt.timedelta(seconds=3) if moment == self.release else dt.timedelta())
        self.wait.side_effect = delayed_wakeup
        with patch.object(booking, "scan_kind", return_value=candidates(self.preview_client, "2434", ("20009846",))):
            with self.preview() as candidate:
                self.assertIsNone(candidate)

    def test_inflight_preview_does_not_block_release_or_cleanup(self):
        started, finish = threading.Event(), threading.Event()
        def slow_scan(*args):
            started.set()
            if not finish.wait(3):
                raise AssertionError("preview should not delay booking or cleanup")
            return candidates(self.preview_client, "2434", ("20009846",))
        def wait_for_start(moment):
            self.wait_until(moment)
            if moment == self.release:
                self.assertTrue(started.wait(1))
        self.wait.side_effect = wait_for_start
        with patch.object(booking, "scan_kind", side_effect=slow_scan):
            try:
                with self.preview() as candidate:
                    self.assertIsNone(candidate)
                    self.assertNotIn("pending_anchor", self.prepared)
                self.assertFalse(self.preview_client.closed.is_set())
            finally:
                finish.set()
            self.assertTrue(self.preview_client.closed.wait(1))

    def test_catchup_skips_preview(self):
        self.current = self.release+dt.timedelta(seconds=1)
        with self.preview() as candidate:
            self.assertIsNone(candidate)
        self.factory.assert_not_called()
