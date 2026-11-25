# 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| `waybill_id` | text | 主键 |
| `pickup_at` | datetime | 业务字段 |
| `deadline_at` | datetime | 业务字段 |
| `delivered_at` | datetime | 业务字段 |
| `paused_hours` | int | 业务字段 |
| `declared_value_cents` | int | 业务字段 |
| `invoice_cents` | int | 业务字段 |

空字符串、`-`、`NA`、`null` 和 `未提供` 按空值处理。金额字段在整理层转为整数分或整数最小单位；日期按 ISO 日期解析；带时间的字段按本地时间解析。标识字段保持为文本，前导零不会被去除。

## 回单对账

| 字段 | 类型 | 说明 |
|---|---|---|
| `receipt_id` | text | 回单唯一号；重复提交按幂等重放处理 |
| `waybill_id` / `waybill_ids` | text / list[text] | 明确回单指明的运单；为空时按合款总额匹配 |
| `amount_cents` | int | 回单金额（最小单位）；普通 `amount`/`total` 小数按元转分，整数视为同一最小单位 |
| `original_receipt_id` / `reverses` | text | 冲销事件指向的原回单号 |
| `remaining_cents` / `outstanding_cents` | int | 运单当前还欠金额 |
| `AMBIGUOUS_ALLOCATION` | code | 合款可匹配多组运单，已挂账且未销账 |
| `NO_MATCHING_ALLOCATION` | code | 合款未匹配到任何未结欠款子集，已挂账 |
