"""净额批次用例编排：归集、确认、交收与单据退回。"""
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .netting import NettingRules
from .netting_repository import NettingRepository
from .repository import Repository
from .rules import DomainRules


GUARDED_ACTIONS = {"settle", "fail", "reverse", "apply_corporate"}


class NettingService:
    def __init__(self, records: Repository, netting_repository: NettingRepository, rules: NettingRules, domain_rules: DomainRules) -> None:
        self.records = records
        self.netting_repository = netting_repository
        self.rules = rules
        self.domain_rules = domain_rules

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.domain_rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _eligible_records(self, key: Dict[str, Any], include_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        records = self.records.list_for_netting(key["account"], key["currency"], key["settlement_day"], sorted(self.rules.ELIGIBLE_STATES))
        locked = self.netting_repository.active_record_ids()
        include = include_ids or set()
        return [record for record in records if record["id"] not in locked or record["id"] in include]

    def _items_of(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items = []
        for record in records:
            payload = record["payload"]
            items.append({
                "record_id": record["id"],
                "instrument": payload["instrument"],
                "side": payload["side"],
                "signed_quantity": self.rules.signed_quantity(payload),
                "signed_amount": self.rules.signed_amount(payload),
            })
        return items

    @staticmethod
    def _included(batch: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [item for item in batch["items"] if item["status"] == "included"]

    def create_batch(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建净额批次")
        key = self.rules.validate_key(data or {})
        if self.netting_repository.unfinished_batch_id(key["account"], key["currency"], key["settlement_day"]) is not None:
            raise Conflict("该账户/币种/交收日已存在未完结批次")
        records = self._eligible_records(key)
        if not records:
            raise ValidationError("没有可归集的结算指令")
        totals = self.rules.aggregate(records)
        return self.netting_repository.create_batch(key["account"], key["currency"], key["settlement_day"], totals, self._items_of(records), actor.user_id)

    def list_batches(self, actor: Actor, account: Optional[str] = None, settlement_day: Optional[int] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.netting_repository.list_batches(account=account, settlement_day=settlement_day, state=state, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.netting_repository.get_batch(batch_id)

    def batch_timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.netting_repository.batch_events(batch_id)

    def act_batch(self, actor: Actor, batch_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        batch = self.netting_repository.get_batch(batch_id)
        new_state = self.rules.require_transition(batch, action)
        data = dict(data or {})
        if action == "confirm":
            included = self._included(batch)
            if not included:
                raise ValidationError("批次没有组成单据")
            for item in included:
                record = self.records.get(item["record_id"])
                if not self.rules.eligible(record):
                    raise Conflict("批次内单据状态已变化，请刷新后确认")
            return self.netting_repository.mutate_batch(batch_id, int(expected_version), new_state, batch["totals"], actor.user_id, "confirm", {"summary": "净额批次已确认", "item_count": len(included)})
        if action == "refresh":
            key = {"account": batch["account"], "currency": batch["currency"], "settlement_day": batch["settlement_day"]}
            own = {item["record_id"] for item in self._included(batch)}
            records = self._eligible_records(key, include_ids=own)
            return self.netting_repository.replace_items(batch_id, int(expected_version), self.rules.aggregate(records), self._items_of(records), actor.user_id)
        if action == "cancel":
            return self.netting_repository.mutate_batch(batch_id, int(expected_version), new_state, batch["totals"], actor.user_id, "cancel", {"summary": "净额批次已取消"}, release=True)
        if action == "settle":
            included = self._included(batch)
            if not included:
                raise ValidationError("批次没有组成单据")
            result = self.rules.validate_settle(batch["totals"], data)
            updates = []
            for item in included:
                record = self.records.get(item["record_id"])
                if record["state"] != "approved":
                    raise Conflict("批次内存在未复核单据，请先复核")
                payload = dict(record["payload"])
                payload["delivered_quantity"] = self.rules.effective_quantity(payload)
                payload["cash_paid"] = payload["net_amount"]
                payload["settled_via_batch"] = batch_id
                updates.append({"record_id": record["id"], "expected_version": record["version"], "payload": payload})
            totals = dict(batch["totals"])
            totals["cash_paid"] = result["cash_paid"]
            return self.netting_repository.settle_batch(batch_id, int(expected_version), totals, updates, actor.user_id, {"summary": "净额批次交收完成", "input": data})
        if action == "fail":
            reason = text(data, "fail_reason")
            return self.netting_repository.mutate_batch(batch_id, int(expected_version), new_state, batch["totals"], actor.user_id, "fail", {"summary": "净额批次交收失败", "fail_reason": reason}, release=True)
        raise ValidationError("未知操作%s" % action)

    def before_record_action(self, actor: Actor, record: Dict[str, Any], action: str) -> None:
        if action not in GUARDED_ACTIONS:
            return
        lock = self.netting_repository.find_lock(record["id"])
        if lock is None:
            return
        if lock["batch_state"] == "confirmed":
            if action == "settle":
                raise Conflict("单据已纳入已确认净额批次%s，不能单独结清" % lock["batch_id"])
            if action == "apply_corporate":
                raise Conflict("单据已纳入已确认净额批次%s，不能调整公司行动" % lock["batch_id"])
            self._return_to_batch(lock, record["id"], actor.user_id, trigger=action, reopen=True)
        elif lock["batch_state"] == "settled" and action == "reverse":
            self._return_to_batch(lock, record["id"], actor.user_id, trigger=action, reopen=False)

    def _return_to_batch(self, lock: Dict[str, Any], record_id: int, actor_id: str, trigger: str, reopen: bool) -> None:
        batch = self.netting_repository.get_batch(lock["batch_id"])
        remaining = [item for item in batch["items"] if item["status"] == "included" and item["record_id"] != record_id]
        totals = self.rules.aggregate_items(remaining)
        new_state = "open" if reopen else None
        self.netting_repository.return_item(lock["batch_id"], record_id, batch["version"], totals, new_state, actor_id, trigger)
