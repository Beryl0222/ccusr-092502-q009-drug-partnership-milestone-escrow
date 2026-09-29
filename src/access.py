"""操作者身份、职责范围与敏感管线资料投影。

- 授权双方代表（party_admin）可见全部合作并可发起合作变更与新裁决；
- 普通成员只能看到职责项目范围内的候选，金额与币种口径仅财务可见；
- 各签署职能只承担本职能的签署，职能绑定在操作者身上而非命令参数里。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import PermissionDenied

SENSITIVE_MONEY_KEYS = (
    "gross_amount_minor", "payable_amount_minor", "amount_minor",
    "prior_paid_amount_minor", "new_payable_amount_minor", "delta_amount_minor",
    "currency", "fx_basis",
)


@dataclass(frozen=True)
class Actor:
    user_id: str
    party: str
    role: str = "member"  # party_admin / member
    offices: tuple[str, ...] = ()
    project_scope: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_party_admin(self) -> bool:
        return self.role == "party_admin"

    def require_admin(self) -> None:
        if not self.is_party_admin:
            raise PermissionDenied("仅授权双方代表可执行该操作")

    def require_permission(self, permission: str) -> None:
        if not self.is_party_admin and permission not in self.permissions:
            raise PermissionDenied(f"缺少权限: {permission}")

    def require_office(self, office: str) -> None:
        if office not in self.offices:
            raise PermissionDenied(f"操作者不承担 {office} 签署职能")

    def can_see_project(self, project_code: str) -> bool:
        return self.is_party_admin or project_code in self.project_scope


_MONEY_MASK = "***（超出职责：金额仅财务可见）"


def redact(value, actor: Actor):
    """按职责裁剪追溯视图；不改动任何底层事实。"""
    if actor.is_party_admin:
        return value
    sees_money = "finance" in actor.offices or "payment.read" in actor.permissions
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "project_code" and isinstance(item, str) and not actor.can_see_project(item):
                result[key] = "***（超出职责项目）"
                continue
            if key in SENSITIVE_MONEY_KEYS and not sees_money:
                result[key] = _MONEY_MASK
                continue
            result[key] = redact(item, actor)
        return result
    if isinstance(value, list):
        return [redact(item, actor) for item in value]
    if isinstance(value, tuple):
        return [redact(item, actor) for item in value]
    return value
