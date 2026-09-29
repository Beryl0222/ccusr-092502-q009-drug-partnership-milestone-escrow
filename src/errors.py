"""托管领域的错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域规则冲突的基类。"""


class ValidationError(DomainError):
    """登记内容不满足合同结构。"""


class ConflictError(DomainError):
    """操作与既有事实冲突，例如重复标识不同内容。"""


class AuthorizationError(DomainError):
    """成员不具备该签署权，或提交者试图批准自己的材料。"""


class WorkflowError(DomainError):
    """当前状态不允许该操作，例如证据或签署尚未齐备。"""
