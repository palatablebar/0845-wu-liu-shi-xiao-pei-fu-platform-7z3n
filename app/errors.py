from flask import jsonify


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    "RECEIPT_REQUIRED": "缺少回单内容",
    "RECEIPT_NOT_FOUND": "回单不存在",
    "RECEIPT_CONFLICT": "同一回单号提交了不一致的内容",
    "RECEIPT_ID_REQUIRED": "缺少原回单号",
    "REVERSAL_TARGET_MISSING": "冲销回单缺少原回单号",
    "WAYBILL_NOT_FOUND": "运单不存在",
    "WAYBILL_ID_MISSING": "运单号为空",
    "DEBT_AMOUNT_MISSING": "运单欠款缺少金额",
    "RECEIPT_AMOUNT_MISSING": "回单缺少金额",
    "BAD_AMOUNT": "金额无法识别",
}


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None):
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(code)


def error_response(exc: DomainIssue):
    return jsonify(error=exc.detail or MESSAGES.get(exc.code, "请求无法处理"), code=exc.code), exc.status
