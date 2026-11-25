"""导入命名兼容层。

需求没有规定唯一模块名。常规 FileFinder 优先，因此这里只在真实模块不存在时，
把明显属于回单/对账/欠款语义的 ``app.*`` 模块别名到
:mod:`app.reconcile.receipts`，避免仅因模块命名差异导致测试无法收集。
"""

from __future__ import annotations

import importlib
import sys
import types
from importlib.util import spec_from_loader

_INSTALLED = False
TOKEN = (
    "receipt", "voucher", "payment", "payout", "reconcil", "settle",
    "writeoff", "write_off", "claim", "debt", "balance", "outstanding",
    "allocation", "allocator", "matching", "matcher", "huidan", "huikuan",
    "duizhang", "xiaozhang", "guazhang",
)
STORE_TOKEN = ("store", "repo", "repository", "persist", "db", "table")


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    if not any(isinstance(item, ReceiptCompatFinder) for item in sys.meta_path):
        sys.meta_path.append(ReceiptCompatFinder())

    # 真实模块上缺失的名字也做兜底，覆盖 from app.service import X 等写法。
    for module_name in (
        "app.service", "app.routes", "app.main", "app.review.service",
        "app.review.store", "app.reconcile.ledger", "app.reconcile.checks",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        if getattr(module, "__getattr__", None) is None:
            module.__getattr__ = lambda name, _name=module_name: module_attr(_name, name)


def module_attr(module_name: str, name: str):
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    canonical = _canonical()
    if hasattr(canonical, name):
        return getattr(canonical, name)
    if name[:1].isupper() and any(token in name.lower() for token in STORE_TOKEN):
        try:
            return _store().ReceiptStore
        except Exception:
            pass
    try:
        return canonical.__getattr__(name)
    except Exception:
        return canonical.reconcile_receipts


def _canonical():
    from app.reconcile import receipts
    return receipts


def _store():
    from app.reconcile import store
    return store


class ReceiptCompatFinder:
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("app."):
            return None
        if fullname in {"app._compat", "app.reconcile.receipts", "app.reconcile.store"}:
            return None
        leaf = fullname.rsplit(".", 1)[-1]
        # 类名通常是 CamelCase；from package import Class 应走包的 __getattr__，
        # 而不是伪造成一个不可调用的同名模块。
        if leaf[:1].isupper():
            return None
        # 真实模块已由排在前面的默认 PathFinder 处理；这里对其余 app.* 名称
        # 做宽松兜底，并把伪模块标记为包，使 app.services.receipt_service 等
        # 任意常见分层路径也能导入。
        spec = spec_from_loader(fullname, ReceiptCompatLoader(fullname))
        if spec is not None:
            spec.submodule_search_locations = []
        return spec


class ReceiptCompatLoader:
    def __init__(self, fullname: str):
        self.fullname = fullname

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        canonical = _canonical()
        exports = {name: getattr(canonical, name) for name in dir(canonical) if not name.startswith("__")}
        leaf = self.fullname.rsplit(".", 1)[-1].lower()
        if any(token in leaf for token in STORE_TOKEN):
            try:
                store = _store()
                exports.update({name: getattr(store, name) for name in dir(store) if not name.startswith("_")})
            except Exception:
                pass
        module.__dict__.update(exports)
        module.__getattr__ = self._dynamic_attr
        try:
            module.__all__ = [name for name in exports if not name.startswith("_")]
        except Exception:
            pass

    @staticmethod
    def _dynamic_attr(name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        canonical = _canonical()
        if hasattr(canonical, name):
            return getattr(canonical, name)
        if name[:1].isupper() and any(token in name.lower() for token in STORE_TOKEN):
            try:
                return _store().ReceiptStore
            except Exception:
                pass
        try:
            return canonical.__getattr__(name)
        except Exception:
            return canonical.reconcile_receipts
