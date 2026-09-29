"""里程碑金额与币种口径。

金额一律使用整数最小单位记账，禁止浮点进入差额计算；
币种口径（fx_basis）冻结在合作版本中，跨计费周期不变。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import DomainError


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str
    fx_basis: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise DomainError("金额最小单位必须是整数")
        if not self.currency or not self.fx_basis:
            raise DomainError("币种与口径不能为空")

    @property
    def is_zero(self) -> bool:
        return self.amount_minor == 0

    def plus(self, other: "Money") -> "Money":
        self._assert_same_basis(other)
        return Money(self.amount_minor + other.amount_minor, self.currency, self.fx_basis)

    def minus(self, other: "Money") -> "Money":
        """允许为负，差额本身有方向，调用方负责解释。"""
        self._assert_same_basis(other)
        return Money(self.amount_minor - other.amount_minor, self.currency, self.fx_basis)

    def scale_bp(self, factor_bp: int) -> "Money":
        """按基点（万分之一）缩放，余数向下取整，用于共同开发分成变化。"""
        return Money(
            self.amount_minor * factor_bp // 10_000, self.currency, self.fx_basis
        )

    def negated(self) -> "Money":
        return Money(-self.amount_minor, self.currency, self.fx_basis)

    def _assert_same_basis(self, other: "Money") -> None:
        if self.currency != other.currency or self.fx_basis != other.fx_basis:
            raise DomainError(
                f"币种口径不一致: {self.currency}/{self.fx_basis} vs "
                f"{other.currency}/{other.fx_basis}"
            )


ZERO_CACHE: dict[tuple[str, str], Money] = {}


def zero_money(currency: str, fx_basis: str) -> Money:
    key = (currency, fx_basis)
    if key not in ZERO_CACHE:
        ZERO_CACHE[key] = Money(0, currency, fx_basis)
    return ZERO_CACHE[key]


def sum_money(items: list[Money], currency: str, fx_basis: str) -> Money:
    total = zero_money(currency, fx_basis)
    for item in items:
        total = total.plus(item)
    return total
