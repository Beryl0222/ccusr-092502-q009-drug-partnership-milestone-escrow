"""领域错误。

所有违反合同规则的操作抛出 DomainError 的子类，
事件存储本身的并发与幂等问题使用 StoreError。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则被违反。"""


class StoreError(Exception):
    """事件流追加被拒绝（版本冲突或事件编号重复）。"""


class PermissionDenied(DomainError):
    """操作者无权执行该命令，或材料涉及超出其职责的范围。"""


class ConflictError(DomainError):
    """同编号材料内容不一致等需要进入争议的冲突。"""
