"""对账包。回单对账的规范实现在 :mod:`app.reconcile.receipts`。"""

from .receipts import (  # noqa: F401
    ClaimLedger,
    CompensationLedger,
    PaymentLedger,
    ReceiptLedger,
    ReceiptMatcher,
    ReceiptReconciler,
    ReconciliationLedger,
    apply_receipt,
    find_subset_groups,
    outstanding_balances,
    reconcile_receipts,
    remaining_by_waybill,
    reverse_receipt,
    settle_receipts,
    waybill_balances,
)


def __getattr__(name):  # pragma: no cover - 命名兼容
    from . import receipts

    return getattr(receipts, name)
