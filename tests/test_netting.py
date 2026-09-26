import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.http_api import create_server


TRADER = Actor("trader-1", "trader")
OFFICER = Actor("officer-1", "settlement_officer")
CORPORATE = Actor("corp-1", "corporate_actions")

BASE = {'instrument': 'ACME', 'settlement_account': 'ACC-9', 'side': 'buy', 'quantity': 100, 'price': 10.0, 'fees': 0.0, 'currency': 'CNY', 'settlement_day': 5, 'corporate_action': 'none', 'action_ratio': 1.0}
KEY = {"account": "ACC-9", "currency": "CNY", "settlement_day": 5}


class NettingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, **overrides):
        self.counter += 1
        data = dict(BASE)
        data.update(overrides)
        return self.service.create(TRADER, "TRD-N%03d" % self.counter, data)

    def _approve(self, record):
        return self.service.act(OFFICER, record["id"], record["version"], "approve", {})

    def _create_batch(self):
        return self.service.create_batch(OFFICER, dict(KEY))

    def test_aggregate_offsets_same_instrument(self):
        self._create(side='buy', quantity=1000, price=12.5, fees=18.0)
        self._create(side='sell', quantity=400, price=12.0)
        self._create(instrument='OTHER', side='buy', quantity=100, price=10.0)
        batch = self._create_batch()
        self.assertEqual(batch["state"], "open")
        self.assertEqual(batch["totals"]["positions"], {"ACME": 600, "OTHER": 100})
        self.assertEqual(batch["totals"]["net_amount"], 8718.0)
        self.assertEqual(batch["totals"]["item_count"], 3)
        self.assertEqual(len(batch["items"]), 3)

    def test_corporate_action_adjusted_quantity_participates(self):
        applied = self._create(quantity=1000, price=12.5, corporate_action='split', action_ratio=2.0)
        self.service.act(CORPORATE, applied["id"], applied["version"], "apply_corporate", {})
        self._create(quantity=500, price=12.5, corporate_action='split', action_ratio=2.0)
        batch = self._create_batch()
        self.assertEqual(batch["totals"]["positions"], {"ACME": 2500})

    def test_confirmed_batch_blocks_individual_settle(self):
        record = self._create()
        record = self._approve(record)
        batch = self._create_batch()
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        self.assertEqual(batch["state"], "confirmed")
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "settle", {"delivered_quantity": 100, "cash_paid": 1000.0})
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 100}, "cash_paid": 1000.0})
        self.assertEqual(batch["state"], "settled")
        settled = self.service.get_record(TRADER, record["id"])
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["payload"]["settled_via_batch"], batch["id"])

    def test_record_fail_returns_to_batch_first(self):
        first = self._approve(self._create(quantity=100, price=10.0))
        second = self._approve(self._create(quantity=50, price=10.0))
        batch = self._create_batch()
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        failed = self.service.act(OFFICER, first["id"], first["version"], "fail", {"fail_reason": "券不足"})
        self.assertEqual(failed["state"], "failed")
        batch = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(batch["state"], "open")
        self.assertEqual(batch["totals"]["positions"], {"ACME": 50})
        self.assertEqual(batch["totals"]["net_amount"], 500.0)
        statuses = {item["record_id"]: item["status"] for item in batch["items"]}
        self.assertEqual(statuses[first["id"]], "returned")
        self.assertEqual(statuses[second["id"]], "included")
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 50}, "cash_paid": 500.0})
        self.assertEqual(batch["state"], "settled")

    def test_reverse_from_settled_batch_returns_record(self):
        first = self._approve(self._create(quantity=100, price=10.0))
        self._approve(self._create(quantity=50, price=10.0))
        batch = self._create_batch()
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 150}, "cash_paid": 1500.0})
        record = self.service.get_record(TRADER, first["id"])
        reversed_record = self.service.act(OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "对手方违约"})
        self.assertEqual(reversed_record["state"], "reversed")
        batch = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(batch["state"], "settled")
        self.assertEqual(batch["totals"]["positions"], {"ACME": 50})
        self.assertEqual(batch["totals"]["net_amount"], 500.0)
        statuses = {item["record_id"]: item["status"] for item in batch["items"]}
        self.assertEqual(statuses[first["id"]], "returned")

    def test_persistence_across_restart(self):
        self._approve(self._create(quantity=100, price=10.0))
        self._create(side='sell', quantity=30, price=10.0)
        batch = self._create_batch()
        self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        restarted = build_service(self.db_path)
        batch = restarted.get_batch(OFFICER, batch["id"])
        self.assertEqual(batch["state"], "confirmed")
        self.assertEqual(batch["totals"]["positions"], {"ACME": 70})
        self.assertEqual(batch["totals"]["net_amount"], 700.0)
        self.assertEqual(len([item for item in batch["items"] if item["status"] == "included"]), 2)
        batches = restarted.list_batches(OFFICER, account="ACC-9", settlement_day=5)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["id"], batch["id"])
        events = restarted.batch_timeline(OFFICER, batch["id"])
        self.assertEqual([event["action"] for event in events], ["created", "confirm"])

    def test_batch_settle_validation(self):
        self._approve(self._create(quantity=100, price=10.0))
        batch = self._create_batch()
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        with self.assertRaises(ValidationError):
            self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 99}, "cash_paid": 1000.0})
        with self.assertRaises(ValidationError):
            self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 100}, "cash_paid": 999.0})
        with self.assertRaises(Conflict):
            self.service.act_batch(OFFICER, batch["id"], batch["version"] + 1, "settle", {"positions": {"ACME": 100}, "cash_paid": 1000.0})

    def test_unapproved_records_block_batch_settle(self):
        record = self._create()
        batch = self._create_batch()
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        with self.assertRaises(Conflict):
            self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 100}, "cash_paid": 1000.0})
        self._approve(self.service.get_record(TRADER, record["id"]))
        batch = self.service.get_batch(OFFICER, batch["id"])
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 100}, "cash_paid": 1000.0})
        self.assertEqual(batch["state"], "settled")

    def test_refresh_picks_up_new_records(self):
        self._create(quantity=100, price=10.0)
        batch = self._create_batch()
        self.assertEqual(batch["totals"]["item_count"], 1)
        self._create(quantity=25, price=10.0)
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "refresh", {})
        self.assertEqual(batch["totals"]["item_count"], 2)
        self.assertEqual(batch["totals"]["positions"], {"ACME": 125})

    def test_duplicate_open_batch_conflicts(self):
        self._create()
        self._create_batch()
        with self.assertRaises(Conflict):
            self._create_batch()

    def test_settled_records_not_reaggregated(self):
        self._approve(self._create())
        batch = self._create_batch()
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        self.service.act_batch(OFFICER, batch["id"], batch["version"], "settle", {"positions": {"ACME": 100}, "cash_paid": 1000.0})
        with self.assertRaises(ValidationError):
            self._create_batch()

    def test_batch_permissions(self):
        self._create()
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(TRADER, dict(KEY))
        with self.assertRaises(PermissionDenied):
            self.service.create_batch(Actor("outsider", "outsider"), dict(KEY))
        batch = self._create_batch()
        with self.assertRaises(PermissionDenied):
            self.service.act_batch(TRADER, batch["id"], batch["version"], "confirm", {})

    def test_batch_fail_releases_records(self):
        record = self._approve(self._create())
        batch = self._create_batch()
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "confirm", {})
        batch = self.service.act_batch(OFFICER, batch["id"], batch["version"], "fail", {"fail_reason": "资金不足"})
        self.assertEqual(batch["state"], "failed")
        statuses = {item["record_id"]: item["status"] for item in batch["items"]}
        self.assertEqual(statuses[record["id"]], "returned")
        current = self.service.get_record(TRADER, record["id"])
        settled = self.service.act(OFFICER, current["id"], current["version"], "settle", {"delivered_quantity": 100, "cash_paid": 1000.0})
        self.assertEqual(settled["state"], "settled")


class NettingHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        static_dir = Path(__file__).resolve().parents[1] / "static"
        self.server = create_server("127.0.0.1", 0, self.service, static_dir)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def _request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request(method, path, payload, headers or {})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def test_batch_endpoints(self):
        status, _ = self._request("GET", "/api/net-batches")
        self.assertEqual(status, 403)
        record = self.service.create(TRADER, "TRD-H001", dict(BASE))
        self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        headers = {"X-User-Id": "officer-1", "X-Role": "settlement_officer", "Content-Type": "application/json"}
        status, batch = self._request("POST", "/api/net-batches", dict(KEY), headers)
        self.assertEqual(status, 201)
        self.assertEqual(batch["totals"]["positions"], {"ACME": 100})
        status, batch = self._request("POST", "/api/net-batches/%s/actions/confirm" % batch["id"], {"expected_version": batch["version"]}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(batch["state"], "confirmed")
        status, data = self._request("GET", "/api/net-batches?account=ACC-9&settlement_day=5", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        status, events = self._request("GET", "/api/net-batches/%s/audit" % batch["id"], headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual([event["action"] for event in events["items"]], ["created", "confirm"])
        status, data = self._request("GET", "/api/net-batches?settlement_day=abc", headers=headers)
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
