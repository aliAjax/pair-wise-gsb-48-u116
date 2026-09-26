import tempfile
import unittest
from pathlib import Path

from app import build_services
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


TRADER = Actor("creator", "trader")
OFFICER = Actor("officer", "settlement_officer")
CORPORATE = Actor("corp", "corporate_actions")


def order(instrument="ACME", side="buy", quantity=100, price=10.0, fees=0.0, account="ACC-1", currency="CNY", day=2, corporate="none", ratio=1.0):
    return {
        "instrument": instrument,
        "settlement_account": account,
        "side": side,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "currency": currency,
        "settlement_day": day,
        "corporate_action": corporate,
        "action_ratio": ratio,
    }


class NettingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        services = build_services(self.db)
        self.records = services.records
        self.netting = services.netting

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, reference, data, corporate=False):
        record = self.records.create(TRADER, reference, data)
        if corporate:
            record = self.records.act(CORPORATE, record["id"], record["version"], "apply_corporate", {})
        return self.records.act(OFFICER, record["id"], record["version"], "approve", {})

    def build(self, account="ACC-1", currency="CNY", day=2):
        return self.netting.build_batch(OFFICER, {"settlement_account": account, "currency": currency, "settlement_day": day})

    def act(self, batch, action, data=None):
        return self.netting.act(OFFICER, batch["id"], batch["version"], action, data or {})

    def test_netting_offsets_same_instrument(self):
        self.approve("TRD-1", order(side="buy", quantity=1000, price=10.0))
        self.approve("TRD-2", order(side="sell", quantity=400, price=10.0))
        self.approve("TRD-3", order(instrument="BETA", side="buy", quantity=50, price=20.0))
        batch = self.build()
        totals = batch["totals"]
        positions = {p["instrument"]: p for p in totals["instruments"]}
        self.assertEqual(positions["ACME"]["buy_quantity"], 1000)
        self.assertEqual(positions["ACME"]["sell_quantity"], 400)
        self.assertEqual(positions["ACME"]["net_quantity"], 600)
        self.assertEqual(positions["ACME"]["direction"], "receive")
        self.assertEqual(positions["BETA"]["net_quantity"], 50)
        # 现金：买入付10000与1000，卖出收4000，净付7000
        self.assertEqual(totals["net_cash"], -7000.0)
        self.assertEqual(totals["cash_direction"], "pay")
        self.assertEqual(totals["record_count"], 3)

    def test_grouping_by_account_and_day(self):
        self.approve("TRD-1", order(account="ACC-1", day=2))
        self.approve("TRD-2", order(account="ACC-2", day=2, price=11.0))
        self.approve("TRD-3", order(account="ACC-1", day=3, price=12.0))
        batch = self.build(account="ACC-1", day=2)
        self.assertEqual(batch["totals"]["record_count"], 1)
        self.assertEqual(batch["settlement_account"], "ACC-1")
        self.assertEqual(batch["settlement_day"], 2)

    def test_adjusted_quantity_participates_after_corporate_action(self):
        self.approve("TRD-1", order(side="buy", quantity=1000, price=10.0, corporate="split", ratio=2.0), corporate=True)
        batch = self.build()
        position = batch["totals"]["instruments"][0]
        self.assertEqual(position["net_quantity"], 2000)
        detail = self.netting.get_batch(OFFICER, batch["id"])
        self.assertTrue(detail["items"][0]["corporate_applied"])
        self.assertEqual(detail["items"][0]["quantity"], 2000)

    def test_confirmed_batch_locks_member_records(self):
        record = self.approve("TRD-1", order())
        batch = self.act(self.build(), "confirm")
        self.assertEqual(batch["state"], "confirmed")
        with self.assertRaises(Conflict):
            self.records.act(OFFICER, record["id"], record["version"], "settle", {"delivered_quantity": 100, "cash_paid": 1000.0})

    def test_settle_marks_records_settled(self):
        first = self.approve("TRD-1", order(side="buy", quantity=1000, price=10.0))
        second = self.approve("TRD-2", order(side="sell", quantity=400, price=10.0))
        batch = self.act(self.build(), "confirm")
        with self.assertRaises(ValidationError):
            self.act(batch, "settle", {"delivered": {"ACME": 600}, "cash_settled": 100.0})
        batch = self.act(batch, "settle", {"delivered": {"ACME": 600}, "cash_settled": 7000.0})
        self.assertEqual(batch["state"], "settled")
        for record_id in (first["id"], second["id"]):
            record = self.records.get_record(TRADER, record_id)
            self.assertEqual(record["state"], "settled")
            self.assertEqual(record["payload"]["settled_via_batch"], batch["batch_ref"])
        # 已交收批次中的单据不能单独冲正，须先退回批次
        record = self.records.get_record(TRADER, first["id"])
        with self.assertRaises(Conflict):
            self.records.act(OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "差错"})

    def test_fail_returns_batch_then_reconfirm(self):
        self.approve("TRD-1", order())
        batch = self.act(self.build(), "confirm")
        batch = self.act(batch, "fail", {"fail_reason": "对手方违约"})
        self.assertEqual(batch["state"], "returned")
        detail = self.netting.get_batch(OFFICER, batch["id"])
        self.assertEqual(len(detail["items"]), 1)
        record = self.records.get_record(TRADER, detail["items"][0]["record_id"])
        self.assertEqual(record["state"], "approved")
        batch = self.act(batch, "confirm")
        batch = self.act(batch, "settle", {"delivered": {"ACME": 100}, "cash_settled": 1000.0})
        self.assertEqual(batch["state"], "settled")

    def test_reverse_returns_batch_and_records(self):
        self.approve("TRD-1", order())
        batch = self.act(self.build(), "confirm")
        batch = self.act(batch, "settle", {"delivered": {"ACME": 100}, "cash_settled": 1000.0})
        batch = self.act(batch, "reverse", {"reverse_reason": "交收差错"})
        self.assertEqual(batch["state"], "returned")
        detail = self.netting.get_batch(OFFICER, batch["id"])
        record = self.records.get_record(TRADER, detail["items"][0]["record_id"])
        self.assertEqual(record["state"], "approved")
        self.assertNotIn("settled_via_batch", record["payload"])

    def test_restart_still_reconcilable(self):
        self.approve("TRD-1", order(side="buy", quantity=1000, price=10.0))
        self.approve("TRD-2", order(side="sell", quantity=400, price=10.0))
        batch = self.act(self.build(), "confirm")
        reopened = build_services(self.db)
        batches = reopened.netting.list_batches(OFFICER, account="ACC-1", settlement_day=2)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["state"], "confirmed")
        self.assertEqual(batches[0]["totals"]["net_cash"], -6000.0)
        detail = reopened.netting.get_batch(OFFICER, batch["id"])
        self.assertEqual(len(detail["items"]), 2)
        timeline = reopened.netting.timeline(OFFICER, batch["id"])
        self.assertEqual([event["action"] for event in timeline], ["build", "confirm"])

    def test_duplicate_active_batch_rejected(self):
        self.approve("TRD-1", order())
        self.build()
        with self.assertRaises(Conflict):
            self.build()

    def test_discard_releases_records(self):
        record = self.approve("TRD-1", order())
        batch = self.act(self.build(), "discard")
        self.assertEqual(batch["state"], "discarded")
        record = self.records.get_record(TRADER, record["id"])
        record = self.records.act(OFFICER, record["id"], record["version"], "settle", {"delivered_quantity": 100, "cash_paid": 1000.0})
        self.assertEqual(record["state"], "settled")

    def test_permission_and_empty_batch(self):
        with self.assertRaises(PermissionDenied):
            self.netting.build_batch(TRADER, {"settlement_account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        with self.assertRaises(ValidationError):
            self.netting.build_batch(OFFICER, {"settlement_account": "ACC-9", "currency": "CNY", "settlement_day": 2})
