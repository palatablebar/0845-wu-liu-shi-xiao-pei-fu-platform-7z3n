"""把已签发批次中的赔款转换成回单对账欠款。"""

from __future__ import annotations

import json
from typing import Any

from app.errors import DomainIssue
from app.reconcile.receipts import ReconciliationError, extract_debts


DEBT_COLLECTION_KEYS = ("debts", "claims", "payables", "waybills", "balances", "debt_items")


def _row_debt(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    if row.get("verdict") == "REJECT":
        return None
    waybill_id = row.get("waybill_id") or row.get("id") or row.get("key")
    if not waybill_id:
        return None
    amount = (row.get("compensation_cents") if row.get("compensation_cents") is not None
              else row.get("metric"))
    if amount is None:
        amount = row.get("invoice_cents")
    if amount is None:
        return None
    return {
        "waybill_id": str(waybill_id),
        "compensation_cents": amount,
        "verdict": row.get("verdict"),
        "reason": row.get("reason"),
        "run_id": row.get("run_id"),
    }


def debts_from_run_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        rows = payload.get("results") if isinstance(payload.get("results"), list) else []
    debts = [debt for row in rows if (debt := _row_debt(row)) is not None]
    # 同一运单在一个批次中只保留一条；后出现的结果通常是最终判定行。
    unique = {}
    for debt in debts:
        unique[debt["waybill_id"]] = debt
    return list(unique.values())


def approved_run_debts(run_store: Any, run_id: str | None = None,
                       *, require_run: bool = False) -> list[dict[str, Any]]:
    if run_id:
        row = run_store.get(run_id) if hasattr(run_store, "get") else None
        if row is None:
            raise DomainIssue("RUN_NOT_FOUND", 404)
        if row.get("status") != "APPROVED":
            raise DomainIssue("NOT_APPROVED", 409)
        payload = json.loads(row["payload"]) if isinstance(row.get("payload"), str) else row.get("payload", {})
        debts = debts_from_run_payload(payload)
        for debt in debts:
            debt["run_id"] = run_id
        return debts

    if not hasattr(run_store, "connect"):
        return []
    debts: dict[str, dict[str, Any]] = {}
    with run_store.connect() as db:
        rows = db.execute(
            "SELECT id, payload, created_at FROM runs WHERE status='APPROVED' ORDER BY created_at ASC"
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        for debt in debts_from_run_payload(payload):
            debt["run_id"] = row["id"]
            # 后签发的批次代表同运单的最新赔款口径。
            debts[debt["waybill_id"]] = debt
    return list(debts.values())


def request_debts(data: dict[str, Any]) -> list[dict[str, Any]]:
    primary_keys = ("debts", "claims", "payables")
    for key in primary_keys:
        if data.get(key) is not None:
            parsed = extract_debts(data[key])
            return list(parsed.values())

    for key in DEBT_COLLECTION_KEYS:
        value = data.get(key)
        if value is None or key in primary_keys:
            continue
        # waybills=["A","B"] 是回单归属，不是欠款快照。
        if key == "waybills" and isinstance(value, list) and all(
            isinstance(item, (str, int)) for item in value
        ):
            continue
        if isinstance(value, (list, dict)):
            try:
                parsed = extract_debts(value)
            except Exception:
                continue
            return list(parsed.values())
    return []


def resolve_scope(data: dict[str, Any], default: str = "workbench") -> str:
    for key in ("scope", "ledger_id", "account_id", "reconciliation_id", "book_id"):
        value = data.get(key)
        if value:
            return str(value)
    run_id = data.get("run_id")
    if run_id:
        return f"run:{run_id}"
    return default


def seed_scope_from_approved_runs(receipt_store: Any, run_store: Any, scope: str,
                                  run_id: str | None = None) -> list[dict[str, Any]]:
    """把已签发赔款写入对应 scope；显式 run_id 时要求该批次已签发。"""

    if run_id:
        debts = approved_run_debts(run_store, run_id, require_run=True)
        target_scope = f"run:{run_id}" if scope == "workbench" else scope
    elif scope.startswith("run:"):
        debts = approved_run_debts(run_store, scope[4:])
        target_scope = scope
    else:
        debts = approved_run_debts(run_store)
        target_scope = scope
    if debts:
        receipt_store.upsert_debts(target_scope, debts)
    return debts


def get_scoped_snapshot(receipt_store: Any, run_store: Any, scope: str,
                        run_id: str | None = None):
    seed_scope_from_approved_runs(receipt_store, run_store, scope, run_id)
    return receipt_store.build_ledger(scope).snapshot()
