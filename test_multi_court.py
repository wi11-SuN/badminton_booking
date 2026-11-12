"""Offline scenarios for booking several courts in one order on the same page."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import book_badminton as booking
from test_booking import CONFLICT, DATE, SESSION, SUCCESS, FakeClient, config


TEN, UP = "2434", "842"
PAIR = {"20009846", "20009847"}
COURTS = {TEN: {24: "20009845", 25: "20009846", 26: "20009847", 27: "20009848"},
          UP: {1: "20003157", 2: "20003158", 3: "20003159", 4: "20003160"}}


def page(kind, fields, *, max_fields=2, total_units=8, missing=()):
    """Two one-hour segments per court. The identifiers belong to this day only."""
    day = DATE.strftime("%Y%m%d")
    html = [
        f'<input id="date" value="{day}"><dd date="{day}"></dd>',
        f'<input id="fieldType" value="{kind}">',
        '<input id="venueId" value="test-venue"><input id="serviceId" value="1002">',
        f'<input name="field" value="{kind}" data-book-hour="1">',
        f'<div id="field-type-{kind}" data-max-hour="8" data-total-hour="{total_units}" '
        f'data-min-serial-num="2" data-pieces-unit="0" data-max-field="{max_fields}"></div>',
    ]
    for number in fields:
        field_id = COURTS[kind][number]
        for start in (38, 40):
            if (number, start) in missing:
                continue
            html.append(f'<i field-id="{field_id}" field-num="{number}" state="0" '
                        f'venue-name="场{number}" start-time="{start}" end-time="{start+2}" '
                        f'field-segment-id="{day}-{field_id}-{start}" price="50" listing-price="60"></i>')
    return "".join(html)


class PageClient(FakeClient):
    def __init__(self, html, responses=(SUCCESS,)):
        super().__init__(responses)
        self.html = html

    def request(self, path, payload=None, referer=None):
        if payload is None:
            return 200, self.html
        if path != booking.ROOT + "/court/commit":
            raise AssertionError(f"Unexpected write to {path}")
        return super().request(path, payload, referer)


def ids(payload):
    return {item["fieldId"] for item in payload["fieldInfoList"]}


class ContendedPageClient(PageClient):
    """The snapshot stays available, but some courts are occupied before commit."""
    def __init__(self, html, occupied):
        super().__init__(html, responses=())
        self.occupied = set(occupied)

    def request(self, path, payload=None, referer=None):
        if payload is not None:
            self.responses.append(CONFLICT if ids(payload) & self.occupied else SUCCESS)
        return super().request(path, payload, referer)


class MultiCourtTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        for stub in (patch.object(booking, "notify"),
                     patch.object(booking.Client, "request", side_effect=AssertionError("No live requests"))):
            stub.start()
            self.addCleanup(stub.stop)

    def run_book(self, cfg, clients, *, runtime=None):
        with redirect_stdout(io.StringIO()):
            return booking.book(cfg, self.runtime if runtime is None else runtime, DATE,
                                prepared={"session": SESSION, "rules": {}, "clients": clients})

    def test_cli_count_and_legacy_config_default(self):
        old = json.loads((booking.HERE / "config.json").read_text(encoding="utf-8-sig"))
        old.pop("court_count", None)
        path = self.runtime / "config.json"
        path.write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(booking.load_config(path)["court_count"], 1)
        with patch.object(booking, "show_plan") as show:
            self.assertEqual(booking.main(["plan", "--config", str(path), "--court-count", "2"]), 0)
        self.assertEqual(show.call_args.args[0]["court_count"], 2)

    def test_preferred_pair_is_a_single_order_with_four_target_day_segments(self):
        cfg = config((TEN, UP), court_count=2)
        clients = {TEN: PageClient(page(TEN, [24, 25, 26])), UP: PageClient(page(UP, [1, 2]))}
        result = self.run_book(cfg, clients)
        self.assertEqual(len(clients[TEN].posts), 1)
        self.assertFalse(clients[UP].posts)
        payload = clients[TEN].posts[0][0]
        self.assertEqual(ids(payload), PAIR)
        self.assertEqual(len(payload["fieldInfoList"]), 2)
        segments = []
        for item in payload["fieldInfoList"]:
            self.assertEqual((item["startSegment"], item["endSegment"]), ("38", "42"))
            self.assertEqual((item["price"], item["listingPrice"]), (100, 120))
            segments.extend(f'20261111-{item["fieldId"]}-{start}' for start in (38, 40))
            self.assertEqual(item["fieldSegmentId"], segments[-1])
        self.assertEqual(payload["fieldInfo"].split(","), segments)
        self.assertEqual(result["preview_yuan"], 200)
        self.assertEqual(result["trade_id"], "offline-order")
        self.assertEqual({c["field_id"] for c in result["courts"]}, PAIR)
        saved = json.loads(booking.state_path(self.runtime, cfg, DATE).read_text(encoding="utf-8"))
        self.assertEqual(saved["court_count"], 2)
        self.assertEqual(saved["attempts"][0]["courts"], result["courts"])

    def test_dry_run_shows_both_courts_and_the_combined_price(self):
        cfg = config((TEN,), court_count=2)
        client = PageClient(page(TEN, [24, 25, 26]))
        output = io.StringIO()
        with patch.object(booking, "load_session", return_value=SESSION), patch.object(booking, "probe"), \
             patch.object(booking, "Client", return_value=client), redirect_stdout(output):
            booking.book(cfg, self.runtime, DATE, dry=True)
        preview = json.loads(output.getvalue())
        first = preview["candidates"][0]
        self.assertEqual(preview["court_count"], 2)
        self.assertTrue(first["is_anchor"])
        self.assertEqual({c["field_id"] for c in first["courts"]}, PAIR)
        self.assertEqual(first["preview_yuan"], 200)
        self.assertFalse(client.posts)

    def test_dry_run_previews_diverse_pairs_without_exceeding_each_type_limit(self):
        cfg = config((TEN, UP), court_count=2)
        clients = {TEN: PageClient(page(TEN, [24, 25, 26, 27])),
                   UP: PageClient(page(UP, [1, 2, 3, 4]))}
        output = io.StringIO()
        with patch.object(booking, "load_session", return_value=SESSION), patch.object(booking, "probe"), \
             patch.object(booking, "Client", side_effect=list(clients.values())), redirect_stdout(output):
            booking.book(cfg, self.runtime, DATE, dry=True)
        preview = json.loads(output.getvalue())
        self.assertFalse(preview["submitted"])
        for kind in (TEN, UP):
            plans = [plan for plan in preview["candidates"] if plan["field_type"] == kind]
            self.assertEqual(len(plans), cfg["max_commit_attempts"])
            first, second = [{court["field_id"] for court in plan["courts"]} for plan in plans[:2]]
            self.assertTrue(first.isdisjoint(second))
            if kind == TEN:
                self.assertEqual(first, PAIR)
                self.assertEqual(second, {COURTS[TEN][24], COURTS[TEN][27]})
                self.assertTrue(plans[0]["is_anchor"])
            self.assertFalse(clients[kind].posts)
        self.assertFalse((self.runtime / "orders").exists())

    def test_one_incomplete_preferred_court_is_replaced_by_a_full_same_type_court(self):
        client = PageClient(page(TEN, [24, 25, 26, 27], missing=[(26, 40)]))
        self.run_book(config((TEN,), court_count=2), {TEN: client})
        picked = ids(client.posts[0][0])
        self.assertEqual(len(picked), 2)
        self.assertIn(COURTS[TEN][25], picked)
        self.assertNotIn(COURTS[TEN][26], picked)

    def test_pair_fallback_can_use_two_upstairs_courts(self):
        clients = {TEN: PageClient(page(TEN, [25])), UP: PageClient(page(UP, [1, 2]))}
        self.run_book(config((TEN, UP), court_count=2), clients)
        self.assertFalse(clients[TEN].posts)
        self.assertEqual(ids(clients[UP].posts[0][0]), {COURTS[UP][1], COURTS[UP][2]})

    def test_contended_preferred_pair_uses_disjoint_backup_within_two_attempts(self):
        cfg = config((TEN,), court_count=2)
        backup = {COURTS[TEN][24], COURTS[TEN][27]}
        for occupied in (25, 26):
            with self.subTest(occupied=occupied):
                client = ContendedPageClient(page(TEN, [24, 25, 26, 27]), [COURTS[TEN][occupied]])
                result = self.run_book(cfg, {TEN: client}, runtime=self.runtime / str(occupied))
                attempts = [ids(payload) for payload, _ in client.posts]
                self.assertEqual(result["trade_id"], "offline-order")
                self.assertEqual(attempts, [PAIR, backup])

    def test_three_available_courts_can_avoid_occupied_training_25_within_limit(self):
        cfg = config((TEN,), court_count=2)
        client = ContendedPageClient(page(TEN, [24, 25, 26]), [COURTS[TEN][25]])
        result = self.run_book(cfg, {TEN: client})
        attempts = [ids(payload) for payload, _ in client.posts]
        self.assertEqual(result["trade_id"], "offline-order")
        self.assertEqual(attempts[0], PAIR)
        self.assertEqual(attempts[-1], {COURTS[TEN][24], COURTS[TEN][26]})
        self.assertLessEqual(len(attempts), 3)
        self.assertEqual(len({frozenset(pair) for pair in attempts}), len(attempts))

    def test_only_training_25_in_snapshot_is_preferred_then_can_be_replaced_after_conflict(self):
        cfg = config((TEN,), court_count=2)
        client = ContendedPageClient(page(TEN, [24, 25, 27]), [COURTS[TEN][25]])
        result = self.run_book(cfg, {TEN: client})
        attempts = [ids(payload) for payload, _ in client.posts]
        self.assertEqual(result["trade_id"], "offline-order")
        self.assertIn(COURTS[TEN][25], attempts[0])
        self.assertEqual(attempts[-1], {COURTS[TEN][24], COURTS[TEN][27]})
        self.assertLessEqual(len(attempts), 3)
        self.assertEqual(len({frozenset(pair) for pair in attempts}), len(attempts))

    def test_upstairs_pair_uses_disjoint_backup_after_either_first_court_is_occupied(self):
        cfg = config((TEN, UP), court_count=2)
        first = {COURTS[UP][1], COURTS[UP][2]}
        backup = {COURTS[UP][3], COURTS[UP][4]}
        for occupied in (1, 2):
            with self.subTest(occupied=occupied):
                upstairs = ContendedPageClient(page(UP, [1, 2, 3, 4]), [COURTS[UP][occupied]])
                clients = {TEN: PageClient(page(TEN, [25])), UP: upstairs}
                result = self.run_book(cfg, clients, runtime=self.runtime / str(occupied))
                self.assertEqual(result["trade_id"], "offline-order")
                self.assertFalse(clients[TEN].posts)
                self.assertEqual([ids(payload) for payload, _ in upstairs.posts], [first, backup])

    def test_more_pair_candidates_do_not_exceed_three_commit_attempts(self):
        cfg = config((TEN,), court_count=2)
        client = ContendedPageClient(page(TEN, [24, 25, 26, 27]), COURTS[TEN].values())
        with self.assertRaisesRegex(booking.BookingError, "提交次数上限"):
            self.run_book(cfg, {TEN: client})
        attempts = [ids(payload) for payload, _ in client.posts]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len({frozenset(pair) for pair in attempts}), 3)

    def test_one_available_court_on_each_page_cannot_be_combined(self):
        cfg = config((TEN, UP), court_count=2, max_wait_seconds=.1)
        clients = {TEN: PageClient(page(TEN, [25])), UP: PageClient(page(UP, [1]))}
        with self.assertRaises(booking.NotAvailable):
            self.run_book(cfg, clients)
        self.assertFalse(any(client.posts for client in clients.values()))

    def test_three_court_order_includes_both_required_courts_and_one_distinct_extra(self):
        client = PageClient(page(TEN, [24, 25, 26, 27], max_fields=3, total_units=12))
        self.run_book(config((TEN,), court_count=3), {TEN: client})
        payload = client.posts[0][0]
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(len(payload["fieldInfoList"]), 3)
        self.assertEqual(len(ids(payload)), 3)
        self.assertTrue(PAIR <= ids(payload))
        self.assertEqual(len(set(payload["fieldInfo"].split(","))), 6)

    def test_missing_mandatory_court_prevents_three_court_booking(self):
        cfg = config((TEN,), court_count=3, max_wait_seconds=.1)
        client = PageClient(page(TEN, [24, 25, 26, 27], missing=[(25, 38)], max_fields=4, total_units=16))
        with self.assertRaises(booking.NotAvailable):
            self.run_book(cfg, {TEN: client})
        self.assertFalse(client.posts)
        self.assertFalse((self.runtime / "orders").exists())

    def test_live_court_and_total_time_limits_prevent_submission(self):
        cfg = config((TEN,), court_count=3, max_wait_seconds=.1)
        for limit, hours, reason in ((2, 12, "最多允许 2 块"), (3, 8, "超过页面上限 4")):
            with self.subTest(limit=limit, hours=hours):
                client = PageClient(page(TEN, [24, 25, 26], max_fields=limit, total_units=hours))
                with self.assertRaisesRegex(booking.BookingError, reason):
                    self.run_book(cfg, {TEN: client})
                self.assertFalse(client.posts)
                self.assertFalse((self.runtime / "orders").exists())

    def test_rejected_pair_changes_combination_without_leaving_a_partial_order(self):
        cfg = config((TEN,), court_count=2)
        client = PageClient(page(TEN, [24, 25, 26]), [CONFLICT, SUCCESS])
        self.run_book(cfg, {TEN: client})
        self.assertEqual(len(client.posts), 2)
        first, second = [ids(payload) for payload, _ in client.posts]
        self.assertEqual(first, PAIR)
        self.assertNotEqual(first, second)
        self.assertEqual(len(second), 2)
        self.assertEqual(len(first & second), 1)

    def test_multi_order_timeout_stops_without_retrying_a_different_combination(self):
        cfg = config((TEN,), court_count=2)
        client = PageClient(page(TEN, [24, 25, 26]), [TimeoutError("offline"), SUCCESS])
        with self.assertRaises(booking.UnknownResult):
            self.run_book(cfg, {TEN: client})
        self.assertEqual(len(client.posts), 1)
        journal = json.loads(booking.state_path(self.runtime, cfg, DATE).read_text(encoding="utf-8"))
        self.assertEqual(journal["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
