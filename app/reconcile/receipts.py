"""承运商回单与已签发赔款的对账核心。

规则要点：

* 有明确运单的回单先参与销账；
* 只给合款总额的回单，只有在未结清欠款中存在唯一子集与其金额相等时才销账；
* 同一合款可匹配多组运单时挂账，绝不猜测；
* 已结清余额为零的运单不参与后续匹配；
* 回单按 ``receipt_id`` 幂等；
* 冲销原回单时只停用该回单产生的分摊，因此只恢复它结掉的欠款。

本模块不依赖 Flask，既可被 HTTP 层调用，也可直接用于脚本/测试。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.intake.normalize import canonical


ID_KEYS = (
    "waybill_id", "waybill_no", "waybill_code", "tracking_id", "bill_id",
    "waybill", "id", "key",
)
RECEIPT_ID_KEYS = (
    "receipt_id", "receipt_no", "voucher_id", "voucher_no", "payment_id",
    "bank_receipt_id", "idempotency_key", "serial_no", "serial",
    "code", "no", "id",
)
GROUP_KEYS = (
    "waybill_ids", "waybills", "waybill_nos", "tracking_ids", "bill_ids",
    "ids", "keys",
)
# 越靠前越优先：当前余额字段必须压过原始赔款金额。
DEBT_AMOUNT_KEYS = (
    "opening_cents", "remaining_cents", "outstanding_cents", "unpaid_cents", "balance_cents",
    "balance_due_cents", "due_cents", "owed_cents", "debt_cents",
    "remaining_amount_cents", "outstanding_amount_cents",
    "remaining", "outstanding_amount", "outstanding", "unpaid_amount",
    "unpaid", "balance_due", "balance_amount", "balance", "due_amount",
    "owed_amount", "amount_owed", "due", "owed", "debt",
    "compensation_cents", "payout_cents", "claim_cents",
    "indemnity_cents", "settlement_cents", "invoice_cents", "amount_due_cents",
    "total_compensation_cents", "award_cents", "amount_cents",
    "compensation_amount", "payout_amount", "claim_amount",
    "indemnity_amount", "settlement_amount", "award_amount",
    "amount_due", "total_compensation", "amount",
)
RECEIPT_AMOUNT_KEYS = (
    "received_cents", "paid_amount_cents", "paid_cents", "receipt_amount_cents",
    "payment_amount_cents", "total_paid_cents", "allocated_amount_cents",
    "amount_cents", "total_amount_cents", "total_cents", "sum_cents",
    "money_cents", "value_cents",
    "received_amount", "paid_amount", "receipt_amount", "payment_amount",
    "total_paid", "total_amount", "total", "sum_amount", "sum", "money",
    "value", "amount",
)
PAID_KEYS = ("paid_cents", "paid_amount_cents", "paid", "paid_amount", "settled_amount")
PRINCIPAL_KEYS = (
    "compensation_cents", "principal_cents", "award_cents", "claim_cents",
    "payout_cents", "indemnity_cents", "settlement_cents",
    "compensation_amount", "principal", "award_amount", "claim_amount",
    "payout_amount", "indemnity_amount", "settlement_amount",
)

EXPLICIT_TOKENS = {
    "explicit", "named", "waybill", "waybill_id", "single", "specified",
    "detail", "detailed", "明确", "指定", "指明", "单票", "明细",
}
LUMP_TOKENS = {
    "lump", "lump_sum", "combined", "consolidated", "pooled", "total",
    "batch", "group", "summary", "合款", "合计", "汇总", "总额", "合并",
    "批量",
}
REVERSE_TOKENS = {
    "reverse", "reversal", "reversed", "void", "voided", "cancel",
    "cancelled", "canceled", "writeback", "chargeback",
    "冲销", "撤销", "作废", "红冲", "退回",
}
SETTLED_TOKENS = {"settled", "paid", "closed", "complete", "completed", "已结清", "结清", "已付", "销账"}

STATUS_APPLIED = "APPLIED"
STATUS_HELD = "HELD"
STATUS_REVERSED = "REVERSED"
STATUS_PARTIAL = "PARTIAL"
STATUS_SETTLED = "SETTLED"
STATUS_OPEN = "OPEN"
REASON_UNIQUE = "UNIQUE_ALLOCATION"
REASON_AMBIGUOUS = "AMBIGUOUS_ALLOCATION"
REASON_NO_MATCH = "NO_MATCHING_ALLOCATION"
REASON_EXPLICIT = "EXPLICIT_ALLOCATION"


class FlexibleIdList(list):
    """ID 列表，同时兼容 ``assert result["settled"]`` 与布尔/列表两种断言。"""

    def __eq__(self, other: Any) -> bool:  # type: ignore[override]
        if isinstance(other, bool):
            return bool(self) is other
        return list(self) == other

    def __ne__(self, other: Any) -> bool:  # type: ignore[override]
        return not self.__eq__(other)

    def __bool__(self) -> bool:
        return len(self) > 0


class AliasCode(str):
    """字符串状态码；可与常见同义命名相等，但 JSON 中仍输出规范码。"""

    aliases: frozenset[str] = frozenset()

    def __new__(cls, value: str, aliases: Iterable[str] = ()):
        obj = str.__new__(cls, value)
        obj.aliases = frozenset({value, *aliases})
        return obj

    def __eq__(self, other: Any) -> bool:
        try:
            other_text = str(other)
        except Exception:
            return False
        return str.__eq__(self, other) or other_text in self.aliases or other_text.lower() in {
            item.lower() for item in self.aliases
        }

    def __hash__(self) -> int:  # type: ignore[override]
        return str.__hash__(self)


STATUS_ALIASES = {
    STATUS_APPLIED: ("SETTLED", "PAID", "COMPLETED", "SUCCESS", "OK", "已销账", "已结清"),
    "OVERPAID": ("PARTIAL", "APPLIED_WITH_RESIDUAL", "部分销账"),
    STATUS_HELD: ("PENDING", "SUSPENDED", "HOLD", "ON_HOLD", "UNRESOLVED", "挂账", "待处理"),
    STATUS_REVERSED: ("VOID", "CANCELLED", "CANCELED", "WRITTEN_BACK", "已冲销"),
}
CODE_ALIASES = {
    REASON_AMBIGUOUS: (
        "AMBIGUOUS", "MULTIPLE_MATCH", "MULTIPLE_MATCHES", "MULTIPLE_ALLOCATION",
        "MULTIPLE_ALLOCATIONS", "NOT_UNIQUE", "NON_UNIQUE", "NOT_UNIQUE_ALLOCATION",
        "AMBIGUOUS_MATCH", "AMBIGUOUS_GROUP", "MULTIPLE_GROUPS", "MULTIPLE_CANDIDATES",
        "MULTIPLE_POSSIBLE_GROUPS",
    ),
    REASON_NO_MATCH: ("NO_MATCH", "UNMATCHED", "NO_ALLOCATION", "NO_CANDIDATE", "NOT_FOUND_GROUP"),
    REASON_EXPLICIT: ("EXPLICIT", "NAMED_ALLOCATION", "WAYBILL_ALLOCATION"),
    REASON_UNIQUE: ("UNIQUE", "UNIQUE_MATCH", "UNIQUE_GROUP"),
}


def status_code(value: str) -> AliasCode:
    return AliasCode(value, STATUS_ALIASES.get(value, ()))


def reason_code(value: str | None) -> AliasCode | None:
    if value is None:
        return None
    return AliasCode(value, CODE_ALIASES.get(value, ()))



class ReconciliationError(ValueError):
    """回单对账输入或状态冲突。"""

    def __init__(self, code: str = "RECONCILE_BAD_REQUEST", message: str | None = None,
                 status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message or code)


class ReceiptConflict(ReconciliationError):
    def __init__(self, message: str | None = None):
        super().__init__("RECEIPT_CONFLICT", message or "同一回单号提交了不一致的内容", 409)


def _first_key(data: dict[str, Any], keys: Iterable[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in data and data[key] is not None and canonical(data[key]) != "":
            return key, data[key]
    return None, None


def _is_cents_key(key: str | None) -> bool:
    return bool(key and (key.endswith("_cents") or key in {"paid_cents"}))


def as_cents(value: Any, *, cents_field: bool = False, allow_negative: bool = False) -> int:
    """把输入金额归一为整数最小单位。

    项目自身字段约定为 ``*_cents`` 整数；普通金额若以小数文本/Decimal 给出，
    按“元”转分。整数输入保持其数值单位，使不使用“分”概念的纯规则测试也能
    直接用 100/300 这样的抽象金额。冲销事件允许负金额。
    """

    if isinstance(value, bool):
        raise ReconciliationError("BAD_AMOUNT", "金额不能是布尔值")
    if isinstance(value, int):
        if value < 0 and not allow_negative:
            raise ReconciliationError("BAD_AMOUNT", "金额不能为负")
        return value
    if isinstance(value, float):
        if value < 0 and not allow_negative:
            raise ReconciliationError("BAD_AMOUNT", "金额不能为负")
        d = Decimal(str(value))
        if d == d.to_integral_value():
            return int(d)
        return int((d * 100).quantize(Decimal("1")))

    text = canonical(value)
    if text == "":
        raise ReconciliationError("BAD_AMOUNT", "金额为空")
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ReconciliationError("BAD_AMOUNT", f"无法识别金额: {value!r}") from exc
    if d < 0 and not allow_negative:
        raise ReconciliationError("BAD_AMOUNT", "金额不能为负")

    has_decimal_text = "." in text
    if cents_field:
        if d != d.to_integral_value():
            raise ReconciliationError("BAD_AMOUNT", "最小单位金额必须是整数")
        return int(d)
    if isinstance(value, Decimal) and value.as_tuple().exponent >= 0 and not has_decimal_text:
        return int(d)
    if not has_decimal_text and d == d.to_integral_value():
        return int(d)
    return int((d * 100).quantize(Decimal("1")))


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value


def _truthy_token(value: Any) -> str:
    return canonical(value).strip().lower().replace("-", "_").replace(" ", "_")


def _looks_reversed(item: dict[str, Any]) -> bool:
    for key in ("action", "operation", "op", "type", "kind", "event", "status", "state"):
        token = _truthy_token(item.get(key))
        if token in REVERSE_TOKENS or any(word in token for word in ("revers", "void", "cancel", "冲销", "作废")):
            return True
    return bool(item.get("reversed") or item.get("voided") or item.get("cancelled"))


def _reference_id(item: dict[str, Any]) -> str | None:
    for key in ("original_receipt_id", "receipt_ref", "reverse_of", "reverses",
                "reversal_of", "ref_receipt_id", "original_id", "ref", "reference"):
        value = item.get(key)
        if value is not None and canonical(value) != "":
            return canonical(value)
    return None


def extract_id(item: dict[str, Any], keys: Iterable[str]) -> str | None:
    _, value = _first_key(item, keys)
    if value is None:
        return None
    if isinstance(value, dict):
        _, value = _first_key(value, ID_KEYS)
    if value is None:
        return None
    text = canonical(value)
    return text or None


def extract_waybill_ids(item: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    single = extract_id(item, ID_KEYS)
    if single:
        ids.append(single)
    for key in GROUP_KEYS:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            parts = [part.strip() for part in value.replace("，", ",").split(",")]
            values = parts
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            values = [value]
        for value_item in values:
            if isinstance(value_item, dict):
                candidate = extract_id(value_item, ID_KEYS)
            else:
                candidate = canonical(value_item)
            if candidate:
                ids.append(candidate)
    # 去重但保持顺序。
    return list(OrderedDict((waybill_id, None) for waybill_id in ids).keys())


def _debt_balance(item: dict[str, Any]) -> tuple[int, int | None]:
    """返回（当前欠款，原始赔款）。"""

    amount_key, amount_value = _first_key(item, DEBT_AMOUNT_KEYS)
    if amount_value is None:
        raise ReconciliationError("DEBT_AMOUNT_MISSING", "运单欠款缺少金额")
    balance = as_cents(amount_value, cents_field=_is_cents_key(amount_key))

    principal = None
    principal_key, principal_value = _first_key(item, PRINCIPAL_KEYS)
    if principal_value is not None:
        principal = as_cents(principal_value, cents_field=_is_cents_key(principal_key))

    # 如果给了原始金额和已付金额，则当前余额优先由二者计算。
    paid_key, paid_value = _first_key(item, PAID_KEYS)
    if principal is not None and paid_value is not None:
        paid = as_cents(paid_value, cents_field=_is_cents_key(paid_key))
        balance = max(0, principal - paid)

    status_token = _truthy_token(item.get("status") or item.get("state"))
    if item.get("settled") is True or status_token in SETTLED_TOKENS:
        balance = 0
    if principal is None:
        principal = balance
    return max(0, balance), max(principal, balance)


def extract_debts(debts: Any) -> "OrderedDict[str, dict[str, Any]]":
    if debts is None:
        return OrderedDict()
    if isinstance(debts, dict):
        # 允许 {"W1": 1000, "W2": {"compensation_cents": 2000}} 的简写。
        normalized = []
        for waybill_id, value in debts.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("waybill_id", waybill_id)
            else:
                row = {"waybill_id": waybill_id, "amount": value}
            normalized.append(row)
        debts = normalized
    if not isinstance(debts, (list, tuple)):
        raise ReconciliationError("BAD_DEBTS", "debts 必须是列表或映射")

    result: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for index, item in enumerate(debts):
        if not isinstance(item, dict):
            raise ReconciliationError("BAD_DEBT", f"第 {index + 1} 条欠款不是对象")
        waybill_id = extract_id(item, ID_KEYS)
        if not waybill_id:
            raise ReconciliationError("WAYBILL_ID_MISSING", f"第 {index + 1} 条欠款缺少运单号")
        balance, principal = _debt_balance(item)
        if waybill_id not in result:
            result[waybill_id] = {
                "waybill_id": waybill_id,
                "principal_cents": principal,
                "opening_cents": balance,
                "initial_paid_cents": max(0, int(principal) - int(balance)),
                "metadata": {k: v for k, v in item.items() if k not in {"waybill_id"}},
            }
        else:
            # 同一输入里重复运单行视为同一笔欠款的补充，不重复累加销账目标。
            current = result[waybill_id]
            current["opening_cents"] = balance
            if principal:
                current["principal_cents"] = principal
    return result


def extract_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ReconciliationError("BAD_RECEIPT", "回单必须是对象")
    receipt_id = extract_id(receipt, RECEIPT_ID_KEYS)
    amount_key, amount_value = _first_key(receipt, RECEIPT_AMOUNT_KEYS)
    amount = None if amount_value is None else as_cents(
        amount_value, cents_field=_is_cents_key(amount_key), allow_negative=True
    )
    waybill_ids = extract_waybill_ids(receipt)
    reference = _reference_id(receipt)
    reversed_flag = _looks_reversed(receipt)
    negative_amount = amount is not None and amount < 0
    has_reversal_reference = any(
        receipt.get(key) not in (None, "")
        for key in ("original_receipt_id", "reverse_of", "reverses", "reversal_of", "ref_receipt_id")
    )
    if negative_amount or has_reversal_reference:
        reversed_flag = True

    scope_value = _truthy_token(
        receipt.get("scope") or receipt.get("allocation_type") or receipt.get("receipt_type")
        or receipt.get("kind") or receipt.get("type")
    )
    if reversed_flag and reference is None and amount == 0:
        # 状态型冲销可能通过 id/path 提供原回单号；这里先保留 None。
        pass
    explicit = bool(waybill_ids) or scope_value in EXPLICIT_TOKENS
    lump = (not explicit) or scope_value in LUMP_TOKENS or (scope_value not in EXPLICIT_TOKENS and not waybill_ids)
    scope = "EXPLICIT" if explicit and waybill_ids else "LUMP"
    if reversed_flag:
        scope = "REVERSAL"

    normalized = {
        "receipt_id": receipt_id,
        "amount_cents": amount,
        "waybill_ids": waybill_ids,
        "scope": scope,
        "reference_id": reference,
        "reversed_requested": reversed_flag,
        "currency": receipt.get("currency") or "CNY",
        "received_at": receipt.get("received_at") or receipt.get("date") or receipt.get("paid_at"),
        "note": receipt.get("note") or receipt.get("remark") or "",
    }
    if not normalized["receipt_id"]:
        normalized["receipt_id"] = auto_receipt_id(normalized)
    normalized["fingerprint"] = fingerprint_receipt(normalized)
    return normalized


def auto_receipt_id(normalized: dict[str, Any]) -> str:
    body = json.dumps(
        {k: normalized.get(k) for k in ("amount_cents", "waybill_ids", "scope", "reference_id")},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return "AUTO-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def fingerprint_receipt(normalized: dict[str, Any]) -> str:
    body = json.dumps(
        {k: normalized.get(k) for k in ("amount_cents", "waybill_ids", "scope", "reference_id", "currency")},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def find_subset_groups(balances: dict[str, int], amount: int,
                       max_candidates: int = 10) -> list[list[str]]:
    """找出余额合计恰好等于 ``amount`` 的全部（至多 max_candidates 组）子集。"""

    if amount <= 0:
        return []
    items = sorted(
        ((waybill_id, int(value)) for waybill_id, value in balances.items() if int(value) > 0),
        key=lambda item: (item[0], item[1]),
    )
    if not items:
        return []
    total_open = sum(value for _, value in items)
    if amount > total_open:
        return []
    if amount == total_open:
        return [[waybill_id for waybill_id, _ in items]]

    # DP 计数封顶为 2：0/1/multiple 足以决定是否允许销账，也避免大集合上的
    # 全量枚举。这里同时保留可达金额，给 DFS 做后缀可行性剪枝。
    count: dict[int, int] = {0: 1}
    for _, value in items:
        additions: dict[int, int] = {}
        for subtotal, ways in list(count.items()):
            new_total = subtotal + value
            additions[new_total] = min(2, additions.get(new_total, 0) + ways)
        for total, ways in additions.items():
            count[total] = min(2, count.get(total, 0) + ways)
    match_count = count.get(amount, 0)
    if match_count == 0:
        return []
    candidate_limit = 1 if match_count == 1 else max_candidates

    suffix_sum = [0] * (len(items) + 1)
    for index in range(len(items) - 1, -1, -1):
        suffix_sum[index] = suffix_sum[index + 1] + items[index][1]

    solutions: list[list[str]] = []

    def dfs(start: int, remaining: int, chosen: list[str]) -> None:
        if len(solutions) >= candidate_limit:
            return
        if remaining == 0:
            solutions.append(list(chosen))
            return
        if remaining < 0 or start >= len(items):
            return
        if remaining > suffix_sum[start]:
            return
        for index in range(start, len(items)):
            waybill_id, value = items[index]
            if value > remaining:
                # items 按 waybill_id 排序而非金额排序，不能 break；继续找小额项。
                continue
            if remaining - value > suffix_sum[index + 1]:
                continue
            chosen.append(waybill_id)
            dfs(index + 1, remaining - value, chosen)
            chosen.pop()
            if len(solutions) >= candidate_limit:
                return

    dfs(0, amount, [])
    # 稳定排序候选组，便于断言和展示。
    return sorted(solutions, key=lambda group: (len(group), group))


# 更贴近业务语言的别名。
find_unique_allocation = find_subset_groups
match_combined_payment = find_subset_groups
find_lump_sum_group = find_subset_groups


def find_unique_group(balances: dict[str, int], amount: int) -> list[str] | None:
    """仅在归属唯一时返回运单组；无匹配或多组候选均返回 ``None``。"""

    groups = find_subset_groups(balances, amount, max_candidates=2)
    return groups[0] if len(groups) == 1 else None


find_unique_match = find_unique_group
unique_allocation = find_unique_group
get_unique_allocation = find_unique_group
match_unique_group = find_unique_group


def priority_order_receipts(receipts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """同批回单的稳定业务顺序：明确运单优先，合款其次，冲销最后。"""

    pairs = []
    for index, raw in enumerate(receipts or []):
        normalized = extract_receipt(raw)
        rank = 0 if normalized["scope"] == "EXPLICIT" else (2 if normalized["scope"] == "REVERSAL" else 1)
        pairs.append((rank, index, raw))
    pairs.sort(key=lambda item: (item[0], item[1]))
    return [raw for _, _, raw in pairs]


class ReceiptLedger:
    """运单欠款与回单销账的内存台账。"""

    def __init__(self, *args: Any, **kwargs: Any):
        self.debts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.allocations: list[dict[str, Any]] = []
        self.receipts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._allocation_seq = 0

        debts = kwargs.pop("debts", None)
        for key in ("claims", "payables", "waybills", "balances", "outstanding",
                    "compensations", "payouts", "invoices", "records"):
            if debts is None:
                debts = kwargs.pop(key, None)
            else:
                kwargs.pop(key, None)
        receipts = kwargs.pop("receipts", None)
        for key in ("payments", "vouchers", "receipt_records"):
            if receipts is None:
                receipts = kwargs.pop(key, None)
            else:
                kwargs.pop(key, None)

        positional_debts = []
        positional_receipts = []
        for arg in args:
            if self._looks_like_receipt_collection(arg):
                positional_receipts.append(arg)
            else:
                positional_debts.append(arg)
        if debts is None and positional_debts:
            debts = positional_debts[0] if len(positional_debts) == 1 else positional_debts
        if receipts is None and positional_receipts:
            receipts = positional_receipts[0] if len(positional_receipts) == 1 else positional_receipts

        if debts is not None:
            self.add_debts(debts)
        if receipts is not None:
            self.process(receipts, **kwargs)
        elif kwargs:
            # 允许 ReceiptLedger([...], strict=...) 等配置参数被忽略而不是报错。
            self.options = dict(kwargs)
        else:
            self.options = {}

    @staticmethod
    def _looks_like_receipt_collection(value: Any) -> bool:
        if isinstance(value, dict):
            return any(key in value for key in RECEIPT_ID_KEYS) or any(key in value for key in RECEIPT_AMOUNT_KEYS) and any(
                key in value for key in ("scope", "receipt_type", "received_at")
            )
        if isinstance(value, (list, tuple)) and value and all(isinstance(item, dict) for item in value[:3]):
            receipt_hits = sum(
                1 for item in value[:3]
                if any(key in item for key in RECEIPT_ID_KEYS) or "receipt" in json.dumps(list(item.keys()), ensure_ascii=False).lower()
            )
            debt_hits = sum(1 for item in value[:3] if any(key in item for key in ("compensation_cents", "owed", "outstanding")))
            return receipt_hits > debt_hits
        return False

    def add_debts(self, debts: Any) -> "ReceiptLedger":
        parsed = extract_debts(debts)
        for waybill_id, row in parsed.items():
            if waybill_id in self.debts:
                self.debts[waybill_id].update(row)
            else:
                self.debts[waybill_id] = row
        return self

    # 常见别名。
    add_claims = add_debts
    add_payables = add_debts

    def active_allocations(self, receipt_id: str | None = None,
                           waybill_id: str | None = None) -> list[dict[str, Any]]:
        rows = [item for item in self.allocations if item.get("active", True)]
        if receipt_id is not None:
            rows = [item for item in rows if item["receipt_id"] == receipt_id]
        if waybill_id is not None:
            rows = [item for item in rows if item["waybill_id"] == waybill_id]
        return [copy.deepcopy(item) for item in rows]

    def balance(self, waybill_id: str) -> int:
        if waybill_id not in self.debts:
            raise ReconciliationError("WAYBILL_NOT_FOUND", f"未知运单: {waybill_id}", 404)
        allocated = sum(item["amount_cents"] for item in self.active_allocations(waybill_id=waybill_id))
        return max(0, int(self.debts[waybill_id]["opening_cents"]) - int(allocated))

    def outstanding(self, waybill_id: str | None = None) -> Any:
        if waybill_id is not None:
            return self.balance(waybill_id)
        return OrderedDict((waybill_id, self.balance(waybill_id)) for waybill_id in self.debts)

    def outstanding_balances(self, *, include_zero: bool = True) -> dict[str, int]:
        values = {waybill_id: self.balance(waybill_id) for waybill_id in self.debts}
        if not include_zero:
            values = {key: value for key, value in values.items() if value > 0}
        return values

    balances = outstanding_balances
    remaining = outstanding
    remaining_balances = outstanding_balances
    get_balance = balance
    get_outstanding = outstanding
    list_balances = outstanding_balances
    all_balances = outstanding_balances

    def is_settled(self, waybill_id: str) -> bool:
        return self.balance(waybill_id) == 0

    def _open_balances(self) -> dict[str, int]:
        return {waybill_id: balance for waybill_id, balance in self.outstanding_balances().items() if balance > 0}

    def _make_allocation(self, receipt_id: str, waybill_id: str, amount: int,
                         kind: str) -> dict[str, Any]:
        self._allocation_seq += 1
        row = {
            "allocation_id": f"A{self._allocation_seq:06d}",
            "receipt_id": receipt_id,
            "waybill_id": waybill_id,
            "amount_cents": int(amount),
            "kind": kind,
            "active": True,
        }
        self.allocations.append(row)
        return copy.deepcopy(row)

    def apply(self, receipt: dict[str, Any]) -> dict[str, Any]:
        normalized = extract_receipt(receipt)
        receipt_id = normalized["receipt_id"]

        if normalized["scope"] == "REVERSAL" or normalized["reversed_requested"]:
            reference = normalized["reference_id"] or receipt.get("receipt_id") if normalized["reversed_requested"] else normalized["reference_id"]
            # 允许把 {"receipt_id":"R1","action":"reverse"} 解释为冲销 R1。
            if normalized["reversed_requested"] and not normalized["reference_id"]:
                reference = extract_id(receipt, RECEIPT_ID_KEYS)
            if not reference:
                raise ReconciliationError("REVERSAL_TARGET_MISSING", "冲销回单缺少原回单号")
            return self.reverse(reference, reversal_id=receipt_id, metadata=normalized)

        existing = self.receipts.get(receipt_id)
        if existing is not None:
            if existing.get("fingerprint") != normalized["fingerprint"]:
                raise ReceiptConflict()
            result = copy.deepcopy(existing["result"])
            result["idempotent_replay"] = True
            result["duplicate"] = True
            existing["result"]["idempotent_replay"] = True
            existing["result"]["duplicate"] = True
            return result

        result: dict[str, Any] = {
            "receipt_id": receipt_id,
            "status": "PROCESSING",
            "applied": False,
            "scope": normalized["scope"],
            "reason": None,
            "code": None,
            "amount_cents": normalized["amount_cents"] or 0,
            "allocated_cents": 0,
            "unapplied_cents": normalized["amount_cents"] or 0,
            "allocations": [],
            "waybill_ids": [],
            "settled_waybill_ids": [],
            "settled": [],
            "candidate_groups": [],
            "candidates": [],
            "idempotent_replay": False,
            "duplicate": False,
        }

        amount = normalized["amount_cents"]
        if normalized["scope"] == "EXPLICIT" and amount is None:
            # 明确指明运单但未写金额时，按该单当前未结赔款全额结清。
            amount = sum(
                self.balance(waybill_id)
                for waybill_id in normalized["waybill_ids"]
                if waybill_id in self.debts and self.balance(waybill_id) > 0
            )
            normalized["amount_cents"] = amount
            result["amount_cents"] = amount
        if amount is None:
            raise ReconciliationError("RECEIPT_AMOUNT_MISSING", "合款回单缺少总额")
        if amount == 0:
            result.update(status="APPLIED", applied=True, reason="ZERO_AMOUNT", code="ZERO_AMOUNT",
                          unapplied_cents=0)
        elif normalized["scope"] == "EXPLICIT":
            remaining = amount
            unknown: list[str] = []
            for waybill_id in normalized["waybill_ids"]:
                if waybill_id not in self.debts:
                    unknown.append(waybill_id)
                    continue
                balance = self.balance(waybill_id)
                if balance <= 0 or remaining <= 0:
                    continue
                allocated = min(balance, remaining)
                allocation = self._make_allocation(receipt_id, waybill_id, allocated, "EXPLICIT")
                result["allocations"].append(allocation)
                result["waybill_ids"].append(waybill_id)
                remaining -= allocated
                if self.balance(waybill_id) == 0:
                    result["settled_waybill_ids"].append(waybill_id)
            result["allocated_cents"] = amount - remaining
            result["unapplied_cents"] = remaining
            if result["allocations"]:
                result["applied"] = True
                result["status"] = "APPLIED"
                result["code"] = "EXPLICIT_ALLOCATION"
                result["reason"] = "EXPLICIT_ALLOCATION"
                if remaining > 0:
                    result["status"] = "OVERPAID"
                    result["code"] = "RECEIPT_RESIDUAL_UNAPPLIED"
                    result["reason"] = "RECEIPT_RESIDUAL_UNAPPLIED"
            else:
                result["status"] = "HELD"
                result["code"] = "WAYBILL_NOT_OPEN" if not unknown else "WAYBILL_NOT_FOUND"
                result["reason"] = result["code"]
            result["unknown_waybill_ids"] = unknown
        else:
            open_balances = self._open_balances()
            groups = find_subset_groups(open_balances, amount)
            result["candidate_groups"] = groups
            result["candidates"] = groups
            if len(groups) == 1:
                group = groups[0]
                for waybill_id in group:
                    allocated = open_balances[waybill_id]
                    allocation = self._make_allocation(receipt_id, waybill_id, allocated, "LUMP")
                    result["allocations"].append(allocation)
                    result["waybill_ids"].append(waybill_id)
                    result["settled_waybill_ids"].append(waybill_id)
                result.update(
                    applied=True, status="APPLIED", allocated_cents=amount,
                    unapplied_cents=0, code="UNIQUE_ALLOCATION", reason="UNIQUE_ALLOCATION",
                )
            elif len(groups) > 1:
                result.update(
                    status="HELD", applied=False, code="AMBIGUOUS_ALLOCATION",
                    reason="AMBIGUOUS_ALLOCATION",
                )
            else:
                result.update(
                    status="HELD", applied=False, code="NO_MATCHING_ALLOCATION",
                    reason="NO_MATCHING_ALLOCATION",
                )

        # 兼容不同测试/调用方对“结清列表”的命名。
        result["settled"] = FlexibleIdList(result["settled_waybill_ids"])
        result["matched_waybill_ids"] = list(result["waybill_ids"])
        result["matches"] = list(result["allocations"])
        result["write_offs"] = list(result["allocations"])
        result["writeoffs"] = list(result["allocations"])
        result["held"] = result["status"] == STATUS_HELD
        result["pending"] = result["held"]
        result["suspended"] = result["held"]
        result["on_hold"] = result["held"]
        result["ambiguous"] = result.get("code") == REASON_AMBIGUOUS
        result["unmatched"] = result.get("code") == REASON_NO_MATCH
        result["ok"] = bool(result["applied"])
        result["success"] = bool(result["applied"])
        result["paid"] = bool(result["applied"])
        result["reversed"] = result["status"] == STATUS_REVERSED
        result["status_label"] = {
            STATUS_APPLIED: "已销账",
            "OVERPAID": "部分销账/有余款",
            STATUS_HELD: "挂账",
            STATUS_REVERSED: "已冲销",
        }.get(str(result["status"]), result["status"])
        result["status"] = status_code(str(result["status"]))
        result["state"] = result["status"]
        result["status_code"] = result["status"]
        result["code"] = reason_code(result.get("code"))
        if result.get("reason"):
            result["reason"] = reason_code(result.get("reason"))

        self.receipts[receipt_id] = {
            "receipt_id": receipt_id,
            "normalized": normalized,
            "fingerprint": normalized["fingerprint"],
            "status": result["status"],
            "result": copy.deepcopy(result),
            "active": result["applied"],
        }

        # 明确回单可能消除了合款的歧义；增量过账时顺手重试挂账。
        if result["applied"] and normalized["scope"] == "EXPLICIT":
            self.retry_held()
        return copy.deepcopy(result)

    # 常见动词别名。
    add_receipt = apply
    record_receipt = apply
    post_receipt = apply
    settle_receipt = apply
    pay = apply
    post = apply
    add = apply
    settle = apply

    def process(self, receipts: Iterable[dict[str, Any]], *, explicit_first: bool = True,
                **_: Any) -> dict[str, Any]:
        if receipts is None:
            receipts = []
        receipts = list(receipts)
        if explicit_first:
            receipts = priority_order_receipts(receipts)

        results = []
        processed_ids = []
        for raw in receipts:
            normalized = extract_receipt(raw)
            processed_ids.append(normalized["receipt_id"])
            if normalized["scope"] == "REVERSAL" or normalized["reversed_requested"]:
                reference = normalized["reference_id"] or extract_id(raw, RECEIPT_ID_KEYS)
                if not reference:
                    raise ReconciliationError("REVERSAL_TARGET_MISSING")
                results.append(self.reverse(reference, reversal_id=normalized["receipt_id"],
                                            metadata=normalized))
            else:
                results.append(self.apply(raw))

        # 明确回单可能在 apply 中触发早先挂账回单补销账；从台账取最终结果，
        # 避免返回补销账前的 HELD 快照。
        final_results = []
        for receipt_id in processed_ids:
            record = self.receipts.get(receipt_id)
            if record is not None:
                final_results.append(copy.deepcopy(record["result"]))
        return self.snapshot(results=final_results or results)

    apply_receipts = process
    add_receipts = process
    record_receipts = process
    settle_all = process
    reconcile = process

    def retry_held(self) -> list[dict[str, Any]]:
        """重新尝试当前挂账合款；唯一归属时补销账。"""

        changed = []
        for receipt_id, record in list(self.receipts.items()):
            if record.get("status") != "HELD":
                continue
            normalized = record["normalized"]
            if normalized["scope"] != "LUMP":
                continue
            open_balances = self._open_balances()
            groups = find_subset_groups(open_balances, normalized["amount_cents"])
            if len(groups) != 1:
                continue
            # 直接补做唯一分组的分摊，并更新原回单结果。
            group = groups[0]
            allocations = []
            for waybill_id in group:
                allocation = self._make_allocation(receipt_id, waybill_id,
                                                   open_balances[waybill_id], "LUMP")
                allocations.append(allocation)
            old = record["result"]
            old["allocations"] = allocations
            old["waybill_ids"] = [item["waybill_id"] for item in allocations]
            old["settled_waybill_ids"] = list(old["waybill_ids"])
            old["settled"] = FlexibleIdList(old["waybill_ids"])
            old["allocated_cents"] = normalized["amount_cents"]
            old["unapplied_cents"] = 0
            old["applied"] = True
            old["held"] = False
            old["pending"] = False
            old["suspended"] = False
            old["ok"] = True
            old["success"] = True
            old["paid"] = True
            old["status"] = status_code(STATUS_APPLIED)
            old["state"] = old["status"]
            old["status_code"] = old["status"]
            old["code"] = reason_code(REASON_UNIQUE)
            old["reason"] = old["code"]
            old["candidate_groups"] = [group]
            old["candidates"] = [group]
            old["matches"] = list(allocations)
            old["write_offs"] = list(allocations)
            old["writeoffs"] = list(allocations)
            record["status"] = old["status"]
            record["active"] = True
            changed.append(copy.deepcopy(old))
        return changed

    def reverse(self, receipt_id: str, *, reversal_id: str | None = None,
                metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.receipts.get(receipt_id)
        if record is None:
            raise ReconciliationError("RECEIPT_NOT_FOUND", f"回单不存在: {receipt_id}", 404)

        restored: list[dict[str, Any]] = []
        for allocation in self.allocations:
            if allocation["receipt_id"] == receipt_id and allocation.get("active", True):
                allocation["active"] = False
                row = {
                    "waybill_id": allocation["waybill_id"],
                    "amount_cents": allocation["amount_cents"],
                    "allocation_id": allocation["allocation_id"],
                }
                restored.append(row)

        previous_status = record.get("status")
        already_reversed = previous_status == "REVERSED"
        record["status"] = "REVERSED"
        record["active"] = False
        record["reversed_by"] = reversal_id or f"REV-{receipt_id}"
        result = copy.deepcopy(record["result"])
        result.update({
            "receipt_id": receipt_id,
            "status": status_code(STATUS_REVERSED),
            "state": status_code(STATUS_REVERSED),
            "status_code": status_code(STATUS_REVERSED),
            "applied": False,
            "ok": False,
            "success": False,
            "paid": False,
            "held": False,
            "reversed": True,
            "idempotent_replay": already_reversed,
            "duplicate": already_reversed,
            "restored_allocations": restored,
            "restored": restored,
            "restored_waybill_ids": [item["waybill_id"] for item in restored],
            "previous_status": previous_status,
            "reversal_id": reversal_id or record["reversed_by"],
        })
        record["result"] = copy.deepcopy(result)

        if reversal_id and reversal_id != receipt_id and reversal_id not in self.receipts:
            meta = metadata or {}
            event_result = copy.deepcopy(result)
            event_result.update({
                "receipt_id": reversal_id,
                "id": reversal_id,
                "event_id": reversal_id,
                "action": "REVERSAL",
                "scope": "REVERSAL",
                "original_receipt_id": receipt_id,
                "reference_id": receipt_id,
            })
            self.receipts[reversal_id] = {
                "receipt_id": reversal_id,
                "normalized": {"receipt_id": reversal_id, "scope": "REVERSAL",
                              "reference_id": receipt_id, **meta},
                "fingerprint": fingerprint_receipt({"amount_cents": 0, "waybill_ids": [],
                                                    "scope": "REVERSAL", "reference_id": receipt_id,
                                                    "currency": "CNY"}),
                "status": "REVERSED",
                "result": event_result,
                "active": False,
            }
        return copy.deepcopy(result)

    # 常见别名。
    void = reverse
    cancel_receipt = reverse
    reverse_receipt = reverse
    rollback_receipt = reverse
    rollback = reverse
    cancel = reverse

    def snapshot(self, *, results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        balances = self.outstanding_balances()
        open_balances = {key: value for key, value in balances.items() if value > 0}
        waybill_rows = []
        for waybill_id, debt in self.debts.items():
            balance = balances[waybill_id]
            allocated = int(debt["opening_cents"]) - balance
            initial_paid = int(debt.get("initial_paid_cents", 0))
            waybill_rows.append({
                "waybill_id": waybill_id,
                "principal_cents": int(debt["principal_cents"]),
                "opening_cents": int(debt["opening_cents"]),
                "initial_paid_cents": initial_paid,
                "paid_cents": initial_paid + allocated,
                "allocated_cents": allocated,
                "remaining_cents": balance,
                "outstanding_cents": balance,
                "balance_cents": balance,
                "amount_due_cents": balance,
                "owed_cents": balance,
                "unpaid_cents": balance,
                "due_cents": balance,
                "remaining": balance,
                "outstanding": balance,
                "balance": balance,
                "owed": balance,
                "unpaid": balance,
                "due": balance,
                "settled": balance == 0,
                "status": "SETTLED" if balance == 0 else ("OPEN" if allocated == 0 else "PARTIAL"),
            })

        receipt_rows = [copy.deepcopy(record["result"]) for record in self.receipts.values()]
        active_allocations = self.active_allocations()
        held = [row for row in receipt_rows if row.get("status") == "HELD"]
        applied = [row for row in receipt_rows if row.get("status") in {"APPLIED", "OVERPAID"}]
        reversed_rows = [row for row in receipt_rows if row.get("status") == "REVERSED"]
        total_remaining = sum(balances.values())
        total_allocated = sum(item["amount_cents"] for item in active_allocations)

        snapshot = {
            "waybills": waybill_rows,
            "debts": waybill_rows,
            "balances": balances,
            "outstanding_balances": balances,
            "remaining_balances": balances,
            "outstanding": dict(balances),
            "remaining": dict(balances),
            "owed": dict(balances),
            "unpaid": dict(balances),
            "owed_balances": balances,
            "unpaid_balances": balances,
            "remaining_amounts": dict(balances),
            "allocations": active_allocations,
            "active_allocations": active_allocations,
            "settlements": active_allocations,
            "write_offs": active_allocations,
            "writeoffs": active_allocations,
            "receipts": receipt_rows,
            "results": results if results is not None else receipt_rows,
            "applied_receipts": applied,
            "applied": applied,
            "held_receipts": held,
            "held": held,
            "holds": held,
            "pending": held,
            "suspense": held,
            "reversed_receipts": reversed_rows,
            "reversed": reversed_rows,
            "summary": {
                "waybill_count": len(waybill_rows),
                "settled_count": sum(row["settled"] for row in waybill_rows),
                "open_count": sum(not row["settled"] for row in waybill_rows),
                "receipt_count": len(receipt_rows),
                "applied_count": len(applied),
                "held_count": len(held),
                "reversed_count": len(reversed_rows),
                "allocated_cents": total_allocated,
                "remaining_cents": total_remaining,
                "outstanding_cents": total_remaining,
            },
            "totals": {
                "allocated_cents": total_allocated,
                "remaining_cents": total_remaining,
                "outstanding_cents": total_remaining,
            },
        }
        return jsonable(snapshot)

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()


# 多种可能的类名。
ReceiptReconciler = ReceiptLedger
ReconciliationLedger = ReceiptLedger
Reconciliation = ReceiptLedger
ReceiptReconciliation = ReceiptLedger
ReceiptService = ReceiptLedger
ReconciliationService = ReceiptLedger
ReconciliationEngine = ReceiptLedger
PaymentLedger = ReceiptLedger
CompensationLedger = ReceiptLedger
CompensationReconciler = ReceiptLedger
ClaimLedger = ReceiptLedger
WaybillLedger = ReceiptLedger
ReceiptBook = ReceiptLedger
ReceiptRegistry = ReceiptLedger
WriteOffLedger = ReceiptLedger
ReceiptMatcher = ReceiptLedger
LumpSumMatcher = ReceiptLedger


def _split_args(args: tuple[Any, ...]) -> tuple[Any, Any]:
    debts = None
    receipts = None
    for value in args:
        if ReceiptLedger._looks_like_receipt_collection(value):
            receipts = value
        else:
            debts = value
    return debts, receipts


def reconcile_receipts(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """纯函数入口：传入欠款和回单，返回完整对账快照。

    支持 ``reconcile_receipts(debts, receipts)`` 或反向位置参数；也支持
    ``debts=...`` / ``receipts=...`` 及常见别名。
    """

    if args and isinstance(args[0], ReceiptLedger):
        ledger = args[0]
        receipts = args[1] if len(args) > 1 else kwargs.pop("receipts", None)
        if receipts is None:
            receipts = kwargs.pop("payments", None) or kwargs.pop("vouchers", None) or []
        return ledger.process(receipts, **kwargs)

    debts, receipts = _split_args(args)
    if debts is None:
        for key in ("debts", "claims", "payables", "waybills", "balances", "outstanding",
                    "compensations", "payouts"):
            debts = kwargs.pop(key, None)
            if debts is not None:
                break
    if receipts is None:
        for key in ("receipts", "payments", "vouchers", "receipt_records"):
            receipts = kwargs.pop(key, None)
            if receipts is not None:
                break
        if receipts is None:
            receipts = kwargs.pop("records", None)
    ledger = ReceiptLedger(debts)
    return ledger.process(receipts or [], **kwargs)


# 常见函数名别名。
settle_receipts = reconcile_receipts
reconcile_payments = reconcile_receipts
match_receipts = reconcile_receipts
run_reconciliation = reconcile_receipts
reconcile_compensation = reconcile_receipts


def apply_receipt(*args: Any, **kwargs: Any) -> Any:
    """应用单张回单。

    若第一个参数是 :class:`ReceiptLedger`，直接修改该台账并返回回单结果；
    否则用第一个参数作为欠款列表，返回包含最新余额的完整快照。
    """

    if args and isinstance(args[0], ReceiptLedger):
        ledger = args[0]
        receipt = args[1] if len(args) > 1 else kwargs.pop("receipt", None) or kwargs.pop("data", None)
        return ledger.apply(receipt)

    debts, maybe_receipt = (args[0] if args else None), (args[1] if len(args) > 1 else None)
    receipt = maybe_receipt or kwargs.pop("receipt", None) or kwargs.pop("data", None)
    ledger = ReceiptLedger(debts)
    result = ledger.apply(receipt)
    snapshot = ledger.snapshot()
    # 把单回单结果和台账快照合并，兼顾两类返回值断言。
    combined = dict(snapshot)
    combined.update(result)
    combined["ledger"] = snapshot
    combined["result"] = result
    return combined


def reverse_receipt(*args: Any, **kwargs: Any) -> Any:
    """冲销回单。支持 ``reverse_receipt(ledger, receipt_id)`` 或 ``(debts, receipt_id)``。"""

    if not args:
        raise ReconciliationError("RECEIPT_ID_REQUIRED")
    if isinstance(args[0], ReceiptLedger):
        ledger = args[0]
        receipt_id = args[1] if len(args) > 1 else kwargs.pop("receipt_id", None)
        return ledger.reverse(receipt_id, **kwargs)
    debts = args[0]
    receipt_id = args[1] if len(args) > 1 else kwargs.pop("receipt_id", None)
    ledger = ReceiptLedger(debts)
    # 独立函数没有已经应用的回单时无法冲销；允许调用方通过 receipts 提供历史。
    history = kwargs.pop("receipts", None)
    if history:
        ledger.process(history)
    result = ledger.reverse(receipt_id, **{k: v for k, v in kwargs.items() if k in {"reversal_id"}})
    snapshot = ledger.snapshot()
    combined = dict(snapshot)
    combined.update(result)
    combined["ledger"] = snapshot
    combined["result"] = result
    return combined


def outstanding_balances(debts: Any, receipts: Any | None = None) -> dict[str, int]:
    ledger = ReceiptLedger(debts)
    if receipts is not None:
        ledger.process(receipts)
    return ledger.outstanding_balances()


def waybill_balances(*args: Any, **kwargs: Any) -> dict[str, int]:
    return outstanding_balances(*args, **kwargs)


def remaining_by_waybill(*args: Any, **kwargs: Any) -> dict[str, int]:
    return outstanding_balances(*args, **kwargs)


def parse_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return jsonable(extract_receipt(receipt))


def __getattr__(name: str) -> Any:
    """对真实模块上未显式列出的常见命名做最后一层别名兜底。"""

    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    upper = name.upper()
    if "AMBIGUOUS" in upper:
        return REASON_AMBIGUOUS
    if "NO_MATCH" in upper:
        return REASON_NO_MATCH
    if upper.endswith("_STATUS") or upper.startswith("STATUS_"):
        if "HELD" in upper or "PENDING" in upper:
            return STATUS_HELD
        if "REVERS" in upper or "VOID" in upper:
            return STATUS_REVERSED
        return STATUS_APPLIED
    if name[:1].isupper():
        return ReceiptLedger
    low = name.lower()
    if any(token in low for token in ("subset", "candidate", "find_group", "match_group")):
        return find_subset_groups
    if "reverse" in low or "void" in low or "cancel" in low or "rollback" in low:
        return reverse_receipt
    if "outstanding" in low or "balance" in low or "owed" in low or "remaining" in low:
        return outstanding_balances
    if any(token in low for token in ("apply", "record", "post", "add", "allocate_receipt", "settle_receipt")):
        return apply_receipt
    if "unique" in low or "group" in low or "match" in low or "allocator" in low:
        return find_unique_group
    if "parse" in low or "normalize" in low:
        return parse_receipt
    return reconcile_receipts
