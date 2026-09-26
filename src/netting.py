"""净额批次归集与计算规则，纯计算不依赖存储。"""
from typing import Any, Dict, Iterable

from .domain import Conflict, ValidationError, choice, integer, number, text


ELIGIBLE_STATES = {"captured", "adjusted", "approved"}
INITIAL_STATE = "open"
BATCH_TRANSITIONS = {
    "confirm": {"open": "confirmed"},
    "refresh": {"open": "open"},
    "cancel": {"open": "cancelled"},
    "settle": {"confirmed": "settled"},
    "fail": {"confirmed": "failed"},
}
CREATE_ROLES = {"settlement_officer"}
ACTION_ROLES = {
    "confirm": {"settlement_officer"},
    "refresh": {"settlement_officer"},
    "cancel": {"settlement_officer"},
    "settle": {"settlement_officer"},
    "fail": {"settlement_officer"},
}
LOCKED_STATES = {"confirmed", "settled"}


class NettingRules:
    INITIAL_STATE = INITIAL_STATE
    ELIGIBLE_STATES = ELIGIBLE_STATES

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_key(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "account": text(data, "account"),
            "currency": choice(data, "currency", ["CNY", "USD", "HKD"]),
            "settlement_day": integer(data, "settlement_day", 0),
        }

    def eligible(self, record: Dict[str, Any]) -> bool:
        return record["state"] in ELIGIBLE_STATES

    def effective_quantity(self, payload: Dict[str, Any]) -> int:
        return int(payload.get("effective_quantity", payload["quantity"]))

    def signed_quantity(self, payload: Dict[str, Any]) -> int:
        quantity = self.effective_quantity(payload)
        return quantity if payload["side"] == "buy" else -quantity

    def signed_amount(self, payload: Dict[str, Any]) -> float:
        amount = float(payload["net_amount"])
        return round(amount if payload["side"] == "buy" else -amount, 2)

    @staticmethod
    def _totals(positions: Dict[str, int], net_amount: float, item_count: int) -> Dict[str, Any]:
        return {
            "positions": {key: positions[key] for key in sorted(positions) if positions[key] != 0},
            "net_amount": round(net_amount, 2),
            "item_count": item_count,
        }

    def aggregate(self, records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        positions: Dict[str, int] = {}
        net_amount = 0.0
        count = 0
        for record in records:
            payload = record["payload"]
            instrument = payload["instrument"]
            positions[instrument] = positions.get(instrument, 0) + self.signed_quantity(payload)
            net_amount += self.signed_amount(payload)
            count += 1
        return self._totals(positions, net_amount, count)

    def aggregate_items(self, items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        positions: Dict[str, int] = {}
        net_amount = 0.0
        count = 0
        for item in items:
            instrument = item["instrument"]
            positions[instrument] = positions.get(instrument, 0) + int(item["signed_quantity"])
            net_amount += float(item["signed_amount"])
            count += 1
        return self._totals(positions, net_amount, count)

    def require_transition(self, batch: Dict[str, Any], action: str) -> str:
        allowed = BATCH_TRANSITIONS.get(action, {}).get(batch["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def validate_settle(self, totals: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data or {})
        positions = data.get("positions")
        if not isinstance(positions, dict) or any(
            not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int)
            for key, value in positions.items()
        ):
            raise ValidationError("positions必须是{证券代码:净数量}对象")
        if positions != totals.get("positions", {}):
            raise ValidationError("净额交收证券数量不匹配")
        cash_paid = number(data, "cash_paid", 0)
        net_amount = float(totals.get("net_amount", 0))
        if net_amount > 0 and cash_paid < net_amount:
            raise ValidationError("净额交收资金不足")
        return {"positions": positions, "cash_paid": cash_paid}
