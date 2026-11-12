"""Offline regressions for training court 25 and its two allowed fallback types."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import book_badminton as booking
from test_booking import CONFLICT, DATE, SESSION, SUCCESS, FakeClient, FakeClock, config, payload
from test_multi_court import COURTS, PageClient, ids, page


TEN_METRE, UPSTAIRS = "2434", "842"
TRAINING_25 = "20009846"
TRAINING_24 = "20009845"
UPSTAIRS_1 = "20003157"


def choices(client, kind, *fields):
    result = []
    for field in fields:
        data = payload(kind, field)
        number = {TRAINING_25: "25", TRAINING_24: "24", UPSTAIRS_1: "1"}[field]
        name = f"训练{number}" if kind == TEN_METRE else f"{number}号场"
        data["fieldInfoList"][0].update(fieldNum=number, venueName=name)
        result.append((kind, data, client, booking.ROOT + "/court/test"))
    return result


def submitted(client):
    return [data["fieldInfoList"][0]["fieldId"] for data, _ in client.posts]


def prefetched(client, day=DATE.strftime("%Y%m%d"), age=0):
    item = choices(client, TEN_METRE, TRAINING_25)[0]
    item[1]["day"] = day
    finished = time.monotonic() - age
    return booking.Candidate(*item, finished - .01, finished)


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.cfg = config((TEN_METRE, UPSTAIRS), anchor_court={
            "field_type": TEN_METRE, "field_id": TRAINING_25,
        })
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(booking, "notify").start()
        patch.object(booking.Client, "request", side_effect=AssertionError("Live network forbidden")).start()

    def book(self, clients, prepared_anchor=None, runtime=None):
        prepared = {"session": SESSION, "rules": {}, "clients": clients}
        with redirect_stdout(self.output):
            return booking.book(self.cfg, runtime or self.runtime, DATE,
                                prepared=prepared, prepared_anchor=prepared_anchor)

    def test_fast_upstairs_read_cannot_be_the_first_post_while_anchor_is_pending(self):
        anchor_lookup, finish_anchor, upstairs_ready = (threading.Event() for _ in range(3))
        any_post = threading.Event()
        clients = {TEN_METRE: FakeClient([SUCCESS]), UPSTAIRS: FakeClient([SUCCESS])}
        for client in clients.values():
            client.before_post = any_post.set

        def scan(client, cfg, date, kind):
            if kind == TEN_METRE:
                anchor_lookup.set()
                if not finish_anchor.wait(4):
                    raise AssertionError("Anchor read never released")
                return choices(client, kind, TRAINING_24, TRAINING_25)
            self.assertEqual(kind, UPSTAIRS)
            upstairs_ready.set()
            return choices(client, kind, UPSTAIRS_1)

        with patch.object(booking, "scan_kind", side_effect=scan), ThreadPoolExecutor(1) as pool:
            outcome = pool.submit(self.book, clients)
            try:
                self.assertTrue(anchor_lookup.wait(2))
                self.assertTrue(upstairs_ready.wait(2))
                self.assertFalse(any_post.wait(.12), "Submitted another court while the anchor read was pending")
            finally:
                finish_anchor.set()
            self.assertEqual(outcome.result(timeout=3)["trade_id"], "offline-order")
        self.assertEqual(submitted(clients[TEN_METRE]), [TRAINING_25])
        self.assertEqual(submitted(clients[UPSTAIRS]), [])

    def test_fresh_prepared_anchor_submits_while_both_release_queries_are_pending(self):
        reader_clients = {TEN_METRE: FakeClient(), UPSTAIRS: FakeClient()}
        anchor_client = FakeClient([SUCCESS])
        started = {kind: threading.Event() for kind in reader_clients}
        finish_reads = threading.Event()

        def scan(client, cfg, date, kind):
            started[kind].set()
            if not finish_reads.wait(5):
                raise AssertionError("Parallel read never released")
            return choices(client, kind, TRAINING_25 if kind == TEN_METRE else UPSTAIRS_1)

        def assert_reads_are_in_flight():
            for kind, signal in started.items():
                self.assertTrue(signal.wait(2), f"Missing independent read for {kind}")
            self.assertFalse(finish_reads.is_set())

        anchor_client.before_post = assert_reads_are_in_flight
        with patch.object(booking, "scan_kind", side_effect=scan), ThreadPoolExecutor(1) as pool:
            outcome = pool.submit(self.book, reader_clients, prefetched(anchor_client))
            try:
                result = outcome.result(timeout=3)
            finally:
                finish_reads.set()
        self.assertEqual(result["trade_id"], "offline-order")
        self.assertEqual(submitted(anchor_client), [TRAINING_25])
        self.assertTrue(all(not c.posts for c in reader_clients.values()))

    def test_stale_or_wrong_date_prepared_anchor_is_discarded_and_queried_again(self):
        for name, kwargs in (("wrong-day", {"day": "20261110"}), ("stale", {"age": 120})):
            with self.subTest(case=name):
                clients = {TEN_METRE: FakeClient([SUCCESS]), UPSTAIRS: FakeClient([SUCCESS])}
                prefetch_client = FakeClient([SUCCESS])
                old = prefetched(prefetch_client, **kwargs)

                def scan(client, cfg, date, kind):
                    self.assertEqual(date, DATE)
                    return choices(client, kind, TRAINING_25 if kind == TEN_METRE else UPSTAIRS_1)

                with patch.object(booking, "scan_kind", side_effect=scan) as scans:
                    result = self.book(clients, old, self.runtime / name)
                self.assertEqual(result["trade_id"], "offline-order")
                self.assertIn(TEN_METRE, {call.args[3] for call in scans.call_args_list})
                self.assertEqual(submitted(clients[TEN_METRE]), [TRAINING_25])
                self.assertEqual(submitted(clients[UPSTAIRS]), [])
                self.assertEqual(submitted(prefetch_client), [])

    def test_explicit_conflict_changes_to_another_ten_metre_court(self):
        clients = {TEN_METRE: FakeClient([CONFLICT, SUCCESS]), UPSTAIRS: FakeClient()}

        def scan(client, cfg, date, kind):
            if kind == TEN_METRE:
                return choices(client, kind, TRAINING_24, TRAINING_25)
            return []

        with patch.object(booking, "scan_kind", side_effect=scan):
            self.book(clients)
        self.assertEqual(submitted(clients[TEN_METRE]), [TRAINING_25, TRAINING_24])
        self.assertEqual(submitted(clients[UPSTAIRS]), [])

    def test_no_continuous_anchor_slot_allows_upstairs_fallback(self):
        clients = {TEN_METRE: FakeClient(), UPSTAIRS: FakeClient([SUCCESS])}

        def scan(client, cfg, date, kind):
            return [] if kind == TEN_METRE else choices(client, kind, UPSTAIRS_1)

        with patch.object(booking, "scan_kind", side_effect=scan) as scans:
            self.book(clients)
        self.assertEqual({call.args[3] for call in scans.call_args_list}, {TEN_METRE, UPSTAIRS})
        self.assertEqual(submitted(clients[TEN_METRE]), [])
        self.assertEqual(submitted(clients[UPSTAIRS]), [UPSTAIRS_1])

    def test_pair_choice_waits_for_in_flight_second_ground_floor_query(self):
        self.cfg["court_count"] = 2
        scenarios = (
            ([24, 25, 26], TEN_METRE, [25, 26]),
            ([24, 25], TEN_METRE, [24, 25]),
            ([], UPSTAIRS, [1, 2]),
        )
        for downstairs, expected_kind, expected_numbers in scenarios:
            with self.subTest(second_ground_floor_snapshot=downstairs):
                clock = FakeClock()
                clients = {kind: PageClient(page(kind, [])) for kind in (TEN_METRE, UPSTAIRS)}
                lookups = {kind: 0 for kind in clients}
                pending_ground = Future()
                finish_ground = None

                def submit(scan, kind):
                    nonlocal finish_ground
                    lookups[kind] += 1
                    self.assertLessEqual(lookups[kind], 2, "A third scan is unnecessary")
                    if lookups[kind] == 2:
                        if kind == TEN_METRE:
                            clients[kind].html = page(kind, downstairs)
                            finish_ground = lambda: pending_ground.set_result(scan(kind))
                            return pending_ground
                        clients[kind].html = page(kind, [1, 2])
                    ready = Future()
                    ready.set_result(scan(kind))
                    return ready

                def wait(jobs, timeout, return_when):
                    # Latest upstairs result is ready first. Only a wait on the
                    # remaining downstairs request can expose that snapshot.
                    if not any(job.done() for job in jobs):
                        self.assertIn(pending_ground, jobs)
                        self.assertEqual(lookups[UPSTAIRS], 2)
                        clock.advance(.2)
                        finish_ground()
                    return {job for job in jobs if job.done()}, {job for job in jobs if not job.done()}

                pool = Mock()
                pool.submit.side_effect = submit
                with patch.object(booking.concurrent.futures, "ThreadPoolExecutor", return_value=pool), \
                     patch.object(booking.concurrent.futures, "wait", side_effect=wait), \
                     patch.object(booking.time, "monotonic", side_effect=clock), \
                     patch.object(booking.time, "sleep", side_effect=clock.advance):
                    self.book(clients, runtime=self.runtime / str(expected_numbers))

                self.assertTrue(pending_ground.done())
                posts = [(kind, ids(data)) for kind, client in clients.items() for data, _ in client.posts]
                self.assertEqual(posts, [(expected_kind, {COURTS[expected_kind][n] for n in expected_numbers})])

    def test_anchor_post_timeout_does_not_use_a_fallback(self):
        clients = {TEN_METRE: FakeClient([TimeoutError(), SUCCESS]), UPSTAIRS: FakeClient([SUCCESS])}

        def scan(client, cfg, date, kind):
            if kind == TEN_METRE:
                return choices(client, kind, TRAINING_24, TRAINING_25)
            return choices(client, kind, UPSTAIRS_1)

        with patch.object(booking, "scan_kind", side_effect=scan), self.assertRaises(booking.UnknownResult):
            self.book(clients)
        self.assertEqual(submitted(clients[TEN_METRE]), [TRAINING_25])
        self.assertEqual(submitted(clients[UPSTAIRS]), [])
        journal = json.loads(booking.state_path(self.runtime, self.cfg, DATE).read_text(encoding="utf-8"))
        self.assertEqual(journal["state"], "unknown")

    def test_shipped_configuration_only_queries_and_previews_the_two_allowed_types(self):
        cfg = booking.load_config(booking.HERE / "config.json")
        self.assertEqual(cfg["field_type_priority"], [TEN_METRE, UPSTAIRS])
        self.assertEqual(cfg["anchor_court"]["field_type"], TEN_METRE)
        self.assertEqual(cfg["anchor_court"]["field_id"], TRAINING_25)

        def scan(client, cfg, date, kind):
            if kind == TEN_METRE:
                return choices(client, kind, TRAINING_24, TRAINING_25)
            return choices(client, kind, UPSTAIRS_1)

        with patch.object(booking, "load_session", return_value=SESSION), \
             patch.object(booking, "Client", side_effect=lambda *args: FakeClient()), \
             patch.object(booking, "probe"), patch.object(booking, "scan_kind", side_effect=scan) as scans, \
             redirect_stdout(self.output):
            booking.book(cfg, self.runtime, DATE, dry=True)
        self.assertEqual({call.args[3] for call in scans.call_args_list}, {TEN_METRE, UPSTAIRS})
        result = json.loads(self.output.getvalue())
        self.assertFalse(result["submitted"])
        self.assertEqual({row["field_type"] for row in result["candidates"]}, {TEN_METRE, UPSTAIRS})
        self.assertEqual([row["field_id"] for row in result["candidates"] if row["is_anchor"]], [TRAINING_25])
        self.assertEqual(result["candidates"][0]["field_id"], TRAINING_25)
        self.assertFalse((self.runtime / "orders").exists())


if __name__ == "__main__":
    unittest.main()
