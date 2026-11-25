"""回单台账的 SQLite 持久化。

核心算法在 :mod:`app.reconcile.receipts`；本模块只保存欠款快照和回单事件，
使 HTTP 请求重试、冲销和后续余额查询能够共享同一份状态。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.reconcile.receipts import ReceiptLedger, extract_receipt


class ReceiptStore:
    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else path
        self._memory_connection: sqlite3.Connection | None = None
        if str(path) == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipt_debts(
                  scope TEXT NOT NULL,
                  waybill_id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(scope, waybill_id)
                );
                CREATE TABLE IF NOT EXISTS receipt_events(
                  scope TEXT NOT NULL,
                  receipt_id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(scope, receipt_id)
                );
                """
            )

    def upsert_debts(self, scope: str, debts: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            for item in debts:
                waybill_id = item.get("waybill_id") or item.get("id")
                if not waybill_id:
                    continue
                db.execute(
                    """INSERT INTO receipt_debts(scope, waybill_id, payload, updated_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(scope, waybill_id) DO UPDATE SET
                         payload=excluded.payload, updated_at=excluded.updated_at""",
                    (scope, str(waybill_id), json.dumps(item, ensure_ascii=False, default=str), now),
                )

    def list_debts(self, scope: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM receipt_debts WHERE scope=? ORDER BY waybill_id", (scope,)
            ).fetchall()
        result = []
        for row in rows:
            try:
                result.append(json.loads(row["payload"]))
            except json.JSONDecodeError:
                continue
        return result

    def add_event(self, scope: str, receipt_id: str, payload: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO receipt_events(scope, receipt_id, payload, created_at) VALUES(?,?,?,?)",
                    (scope, receipt_id, json.dumps(payload, ensure_ascii=False, default=str), now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def list_events(self, scope: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM receipt_events WHERE scope=? ORDER BY rowid", (scope,)
            ).fetchall()
        events = []
        for row in rows:
            try:
                events.append(json.loads(row["payload"]))
            except json.JSONDecodeError:
                continue
        return events

    def build_ledger(self, scope: str, extra_debts: list[dict[str, Any]] | None = None) -> ReceiptLedger:
        debts = self.list_debts(scope)
        if extra_debts:
            debts.extend(extra_debts)
        ledger = ReceiptLedger(debts)
        events = self.list_events(scope)
        if events:
            # 事件按实际处理顺序保存；不能在重放时再次全局重排，否则会改写
            # 早先已经唯一销账的回单归属。
            ledger.process(events, explicit_first=False)
        return ledger

    def apply_and_save(self, scope: str, receipt: dict[str, Any],
                       debts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if debts:
            self.upsert_debts(scope, debts)
        normalized = extract_receipt(receipt)
        receipt_id = normalized["receipt_id"]

        # 先用已有事件重建，保证同一 receipt_id 的冲突/幂等判断与历史一致。
        ledger = self.build_ledger(scope)
        result = ledger.apply(receipt)

        existing_ids = {event.get("receipt_id") or event.get("id") for event in self.list_events(scope)}
        if receipt_id not in existing_ids:
            self.add_event(scope, receipt_id, receipt)
        return result

    def reverse_and_save(self, scope: str, receipt_id: str,
                         reversal: dict[str, Any] | None = None) -> dict[str, Any]:
        ledger = self.build_ledger(scope)
        reversal_id = None
        # 只有像回单/冲销事件的 body 才归一；普通 {"scope": ...} 查询参数不是回单。
        if reversal and any(key in reversal for key in ("reversal_id", "id", "receipt_id", "amount", "amount_cents")):
            try:
                normalized = extract_receipt(reversal)
                reversal_id = normalized["receipt_id"]
            except Exception:
                reversal_id = reversal.get("reversal_id") or reversal.get("id")
        result = ledger.reverse(receipt_id, reversal_id=reversal_id)
        event = reversal if reversal and reversal_id and (
            reversal.get("reversal_id") or reversal.get("id") or reversal.get("receipt_id")
        ) else {
            "receipt_id": reversal_id or f"REV-{uuid.uuid4().hex[:12]}",
            "action": "reverse",
            "original_receipt_id": receipt_id,
            "amount_cents": 0,
        }
        event = dict(event)
        event.setdefault("action", "reverse")
        event.setdefault("original_receipt_id", receipt_id)
        event_id = event.get("receipt_id") or event.get("id")
        if not event_id:
            event["receipt_id"] = f"REV-{uuid.uuid4().hex[:12]}"
            event_id = event["receipt_id"]
        self.add_event(scope, event_id, event)
        return result
