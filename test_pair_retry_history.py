"""Offline regressions for pair retries across prefetch and inventory refreshes."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

import book_badminton as booking
from test_booking import CONFLICT, DATE, SESSION, SUCCESS, FakeClock, config
from test_multi_court import COURTS, PAIR, TEN, UP, PageClient, ids, page


class PairRetryHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        for stub in (patch.object(booking, "notify"),
                     patch.object(booking.Client, "request", side_effect=AssertionError("No live requests"))):
            stub.start()
            self.addCleanup(stub.stop)

    def book(self, cfg, client, prepared_anchor=None):
        prepared = {"session": SESSION, "rules": {}, "clients": {TEN: client}}
        with redirect_stdout(io.StringIO()):
            return booking.book(cfg, self.runtime, DATE, prepared=prepared,
                                prepared_anchor=prepared_anchor)

    def assert_pair_submits_while_release_queries_are_pending(self, preview_ready):
        cfg = config((TEN, UP), court_count=2)
        preview_client = PageClient(page(TEN, [25, 26]))
        self.addCleanup(preview_client.close)
        preview = booking.scan_kind(preview_client, cfg, DATE, TEN)[0]
        readers = {TEN: PageClient(page(TEN, [24, 27])), UP: PageClient(page(UP, [1, 2]))}
        prepared = {"session": SESSION, "rules": {}, "clients": readers}
        delayed = Future()
        read_started = {kind: threading.Event() for kind in readers}
        finish_reads = threading.Event()
        scan = booking.scan_kind

        def scan_on_release(client, cfg, date, kind):
            read_started[kind].set()
            if not finish_reads.wait(5):
                raise AssertionError("Release query was never unblocked")
            return scan(client, cfg, date, kind)

        def both_reads_started():
            for started in read_started.values():
                self.assertTrue(started.wait(2), "A background release query never started")

        preview_client.before_post = both_reads_started
        started = time.monotonic()
        if preview_ready:
            anchor = booking.Candidate(*preview, started, started)
        else:
            anchor = None
            prepared["pending_anchor"] = delayed

        with patch.object(booking, "scan_kind", side_effect=scan_on_release), \
             ThreadPoolExecutor(1) as pool, redirect_stdout(io.StringIO()):
            run = pool.submit(booking.book, cfg, self.runtime, DATE, prepared=prepared,
                              release=booking.now(), prepared_anchor=anchor)
            try:
                if not preview_ready:
                    both_reads_started()
                    delayed.set_result(booking.Candidate(*preview, started, time.monotonic()))
                self.assertEqual(run.result(timeout=2)["trade_id"], "offline-order")
            finally:
                finish_reads.set()
            for reader in readers.values():
                self.assertTrue(reader.closed.wait(2))
        self.assertTrue(all(not reader.posts for reader in readers.values()))
        self.assertEqual(len(preview_client.posts), 1)
        order = preview_client.posts[0][0]
        self.assertEqual(ids(order), PAIR)
        day = DATE.strftime("%Y%m%d")
        expected = [f"{day}-{COURTS[TEN][field]}-{start}" for field in (25, 26) for start in (38, 40)]
        self.assertEqual(order["fieldInfo"].split(","), expected)
        self.assertEqual([f["fieldSegmentId"] for f in order["fieldInfoList"]], expected[1::2])
        journal = json.loads(booking.state_path(self.runtime, cfg, DATE).read_text(encoding="utf-8"))
        self.assertEqual(len(journal["attempts"]), 1)

    def test_ready_pair_submits_once_while_both_release_reads_are_in_flight(self):
        self.assert_pair_submits_while_release_queries_are_pending(preview_ready=True)

    def test_just_finished_preview_submits_once_while_both_release_reads_are_in_flight(self):
        self.assert_pair_submits_while_release_queries_are_pending(preview_ready=False)

    def test_invalid_delayed_preview_falls_back_to_target_day_query(self):
        cfg = config((TEN,), court_count=2)
        for failure in ("wrong-day", "expired", "timeout"):
            with self.subTest(failure=failure):
                preview_client = PageClient(page(TEN, [25, 26]))
                self.addCleanup(preview_client.close)
                preview = booking.scan_kind(preview_client, cfg, DATE, TEN)[0]
                delayed = Future()
                if failure == "timeout":
                    delayed.set_exception(TimeoutError("Offline preview timed out"))
                else:
                    if failure == "wrong-day":
                        preview[1]["day"] = "20261110"
                    finished = time.monotonic() - (60 if failure == "expired" else 0)
                    delayed.set_result(booking.Candidate(*preview, finished, finished))
                reader = PageClient(page(TEN, [24, 27]))
                prepared = {"session": SESSION, "rules": {}, "clients": {TEN: reader},
                            "pending_anchor": delayed}
                with redirect_stdout(io.StringIO()):
                    booking.book(cfg, self.runtime / failure, DATE, prepared=prepared)
                self.assertFalse(preview_client.posts)
                self.assertEqual([ids(data) for data, _ in reader.posts],
                                 [{COURTS[TEN][24], COURTS[TEN][27]}])

    def test_prefetched_pair_conflict_guides_selection_from_changed_inventory(self):
        cfg = config((TEN,), court_count=2)
        preview_client = PageClient(page(TEN, [25, 26]), [CONFLICT])
        self.addCleanup(preview_client.close)
        preview = booking.scan_kind(preview_client, cfg, DATE, TEN)[0]
        finished = time.monotonic()
        anchor = booking.Candidate(*preview, finished - .01, finished)
        # Court 26 disappeared after the prefetch. The new page's first pair
        # still includes 25, but 24+27 avoids both courts in the failed order.
        current_client = PageClient(page(TEN, [24, 25, 27]), [SUCCESS])

        result = self.book(cfg, current_client, prepared_anchor=anchor)

        self.assertEqual(result["trade_id"], "offline-order")
        self.assertEqual([ids(data) for data, _ in preview_client.posts], [PAIR])
        self.assertEqual([ids(data) for data, _ in current_client.posts],
                         [{COURTS[TEN][24], COURTS[TEN][27]}])
        saved = json.loads(booking.state_path(self.runtime, cfg, DATE).read_text(encoding="utf-8"))
        self.assertEqual(len(saved["attempts"]), 2)

    def test_expired_cache_refresh_keeps_conflict_history_and_excludes_failed_pair(self):
        cfg = config((TEN,), court_count=2, candidate_ttl_seconds=2, max_wait_seconds=20)
        clock = FakeClock()
        client = PageClient(page(TEN, [24, 25, 26]), [CONFLICT, SUCCESS])
        queries = []
        original_request = client.request

        def request(path, payload=None, referer=None):
            if payload is None:
                queries.append(clock())
            return original_request(path, payload, referer)

        def invalidate_first_snapshot():
            if not client.posts:
                # The first failed POST takes longer than the cache TTL.
                # A fresh page adds court 27 and includes the failed pair again.
                client.html = page(TEN, [24, 25, 26, 27])
                clock.advance(3)

        client.before_post = invalidate_first_snapshot

        def complete_query(fn, *args):
            future = Future()
            try:
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)
            return future

        pool = Mock()
        pool.submit.side_effect = complete_query
        with patch.object(client, "request", side_effect=request), \
             patch.object(booking.concurrent.futures, "ThreadPoolExecutor", return_value=pool), \
             patch.object(booking.time, "monotonic", side_effect=clock), \
             patch.object(booking.time, "sleep", side_effect=clock.advance):
            result = self.book(cfg, client)

        self.assertEqual(result["trade_id"], "offline-order")
        self.assertEqual(queries, [100, 103])
        attempted = [ids(data) for data, _ in client.posts]
        self.assertEqual(attempted, [PAIR, {COURTS[TEN][24], COURTS[TEN][27]}])
        self.assertEqual(len({frozenset(pair) for pair in attempted}), len(attempted))


if __name__ == "__main__":
    unittest.main()
