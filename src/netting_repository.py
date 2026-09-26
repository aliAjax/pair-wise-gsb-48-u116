"""净额批次 SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NettingRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS net_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_day INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    totals TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS net_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES net_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    instrument TEXT NOT NULL,
                    side TEXT NOT NULL,
                    signed_quantity INTEGER NOT NULL,
                    signed_amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'included',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS net_batch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES net_batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_net_batches_key ON net_batches(account, currency, settlement_day);
                CREATE INDEX IF NOT EXISTS idx_net_batches_state ON net_batches(state);
                CREATE INDEX IF NOT EXISTS idx_net_batch_items_record ON net_batch_items(record_id, status);
                CREATE INDEX IF NOT EXISTS idx_net_batch_events_batch ON net_batch_events(batch_id, id);
                """
            )

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["totals"] = json.loads(item["totals"])
        return item

    def _fetch_batch(self, connection: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM net_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("净额批次不存在")
        return row

    @staticmethod
    def _check_version(row: sqlite3.Row, expected_version: int) -> None:
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")

    def create_batch(self, account: str, currency: str, settlement_day: int, totals: Dict[str, Any], items: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM net_batches WHERE account=? AND currency=? AND settlement_day=? AND state IN ('open','confirmed')",
                (account, currency, settlement_day),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                raise Conflict("该账户/币种/交收日已存在未完结批次")
            cursor = connection.execute(
                "INSERT INTO net_batches(account,currency,settlement_day,state,version,totals,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (account, currency, settlement_day, "open", 1, json.dumps(totals, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
            batch_id = int(cursor.lastrowid)
            for item in items:
                connection.execute(
                    "INSERT INTO net_batch_items(batch_id,record_id,instrument,side,signed_quantity,signed_amount,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, item["record_id"], item["instrument"], item["side"], item["signed_quantity"], item["signed_amount"], "included", now),
                )
            connection.execute(
                "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "created", actor_id, 1, json.dumps({"summary": "净额批次已创建", "state": "open", "item_count": len(items)}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_batch(connection, batch_id)
            items = connection.execute("SELECT * FROM net_batch_items WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        batch = self._batch(row)
        batch["items"] = [dict(item) for item in items]
        return batch

    def list_batches(self, account: Optional[str] = None, settlement_day: Optional[int] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if account:
            clauses.append("account=?")
            params.append(account)
        if settlement_day is not None:
            clauses.append("settlement_day=?")
            params.append(int(settlement_day))
        if state:
            clauses.append("state=?")
            params.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM net_batches%s ORDER BY id DESC LIMIT ?" % where, params).fetchall()
        return [self._batch(row) for row in rows]

    def mutate_batch(self, batch_id: int, expected_version: int, state: str, totals: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], release: bool = False) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._fetch_batch(connection, batch_id)
            self._check_version(row, expected_version)
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE net_batches SET state=?,version=?,totals=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(totals, ensure_ascii=False, sort_keys=True), actor_id, now, batch_id),
            )
            if release:
                connection.execute("UPDATE net_batch_items SET status='returned' WHERE batch_id=? AND status='included'", (batch_id,))
            connection.execute(
                "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_batch(batch_id)

    def replace_items(self, batch_id: int, expected_version: int, totals: Dict[str, Any], items: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._fetch_batch(connection, batch_id)
            self._check_version(row, expected_version)
            version = int(expected_version) + 1
            connection.execute("DELETE FROM net_batch_items WHERE batch_id=? AND status='included'", (batch_id,))
            for item in items:
                connection.execute(
                    "INSERT INTO net_batch_items(batch_id,record_id,instrument,side,signed_quantity,signed_amount,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, item["record_id"], item["instrument"], item["side"], item["signed_quantity"], item["signed_amount"], "included", now),
                )
            connection.execute(
                "UPDATE net_batches SET version=?,totals=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(totals, ensure_ascii=False, sort_keys=True), actor_id, now, batch_id),
            )
            connection.execute(
                "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "refresh", actor_id, version, json.dumps({"summary": "批次重新归集", "item_count": len(items)}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_batch(batch_id)

    def return_item(self, batch_id: int, record_id: int, expected_version: int, totals: Dict[str, Any], new_state: Optional[str], actor_id: str, trigger: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._fetch_batch(connection, batch_id)
            self._check_version(row, expected_version)
            item = connection.execute(
                "SELECT id FROM net_batch_items WHERE batch_id=? AND record_id=? AND status='included'",
                (batch_id, record_id),
            ).fetchone()
            if item is None:
                connection.rollback()
                raise NotFound("单据不在批次组成中")
            connection.execute("UPDATE net_batch_items SET status='returned' WHERE id=?", (item["id"],))
            version = int(expected_version) + 1
            state = new_state or row["state"]
            connection.execute(
                "UPDATE net_batches SET state=?,version=?,totals=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(totals, ensure_ascii=False, sort_keys=True), actor_id, now, batch_id),
            )
            connection.execute(
                "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "record_returned", actor_id, version, json.dumps({"summary": "单据退回批次", "record_id": record_id, "trigger": trigger}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_batch(batch_id)

    def settle_batch(self, batch_id: int, expected_version: int, totals: Dict[str, Any], record_updates: List[Dict[str, Any]], actor_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._fetch_batch(connection, batch_id)
            self._check_version(row, expected_version)
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE net_batches SET state='settled',version=?,totals=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(totals, ensure_ascii=False, sort_keys=True), actor_id, now, batch_id),
            )
            connection.execute(
                "INSERT INTO net_batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "settle", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            for update in record_updates:
                record = connection.execute("SELECT version FROM records WHERE id=?", (update["record_id"],)).fetchone()
                if record is None or int(record["version"]) != int(update["expected_version"]):
                    connection.rollback()
                    raise Conflict("批次内单据版本冲突，请刷新后重试")
                record_version = int(update["expected_version"]) + 1
                connection.execute(
                    "UPDATE records SET state='settled',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (record_version, json.dumps(update["payload"], ensure_ascii=False, sort_keys=True), actor_id, now, update["record_id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (update["record_id"], "settle", actor_id, record_version, json.dumps({"summary": "净额批次交收完成", "batch_id": batch_id, "via_batch": True}, ensure_ascii=False, sort_keys=True), now),
                )
            connection.commit()
        return self.get_batch(batch_id)

    def active_record_ids(self) -> set:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT i.record_id AS record_id FROM net_batch_items i JOIN net_batches b ON b.id=i.batch_id WHERE i.status='included' AND b.state IN ('open','confirmed')"
            ).fetchall()
        return {int(row["record_id"]) for row in rows}

    def unfinished_batch_id(self, account: str, currency: str, settlement_day: int) -> Optional[int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM net_batches WHERE account=? AND currency=? AND settlement_day=? AND state IN ('open','confirmed')",
                (account, currency, settlement_day),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def find_lock(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT b.id AS batch_id, b.state AS batch_state, b.version AS batch_version FROM net_batch_items i JOIN net_batches b ON b.id=i.batch_id WHERE i.record_id=? AND i.status='included' AND b.state IN ('confirmed','settled') ORDER BY b.id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def batch_events(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            self._fetch_batch(connection, batch_id)
            rows = connection.execute("SELECT * FROM net_batch_events WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
