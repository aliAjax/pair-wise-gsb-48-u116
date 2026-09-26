"""净额批次用例编排：建批、确认、交收、失败退回、冲正退回与废弃。"""
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .netting_repository import NettingRepository
from .netting_rules import NettingRules
from .repository import Repository


class NettingService:
    def __init__(self, repository: Repository, netting_repository: NettingRepository, rules: NettingRules) -> None:
        self.repository = repository
        self.netting_repository = netting_repository
        self.rules = rules

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def record_lock_guard(self, record_id: int, action: str) -> None:
        """单据级守卫：已确认/已交收批次的组成单据不能单独结清、失败或冲正。"""
        if action not in {"settle", "fail", "reverse"}:
            return
        batch = self.netting_repository.active_batch_for_record(record_id)
        if batch and batch["state"] in self.rules.LOCKING_STATES:
            raise Conflict("单据已锁定在净额批次%s（%s），不能单独结清；冲正或交收失败请先退回批次" % (batch["batch_ref"], batch["state"]))

    def build_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "build"):
            raise PermissionDenied("角色无权建立净额批次")
        key = self.rules.validate_key(payload or {})
        records = [record for record in self.repository.list_records(limit=500) if self.rules.is_eligible(record, key)]
        if not records:
            raise ValidationError("该账户/币种/交收日没有可归集的已复核单据")
        totals = self.rules.compute(records)["totals"]
        return self.netting_repository.create_batch(key, totals, [record["id"] for record in records], actor.user_id)

    def list_batches(self, actor: Actor, account: Optional[str] = None, settlement_day: Optional[int] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.netting_repository.list_batches(account=account, settlement_day=settlement_day, state=state, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.netting_repository.get(batch_id)
        records = [self.repository.get(record_id) for record_id in self.netting_repository.item_record_ids(batch_id)]
        computed = self.rules.compute(records)
        return {"batch": batch, "items": computed["lines"]}

    def timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.netting_repository.audit_timeline(batch_id)

    def _item_records(self, batch_id: int) -> List[Dict[str, Any]]:
        return [self.repository.get(record_id) for record_id in self.netting_repository.item_record_ids(batch_id)]

    def act(self, actor: Actor, batch_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        batch = self.netting_repository.get(batch_id)
        new_state = self.rules.require_transition(batch, action)
        data = dict(data or {})
        if action == "confirm":
            records = self._item_records(batch_id)
            totals = self.rules.compute(records)["totals"]
            verify = {record["id"]: "approved" for record in records}
            return self.netting_repository.finalize(
                batch_id, expected_version, new_state, actor.user_id, "confirm",
                {"summary": "净额批次确认，组成单据锁定"}, totals=totals, verify_record_states=verify,
            )
        if action == "settle":
            settlement = self.rules.validate_settlement(batch["totals"], data)
            updates = []
            for record in self._item_records(batch_id):
                payload = dict(record["payload"])
                payload["delivered_quantity"] = self.rules.effective_quantity(payload)
                payload["cash_paid"] = round(float(payload["net_amount"]), 2)
                payload["settled_via_batch"] = batch["batch_ref"]
                updates.append({
                    "record_id": record["id"],
                    "expected_version": record["version"],
                    "required_state": "approved",
                    "state": "settled",
                    "payload": payload,
                    "action": "settle",
                    "details": {"summary": "净额批次交收", "batch": batch["batch_ref"], "from": record["state"], "to": "settled"},
                })
            return self.netting_repository.finalize(
                batch_id, expected_version, new_state, actor.user_id, "settle",
                {"summary": "净额批次交收完成", "settlement": settlement}, record_updates=updates,
            )
        if action == "fail":
            reason = text(data, "fail_reason")
            return self.netting_repository.finalize(
                batch_id, expected_version, new_state, actor.user_id, "fail",
                {"summary": "净额交收失败，退回批次", "fail_reason": reason},
            )
        if action == "reverse":
            reason = text(data, "reverse_reason")
            updates = []
            for record in self._item_records(batch_id):
                payload = dict(record["payload"])
                for key in ("delivered_quantity", "cash_paid", "settled_via_batch"):
                    payload.pop(key, None)
                payload["reverse_reason"] = reason
                updates.append({
                    "record_id": record["id"],
                    "expected_version": record["version"],
                    "required_state": "settled",
                    "state": "approved",
                    "payload": payload,
                    "action": "reverse",
                    "details": {"summary": "批次冲正，退回批次", "batch": batch["batch_ref"], "from": record["state"], "to": "approved"},
                })
            return self.netting_repository.finalize(
                batch_id, expected_version, new_state, actor.user_id, "reverse",
                {"summary": "批次冲正，退回批次", "reverse_reason": reason}, record_updates=updates,
            )
        if action == "discard":
            return self.netting_repository.finalize(
                batch_id, expected_version, new_state, actor.user_id, "discard",
                {"summary": "批次废弃，组成单据解除归集"},
            )
        raise ValidationError("未知批次操作%s" % action)
