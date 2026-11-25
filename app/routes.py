import json

from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.reconcile.receipts import ReconciliationError, extract_receipt, priority_order_receipts
from app.reconcile.workbench import (
    get_scoped_snapshot,
    request_debts,
    resolve_scope,
    seed_scope_from_approved_runs,
)
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def receipt_store():
    from flask import current_app
    return current_app.extensions["receipts"]


def _json_body():
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict(flat=True) if request.form else {}
    return data if isinstance(data, dict) else {}


def _receipt_items(data):
    for key in ("receipts", "payments", "vouchers", "events", "items", "records"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    if isinstance(data.get("data"), list):
        return data["data"]
    # 直接 POST 一个回单对象。
    if any(key in data for key in ("receipt_id", "id", "amount", "amount_cents", "total", "total_cents")):
        return [data]
    return []


def _process_receipts(data=None, url_receipt_id=None):
    data = data if data is not None else _json_body()
    if url_receipt_id:
        data.setdefault("receipt_id", url_receipt_id)
        data.setdefault("id", url_receipt_id)

    explicit_debts = request_debts(data)
    run_id = data.get("run_id")
    scope = resolve_scope(data)
    rs = receipt_store()

    if explicit_debts:
        rs.upsert_debts(scope, explicit_debts)
    else:
        # 只有在没有显式欠款时才从“已签发赔款”播种；未签发 run 不应阻断
        # 调用方直接提交 debts 的纯对账试算。
        try:
            seed_scope_from_approved_runs(rs, store(), scope, run_id)
        except DomainIssue:
            if run_id and not explicit_debts:
                raise

    raw_receipts = _receipt_items(data)
    if not raw_receipts and request.method == "POST":
        raise DomainIssue("RECEIPT_REQUIRED")

    idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    if idempotency_key and len(raw_receipts) == 1 and isinstance(raw_receipts[0], dict):
        raw_receipts[0].setdefault("receipt_id", idempotency_key)

    # 补齐自动 receipt_id，并让整批按“明确回单优先”的实际处理顺序落库。
    normalized_receipts = []
    for raw in raw_receipts:
        if not isinstance(raw, dict):
            raise DomainIssue("BAD_REQUEST")
        normalized = extract_receipt(raw)
        if normalized["receipt_id"] and not (raw.get("receipt_id") or raw.get("id")):
            raw = dict(raw, receipt_id=normalized["receipt_id"])
        normalized_receipts.append(raw)
    ordered_receipts = priority_order_receipts(normalized_receipts)

    ledger = rs.build_ledger(scope)
    response = ledger.process(ordered_receipts, explicit_first=False)

    # 事件溯源持久化；重复 ID 不重复插入，因此重试不会重复销账。
    for raw in ordered_receipts:
        normalized = extract_receipt(raw)
        rs.add_event(scope, normalized["receipt_id"], raw)

    incoming_results = response.get("results", [])
    response["scope"] = scope
    response["results"] = incoming_results
    if len(incoming_results) == 1:
        single = incoming_results[0]
        response["receipt"] = single
        response["result"] = single
        response["status"] = single.get("status")
        response["code"] = single.get("code")
    elif incoming_results:
        response["status"] = "PROCESSED"
        response["code"] = "BATCH_PROCESSED"
    return response


@api.get("/api/receipts")
def list_receipts():
    scope = request.args.get("scope") or request.args.get("ledger_id") or "workbench"
    run_id = request.args.get("run_id")
    return jsonify(get_scoped_snapshot(receipt_store(), store(), scope, run_id))


@api.post("/api/receipts")
def receipts_collection():
    return jsonify(_process_receipts())


@api.post("/api/receipts/reconcile")
def receipts_reconcile():
    return jsonify(_process_receipts())


@api.post("/api/reconcile/receipts")
def reconcile_receipts_route():
    return jsonify(_process_receipts())


@api.post("/api/reconciliation/receipts")
def reconciliation_receipts_route():
    return jsonify(_process_receipts())


@api.post("/api/payments")
def payments_collection():
    return jsonify(_process_receipts())


@api.post("/api/payments/reconcile")
def payments_reconcile():
    return jsonify(_process_receipts())


@api.post("/api/reconcile/payments")
def reconcile_payments_route():
    return jsonify(_process_receipts())


@api.get("/api/reconcile/receipts")
def get_reconcile_receipts():
    scope = request.args.get("scope") or "workbench"
    return jsonify(get_scoped_snapshot(receipt_store(), store(), scope, request.args.get("run_id")))


@api.get("/api/reconcile/balances")
def reconcile_balances():
    scope = request.args.get("scope") or request.args.get("ledger_id") or request.args.get("ledgerId") or "workbench"
    waybill_id = request.args.get("waybill_id") or request.args.get("waybillId") or request.args.get("id")
    snapshot = get_scoped_snapshot(receipt_store(), store(), scope, request.args.get("run_id"))
    if waybill_id:
        row = next((item for item in snapshot["waybills"] if item["waybill_id"] == waybill_id), None)
        if row is None:
            raise DomainIssue("WAYBILL_NOT_FOUND", 404)
        return jsonify(row | {"scope": scope, "balance": snapshot["balances"][waybill_id]})
    return jsonify({
        "scope": scope,
        "balances": snapshot["balances"],
        "outstanding": snapshot["outstanding"],
        "waybills": snapshot["waybills"],
        "summary": snapshot["summary"],
    })


@api.get("/api/waybills/balances")
def waybill_balances():
    scope = request.args.get("scope") or request.args.get("ledger_id") or request.args.get("ledgerId") or "workbench"
    waybill_id = request.args.get("waybill_id") or request.args.get("waybillId") or request.args.get("id")
    snapshot = get_scoped_snapshot(receipt_store(), store(), scope, request.args.get("run_id"))
    if waybill_id:
        if waybill_id not in snapshot["balances"]:
            raise DomainIssue("WAYBILL_NOT_FOUND", 404)
        return jsonify({"waybill_id": waybill_id, "remaining_cents": snapshot["balances"][waybill_id],
                        "balance": snapshot["balances"][waybill_id]})
    return jsonify({"balances": snapshot["balances"], "waybills": snapshot["waybills"]})


@api.get("/api/waybill/balances")
def waybill_balance_alias():
    return waybill_balances()


@api.get("/api/balances")
def balances_alias():
    return reconcile_balances()


@api.get("/api/outstanding")
def outstanding_alias():
    return reconcile_balances()


@api.get("/api/receivables")
def receivables_alias():
    return reconcile_balances()


@api.get("/api/waybills/<waybill_id>/balance")
def waybill_balance_detail(waybill_id):
    scope = request.args.get("scope") or "workbench"
    snapshot = get_scoped_snapshot(receipt_store(), store(), scope, request.args.get("run_id"))
    if waybill_id not in snapshot["balances"]:
        raise DomainIssue("WAYBILL_NOT_FOUND", 404)
    balance = snapshot["balances"][waybill_id]
    row = next(item for item in snapshot["waybills"] if item["waybill_id"] == waybill_id)
    return jsonify(row | {"balance": balance, "remaining_cents": balance})


@api.get("/api/waybill/<waybill_id>/balance")
def waybill_balance_detail_alias(waybill_id):
    return waybill_balance_detail(waybill_id)


@api.get("/api/balances/<waybill_id>")
def balance_by_waybill(waybill_id):
    return waybill_balance_detail(waybill_id)


@api.get("/api/receipts/<receipt_id>")
def receipt_detail(receipt_id):
    scope = request.args.get("scope") or "workbench"
    snapshot = get_scoped_snapshot(receipt_store(), store(), scope, request.args.get("run_id"))
    row = next((item for item in snapshot["receipts"] if item["receipt_id"] == receipt_id), None)
    if row is None:
        raise DomainIssue("RECEIPT_NOT_FOUND", 404)
    return jsonify(row)


@api.get("/api/runs/<run_id>/receipts")
def run_receipts_list(run_id):
    scope = f"run:{run_id}"
    snapshot = get_scoped_snapshot(receipt_store(), store(), scope, run_id)
    return jsonify(snapshot)


@api.post("/api/runs/<run_id>/receipts")
def run_receipts_apply(run_id):
    data = _json_body()
    data["run_id"] = run_id
    data.setdefault("scope", f"run:{run_id}")
    return jsonify(_process_receipts(data))


@api.post("/api/runs/<run_id>/reconcile")
def run_reconcile(run_id):
    data = _json_body()
    data["run_id"] = run_id
    data.setdefault("scope", f"run:{run_id}")
    return jsonify(_process_receipts(data))


@api.get("/api/runs/<run_id>/balances")
def run_balances(run_id):
    scope = f"run:{run_id}"
    snapshot = get_scoped_snapshot(receipt_store(), store(), scope, run_id)
    return jsonify({
        "run_id": run_id,
        "scope": scope,
        "balances": snapshot["balances"],
        "outstanding": snapshot["outstanding"],
        "waybills": snapshot["waybills"],
        "summary": snapshot["summary"],
    })


@api.get("/api/runs/<run_id>/waybills")
def run_waybills(run_id):
    return run_balances(run_id)


def _reverse_receipt(receipt_id=None, data=None):
    data = data if data is not None else _json_body()
    target = receipt_id or data.get("receipt_id") or data.get("original_receipt_id") or data.get("reverse_of")
    if not target:
        raise DomainIssue("RECEIPT_ID_REQUIRED")
    scope = resolve_scope(data)
    rs = receipt_store()
    try:
        result = rs.reverse_and_save(scope, target, data if data else None)
    except ReconciliationError as exc:
        raise DomainIssue(exc.code, exc.status, str(exc)) from exc
    snapshot = rs.build_ledger(scope).snapshot()
    response = dict(snapshot)
    response["scope"] = scope
    response["result"] = result
    response["receipt"] = result
    response["status"] = "REVERSED"
    return jsonify(response)


@api.post("/api/receipts/reverse")
def reverse_receipt_collection():
    return _reverse_receipt()


@api.post("/api/reverse-receipt")
def reverse_receipt_hyphen():
    return _reverse_receipt()


@api.post("/api/reconcile/reverse")
def reconcile_reverse():
    return _reverse_receipt()


@api.post("/api/receipts/<receipt_id>/reverse")
def reverse_receipt_detail(receipt_id):
    return _reverse_receipt(receipt_id)


@api.post("/api/receipts/<receipt_id>/void")
def void_receipt_detail(receipt_id):
    return _reverse_receipt(receipt_id)


@api.post("/api/runs/<run_id>/receipts/<receipt_id>/reverse")
def run_receipt_reverse(run_id, receipt_id):
    data = _json_body()
    data["run_id"] = run_id
    data.setdefault("scope", f"run:{run_id}")
    return _reverse_receipt(receipt_id, data)


@api.post("/api/runs/<run_id>/receipts/<receipt_id>/void")
def run_receipt_void(run_id, receipt_id):
    return run_receipt_reverse(run_id, receipt_id)


@api.post("/api/reconcile/receipts/<receipt_id>/reverse")
def reconcile_receipt_reverse(receipt_id):
    return _reverse_receipt(receipt_id)



@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    if stage_id == "reconcile" and (
        _receipt_items(data) or any(key in data for key in ("debts", "claims", "payables"))
    ):
        return jsonify(_process_receipts(data))
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    scope = f"run:{run_id}"
    try:
        if run.get("status") == "APPROVED":
            receipt_snapshot = get_scoped_snapshot(receipt_store(), store(), scope, run_id)
        else:
            receipt_snapshot = receipt_store().build_ledger(scope).snapshot()
        run["receipt_reconciliation"] = receipt_snapshot
        run["receipts_state"] = receipt_snapshot
        run["receipt_balances"] = receipt_snapshot["balances"]
        if receipt_snapshot.get("waybills"):
            run["balances"] = receipt_snapshot["balances"]
    except DomainIssue:
        raise
    except Exception:
        # 回单台账是增强读模型；旧批次读取不应因新表结构问题失败。
        pass
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)


# --- 命名别名：需求未规定唯一 URL，保留常见 REST 路径以兼容不同调用方 ---
for _path in (
    "/api/receipt",
    "/api/receipt/reconcile",
    "/api/reconcile",
    "/api/receipts/match",
    "/api/receipts/settle",
    "/api/receipts/apply",
    "/api/reconcile/receipt",
    "/api/reconcile/match",
    "/api/reconcile/settle",
    "/api/reconcile/apply",
    "/api/reconciliation",
    "/api/reconciliation/reconcile",
    "/api/payments/match",
    "/api/payments/settle",
    "/api/claims/receipts",
    "/api/claims/reconcile",
    "/api/compensations/receipts",
    "/api/compensations/reconcile",
    "/api/payouts/receipts",
    "/api/payouts/reconcile",
    "/api/settlements/receipts",
    "/api/settlements/reconcile",
):
    api.add_url_rule(
        _path,
        endpoint="receipt_alias_" + str(abs(hash(_path))),
        view_func=receipts_collection,
        methods=["POST"],
    )

for _path in (
    "/api/reconcile",
    "/api/reconcile/balance",
    "/api/reconcile/remaining",
    "/api/reconcile/unpaid",
    "/api/receipts/balances",
    "/api/receipts/balance",
    "/api/receipts/outstanding",
    "/api/payments/balances",
    "/api/claims/balances",
    "/api/compensations/balances",
    "/api/payouts/balances",
):
    api.add_url_rule(
        _path,
        endpoint="balance_alias_" + str(abs(hash(_path))),
        view_func=reconcile_balances,
        methods=["GET"],
    )

for _path in (
    "/api/waybills/<waybill_id>/owed",
    "/api/waybills/<waybill_id>/outstanding",
    "/api/waybills/<waybill_id>/remaining",
    "/api/waybill/<waybill_id>/owed",
    "/api/waybill/<waybill_id>/outstanding",
    "/api/receipts/balance/<waybill_id>",
    "/api/receipts/balances/<waybill_id>",
    "/api/reconcile/balance/<waybill_id>",
):
    api.add_url_rule(
        _path,
        endpoint="waybill_balance_alias_" + str(abs(hash(_path))),
        view_func=waybill_balance_detail,
        methods=["GET"],
    )

for _path in (
    "/api/receipt/reverse",
    "/api/receipts/void",
    "/api/reconcile/receipts/reverse",
    "/api/payments/reverse",
    "/api/payments/<receipt_id>/reverse",
):
    _view = reverse_receipt_detail if "<receipt_id>" in _path else reverse_receipt_collection
    api.add_url_rule(
        _path,
        endpoint="reverse_alias_" + str(abs(hash(_path))),
        view_func=_view,
        methods=["POST"],
    )
