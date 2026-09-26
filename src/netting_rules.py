"""净额批次归集规则：按账户、币种、交收日归集，买正卖负，同一证券先相抵。

纯规则模块，不依赖存储与接口，可独立测试。
"""
from typing import Any, Dict, List

from .domain import Conflict, ValidationError, choice, integer, number, text


ELIGIBLE_RECORD_STATE = "approved"
LOCKING_STATES = {"confirmed", "settled"}
TRANSITIONS = {
    "confirm": {"open": "confirmed", "returned": "confirmed"},
    "settle": {"confirmed": "settled"},
    "fail": {"confirmed": "returned"},
    "reverse": {"settled": "returned"},
    "discard": {"open": "discarded", "returned": "discarded"},
}
ACTION_ROLES = {
    "build": {"settlement_officer"},
    "confirm": {"settlement_officer"},
    "settle": {"settlement_officer"},
    "fail": {"settlement_officer"},
    "reverse": {"settlement_officer"},
    "discard": {"settlement_officer"},
}
KNOWN_ROLES = {"trader", "corporate_actions", "settlement_officer"}
CURRENCIES = ["CNY", "USD", "HKD"]


class NettingRules:
    INITIAL_STATE = "open"
    TRANSITIONS = TRANSITIONS
    LOCKING_STATES = LOCKING_STATES

    def known_role(self, role: str) -> bool:
        return role == "admin" or role in KNOWN_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_key(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        return {
            "settlement_account": text(data, "settlement_account"),
            "currency": choice(data, "currency", CURRENCIES),
            "settlement_day": integer(data, "settlement_day", 0),
        }

    def require_transition(self, batch: Dict[str, Any], action: str) -> str:
        if action not in TRANSITIONS:
            raise ValidationError("未知批次操作%s" % action)
        allowed = TRANSITIONS[action].get(batch["state"])
        if allowed is None:
            raise Conflict("批次当前状态不允许执行%s" % action)
        return allowed

    @staticmethod
    def effective_quantity(payload: Dict[str, Any]) -> int:
        """已应用公司行动的按调整后数量参与净额。"""
        if payload.get("corporate_applied"):
            return int(payload.get("effective_quantity", payload["quantity"]))
        return int(payload["quantity"])

    def is_eligible(self, record: Dict[str, Any], key: Dict[str, Any]) -> bool:
        if record["state"] != ELIGIBLE_RECORD_STATE:
            return False
        payload = record["payload"]
        return (
            payload.get("settlement_account") == key["settlement_account"]
            and payload.get("currency") == key["currency"]
            and payload.get("settlement_day") == key["settlement_day"]
        )

    def compute(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """买入为正、卖出为负，同一证券先相抵；现金按净额收付。"""
        instruments: Dict[str, Dict[str, Any]] = {}
        lines: List[Dict[str, Any]] = []
        net_cash = 0.0
        for record in records:
            payload = record["payload"]
            side = payload["side"]
            sign = 1 if side == "buy" else -1
            quantity = self.effective_quantity(payload)
            amount = round(float(payload["net_amount"]), 2)
            name = payload["instrument"]
            bucket = instruments.setdefault(name, {"instrument": name, "buy_quantity": 0, "sell_quantity": 0, "net_quantity": 0})
            if side == "buy":
                bucket["buy_quantity"] += quantity
            else:
                bucket["sell_quantity"] += quantity
            bucket["net_quantity"] += sign * quantity
            signed_cash = -amount if side == "buy" else amount
            net_cash += signed_cash
            lines.append({
                "record_id": record["id"],
                "reference": record["reference"],
                "state": record["state"],
                "instrument": name,
                "side": side,
                "quantity": quantity,
                "signed_quantity": sign * quantity,
                "net_amount": amount,
                "signed_cash": round(signed_cash, 2),
                "corporate_applied": bool(payload.get("corporate_applied")),
            })
        positions = []
        for name in sorted(instruments):
            bucket = instruments[name]
            net = bucket["net_quantity"]
            bucket["direction"] = "receive" if net > 0 else ("deliver" if net < 0 else "flat")
            positions.append(bucket)
        net_cash = round(net_cash, 2)
        totals = {
            "instruments": positions,
            "net_cash": net_cash,
            "cash_direction": "receive" if net_cash > 0 else ("pay" if net_cash < 0 else "flat"),
            "record_count": len(records),
        }
        return {"totals": totals, "lines": lines}

    def validate_settlement(self, totals: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """批次交收完整性：各证券净额数量必须足额，现金不得低于净额。"""
        delivered = data.get("delivered")
        if not isinstance(delivered, dict):
            raise ValidationError("delivered必须是对象")
        checked = {}
        for position in totals["instruments"]:
            name = position["instrument"]
            value = delivered.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError("证券%s的净交收数量必须是数字" % name)
            if int(value) != abs(int(position["net_quantity"])):
                raise ValidationError("证券%s净交收数量不匹配" % name)
            checked[name] = int(value)
        cash = number(data, "cash_settled", 0)
        due = abs(round(float(totals["net_cash"]), 2))
        if round(cash, 2) + 1e-9 < due:
            raise ValidationError("净额交收资金不足")
        return {"delivered": checked, "cash_settled": round(cash, 2)}
