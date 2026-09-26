"""净额批次 SQLite 存储：批次、组成单据与批次审计，独立于单据存储模块。

批次交收/冲正需要在同一事务内更新批次与组成单据，``finalize`` 为此
直接写入 records/audit_events 表（同一数据库文件），保证重启后两边一致可核对。
"""
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
                CREATE TABLE IF NOT EXISTS netting_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_ref TEXT NOT NULL UNIQUE,
                    settlement_account TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS netting_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES netting_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    UNIQUE(batch_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS netting_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES netting_batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_netting_active_key
                    ON netting_batches(settlement_account, currency, settlement_day) WHERE state != 'discarded';
                CREATE INDEX IF NOT EXISTS idx_netting_items_record ON netting_batch_items(record_id);
                CREATE INDEX IF NOT EXISTS idx_netting_audit_batch ON netting_audit_events(batch_id, id);
                """
            )

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["totals"] = json.loads(item["totals"])
        return item

    def create_batch(self, key: Dict[str, Any], totals: Dict[str, Any], record_ids: List[int], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM netting_batches WHERE settlement_account=? AND currency=? AND settlement_day=? AND state != 'discarded'",
                (key["settlement_account"], key["currency"], key["settlement_day"]),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                raise Conflict("该账户/币种/交收日已存在未关闭的净额批次")
            cursor = connection.execute(
                "INSERT INTO netting_batches(batch_ref,settlement_account,currency,settlement_day,state,version,totals,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("", key["settlement_account"], key["currency"], key["settlement_day"], "open", 1, json.dumps(totals, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
            batch_id = int(cursor.lastrowid)
            connection.execute("UPDATE netting_batches SET batch_ref=? WHERE id=?", ("NB-%06d" % batch_id, batch_id))
            for record_id in record_ids:
                connection.execute("INSERT INTO netting_batch_items(batch_id,record_id) VALUES(?,?)", (batch_id, int(record_id)))
            connection.execute(
                "INSERT INTO netting_audit_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "build", actor_id, 1, json.dumps({"summary": "净额批次建立", "records": len(record_ids)}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM netting_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch(row)

    def get(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM netting_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("净额批次不存在")
        return self._batch(row)

    def item_record_ids(self, batch_id: int) -> List[int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT record_id FROM netting_batch_items WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return [int(row["record_id"]) for row in rows]

    def list_batches(self, account: Optional[str] = None, settlement_day: Optional[int] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM netting_batches WHERE 1=1"
        params: List[Any] = []
        if account:
            sql += " AND settlement_account=?"
            params.append(account)
        if settlement_day is not None:
            sql += " AND settlement_day=?"
            params.append(int(settlement_day))
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._batch(row) for row in rows]

    def active_batch_for_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT b.* FROM netting_batches b JOIN netting_batch_items i ON i.batch_id=b.id WHERE i.record_id=? AND b.state != 'discarded' ORDER BY b.id DESC LIMIT 1",
                (int(record_id),),
            ).fetchone()
        return self._batch(row) if row is not None else None

    def finalize(
        self,
        batch_id: int,
        expected_version: int,
        new_state: str,
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        totals: Optional[Dict[str, Any]] = None,
        record_updates: List[Dict[str, Any]] = (),
        verify_record_states: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Any]:
        """批次状态迁移；必要时同事务校验/更新组成单据，保证批次与单据一致。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM netting_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("净额批次不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            for record_id, required in (verify_record_states or {}).items():
                rrow = connection.execute("SELECT state FROM records WHERE id=?", (int(record_id),)).fetchone()
                if rrow is None:
                    connection.rollback()
                    raise NotFound("记录不存在")
                if rrow["state"] != required:
                    connection.rollback()
                    raise Conflict("批次组成单据状态已变化，请废弃后重建")
            for update in record_updates:
                rrow = connection.execute("SELECT version, state FROM records WHERE id=?", (update["record_id"],)).fetchone()
                if rrow is None:
                    connection.rollback()
                    raise NotFound("记录不存在")
                required = update.get("required_state")
                if required and rrow["state"] != required:
                    connection.rollback()
                    raise Conflict("批次组成单据状态已变化，请废弃后重建")
                if int(rrow["version"]) != int(update["expected_version"]):
                    connection.rollback()
                    raise Conflict("单据版本冲突，请刷新后重试")
                record_version = int(rrow["version"]) + 1
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (update["state"], record_version, json.dumps(update["payload"], ensure_ascii=False, sort_keys=True), actor_id, now, update["record_id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (update["record_id"], update["action"], actor_id, record_version, json.dumps(update["details"], ensure_ascii=False, sort_keys=True), now),
                )
            if totals is not None:
                connection.execute(
                    "UPDATE netting_batches SET state=?,version=?,totals=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_state, version, json.dumps(totals, ensure_ascii=False, sort_keys=True), actor_id, now, batch_id),
                )
            else:
                connection.execute(
                    "UPDATE netting_batches SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_state, version, actor_id, now, batch_id),
                )
            connection.execute(
                "INSERT INTO netting_audit_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM netting_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch(result)

    def audit_timeline(self, batch_id: int) -> List[Dict[str, Any]]:
        self.get(batch_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM netting_audit_events WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
