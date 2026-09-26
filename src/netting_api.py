"""净额批次 HTTP 路由，与单据接口分离，由 http_api 组合挂载。"""
import re
from typing import Any
from urllib.parse import parse_qs

from .domain import ValidationError


BATCH_RE = re.compile(r"^/api/netting/batches/(\d+)$")
BATCH_ACTION_RE = re.compile(r"^/api/netting/batches/(\d+)/actions/([a-z_]+)$")
BATCH_AUDIT_RE = re.compile(r"^/api/netting/batches/(\d+)/audit$")


def _int_param(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s必须是整数" % name) from exc


def handle_get(handler: Any, service: Any, parsed: Any) -> bool:
    """处理净额批次GET路由；未命中返回False交由其他路由。"""
    if parsed.path == "/api/netting/batches":
        query = parse_qs(parsed.query)
        day = query.get("settlement_day", [None])[0]
        settlement_day = _int_param(day, "settlement_day") if day is not None else None
        limit = _int_param(query.get("limit", ["100"])[0], "limit")
        items = service.list_batches(
            handler._actor(),
            account=query.get("account", [None])[0],
            settlement_day=settlement_day,
            state=query.get("state", [None])[0],
            limit=limit,
        )
        handler._send(200, {"items": items})
        return True
    match = BATCH_RE.match(parsed.path)
    if match:
        handler._send(200, service.get_batch(handler._actor(), int(match.group(1))))
        return True
    match = BATCH_AUDIT_RE.match(parsed.path)
    if match:
        handler._send(200, {"items": service.timeline(handler._actor(), int(match.group(1)))})
        return True
    return False


def handle_post(handler: Any, service: Any, parsed: Any, body: Any) -> bool:
    """处理净额批次POST路由；未命中返回False交由其他路由。"""
    if parsed.path == "/api/netting/batches":
        handler._send(201, service.build_batch(handler._actor(), body))
        return True
    match = BATCH_ACTION_RE.match(parsed.path)
    if match:
        version = body.get("expected_version")
        if not isinstance(version, int):
            raise ValidationError("expected_version必须是整数")
        handler._send(200, service.act(handler._actor(), int(match.group(1)), version, match.group(2), body.get("data", {})))
        return True
    return False
