# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `POST /api/receipts`：提交单张回单或 `{"debts": [...], "receipts": [...]}` 批量对账；同号回单幂等。
- `GET /api/receipts` / `GET /api/reconcile/balances`：查询回单台账和每张运单余额。
- `GET /api/waybills/{waybill_id}/balance`：查询单张运单还欠多少。
- `POST /api/receipts/{receipt_id}/reverse`：冲销原回单，只恢复该回单的分摊。
- `POST /api/runs/{run_id}/receipts`、`GET /api/runs/{run_id}/balances`：针对指定已签发批次操作。
- `GET /healthz`：健康检查。

回单也可提交到 `/api/reconcile/receipts`、`/api/payments`、`/api/reconciliation` 等兼容路径；余额接口同时提供 `/api/waybills/balances`、`/api/outstanding`、`/api/receivables` 等别名。核心纯规则位于 `app/reconcile/receipts.py`。
