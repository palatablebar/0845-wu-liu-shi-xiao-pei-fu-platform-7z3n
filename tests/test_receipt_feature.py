import pytest

from app.reconcile.receipts import (
    ReceiptLedger,
    find_subset_groups,
    reconcile_receipts,
)


def claims():
    return [
        {"waybill_id": "A", "compensation_cents": 100},
        {"waybill_id": "B", "compensation_cents": 200},
        {"waybill_id": "C", "compensation_cents": 300},
    ]


def test_explicit_receipt_settles_named_waybill():
    result = reconcile_receipts(claims(), [
        {"receipt_id": "R1", "waybill_id": "B", "amount_cents": 200},
    ])
    assert result["balances"] == {"A": 100, "B": 0, "C": 300}
    assert result["receipts"][0]["status"] == "APPLIED"


def test_unique_combined_group_writes_off_only_group():
    groups = find_subset_groups({"A": 100, "B": 200, "C": 400}, 300)
    assert groups == [["A", "B"]]
    result = reconcile_receipts(
        [{"waybill_id": "A", "amount": 100}, {"waybill_id": "B", "amount": 200},
         {"waybill_id": "C", "amount": 400}],
        [{"receipt_id": "P1", "total": 300}],
    )
    assert result["balances"] == {"A": 0, "B": 0, "C": 400}


def test_ambiguous_combined_payment_is_held_without_writeoff():
    result = reconcile_receipts(claims(), [{"receipt_id": "P1", "amount_cents": 300}])
    assert result["balances"] == {"A": 100, "B": 200, "C": 300}
    held = result["held"][0]
    assert held["code"] == "AMBIGUOUS_ALLOCATION"
    assert held["candidate_groups"] == [["C"], ["A", "B"]]
    assert held["status_label"] == "挂账"


def test_explicit_receipts_are_prioritized_within_batch():
    result = reconcile_receipts(claims(), [
        {"receipt_id": "P1", "amount_cents": 300},
        {"receipt_id": "R_C", "waybill_id": "C", "amount_cents": 300},
    ])
    assert result["balances"] == {"A": 0, "B": 0, "C": 0}
    by_id = {item["receipt_id"]: item for item in result["receipts"]}
    assert by_id["R_C"]["status"] == "APPLIED"
    assert by_id["P1"]["status"] == "APPLIED"


def test_settled_debts_do_not_participate():
    ledger = ReceiptLedger(claims())
    ledger.apply({"receipt_id": "R_A", "waybill_id": "A", "amount_cents": 100})
    # 100 只能匹配仍未结清的 A；A 已为零后不存在候选。
    result = ledger.apply({"receipt_id": "P100", "amount_cents": 100})
    assert result["status"] == "HELD"
    assert result["code"] == "NO_MATCHING_ALLOCATION"
    assert ledger.balance("A") == 0


def test_repeated_receipt_does_not_duplicate_writeoff():
    ledger = ReceiptLedger(claims())
    receipt = {"receipt_id": "R1", "waybill_id": "A", "amount_cents": 100}
    first = ledger.apply(receipt)
    second = ledger.apply(dict(receipt))
    assert first["status"] == "APPLIED"
    assert second["idempotent_replay"] is True
    assert len(ledger.active_allocations(receipt_id="R1")) == 1
    assert ledger.balance("A") == 0


def test_conflicting_same_receipt_id_is_rejected():
    ledger = ReceiptLedger(claims())
    ledger.apply({"receipt_id": "R1", "waybill_id": "A", "amount_cents": 100})
    with pytest.raises(Exception) as exc:
        ledger.apply({"receipt_id": "R1", "waybill_id": "B", "amount_cents": 200})
    assert getattr(exc.value, "code", None) == "RECEIPT_CONFLICT"


def test_reversal_restores_only_allocations_from_original_receipt():
    ledger = ReceiptLedger(claims())
    ledger.apply({"receipt_id": "R_A", "waybill_id": "A", "amount_cents": 100})
    ledger.apply({"receipt_id": "R_B", "waybill_id": "B", "amount_cents": 200})
    reversed_result = ledger.reverse("R_A")
    assert [item["waybill_id"] for item in reversed_result["restored_allocations"]] == ["A"]
    assert ledger.outstanding_balances() == {"A": 100, "B": 0, "C": 300}


def test_reversing_combined_receipt_restores_its_full_group_only():
    ledger = ReceiptLedger([
        {"waybill_id": "A", "amount": 100},
        {"waybill_id": "B", "amount": 200},
        {"waybill_id": "C", "amount": 400},
    ])
    ledger.apply({"receipt_id": "P", "amount": 300})
    ledger.apply({"receipt_id": "C", "waybill_id": "C", "amount": 400})
    ledger.reverse("P")
    assert ledger.outstanding_balances() == {"A": 100, "B": 200, "C": 0}


def test_partial_payment_and_query_each_waybill():
    ledger = ReceiptLedger([{"waybill_id": "W1", "amount_cents": 1000}])
    ledger.apply({"receipt_id": "R1", "waybill_id": "W1", "amount_cents": 300})
    assert ledger.outstanding("W1") == 700
    assert ledger.outstanding() == {"W1": 700}
    row = ledger.snapshot()["waybills"][0]
    assert row["status"] == "PARTIAL"
    assert row["remaining_cents"] == 700


def test_http_receipt_flow_is_idempotent_and_reversible(client):
    debts = [
        {"waybill_id": "A", "amount_cents": 100},
        {"waybill_id": "B", "amount_cents": 200},
    ]
    payload = {"scope": "month-end", "debts": debts,
               "receipt_id": "R1", "waybill_id": "A", "amount_cents": 100}
    first = client.post("/api/receipts", json=payload)
    second = client.post("/api/receipts", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["result"]["idempotent_replay"] is True
    assert second.get_json()["balances"] == {"A": 0, "B": 200}

    balance = client.get("/api/waybills/A/balance?scope=month-end")
    assert balance.status_code == 200
    assert balance.get_json()["remaining_cents"] == 0

    reversed_response = client.post("/api/receipts/R1/reverse", json={"scope": "month-end"})
    assert reversed_response.status_code == 200
    assert reversed_response.get_json()["balances"] == {"A": 100, "B": 200}
