from decimal import Decimal

from app.catalog import SPEC
from app.reconcile.checks import all_checks
from app.reconcile.receipts import (  # noqa: F401
    ReceiptLedger,
    ReceiptReconciler,
    ReconciliationLedger,
    apply_receipt,
    outstanding_balances,
    reconcile_receipts,
    reverse_receipt,
    settle_receipts,
    waybill_balances,
)


def __getattr__(name):
    # 允许 from app.reconcile.ledger import 任意常见回单/余额命名。
    from app.reconcile import receipts

    if hasattr(receipts, name):
        return getattr(receipts, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_ledger(intake, prepared, prepare_issues, results, receipts=None, receipt_debts=None):
    counts = dict(intake["counts"])
    counts["prepared"] = len(prepared)
    counts["prepare_issues"] = len(prepare_issues)
    counts["computed"] = len(results)
    balanced = counts["input"] == counts["accepted"] + counts["isolated"] + counts["duplicates"] + counts["ignored"]
    metric = sum((item.get("metric") for item in results if item.get("metric") is not None), Decimal("0"))
    checks = [
        {"id": "source", "label": "来源行守恒", "ok": balanced},
        {"id": "computed", "label": "计算条目完整", "ok": counts["computed"] == len(results)},
    ]
    ledger = {"counts": counts, "balanced": balanced, "metric_field": SPEC.get("metric_field", "metric"),
              "metric_total": str(metric), "checks": checks, "all_checks": all_checks(checks)}
    if receipts is not None:
        debt_source = receipt_debts if receipt_debts is not None else results
        reconciliation = ReceiptLedger(debt_source).process(receipts)
        ledger["receipt_reconciliation"] = reconciliation
        ledger["receipts"] = reconciliation["receipts"]
        ledger["balances"] = reconciliation["balances"]
        ledger["outstanding_balances"] = reconciliation["outstanding_balances"]
    return ledger
