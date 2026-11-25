"""物流时效赔付台应用包。

导入时安装轻量命名兼容层；真实模块始终优先，兼容层只兜底缺失的回单/对账
同义模块名。
"""

try:  # pragma: no cover - 兼容层异常不能影响主应用
    from app._compat import install as _install_compat

    _install_compat()
except Exception:  # noqa: BLE001
    pass
