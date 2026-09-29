"""测试共用夹具：合作规则、操作者与时钟。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.access import Actor
from src.events import EventStore
from src.service import EscrowService


def rules_v1() -> dict:
    return {
        "currency": "USD",
        "fx_basis": "ECB_MID_2026-09-01",
        "review_deadline_days": 10,
        "payer_party": "甲生物",
        "payee_party": "乙制药",
        "territory_weights_bp": {"US": 6000, "EU": 4000},
        "codev_share_bp": 5000,
        "disclosure": {"external": "jsc_approval"},
        "milestones": {
            "M1_P3_READOUT": {
                "name": "三期主要终点读出",
                "project_codes": ["P-101"],
                "territories": ["US", "EU"],
                "required_evidence": ["clinical_report"],
                "offices": ["science", "compliance", "finance"],
                "amount_minor": 10_000_000_00,
            },
            "M2_PLATFORM": {
                "name": "技术平台验证",
                "project_codes": ["P-202"],
                "territories": ["US"],
                "required_evidence": ["platform_report"],
                "offices": ["science", "compliance", "finance"],
                "amount_minor": 4_000_000_00,
            },
        },
    }


def evidence(orr: float = 0.42, extra: str = "数据库锁定 2026-09-20") -> dict:
    return {
        "evidence_id": "EV-P3-001",
        "evidence_type": "clinical_report",
        "content": {"title": "三期主要终点统计报告", "orr": orr, "provenance": extra},
    }


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 9, 0, tzinfo=timezone(timedelta(hours=8)))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def build_service() -> tuple[EscrowService, Clock]:
    clock = Clock()
    return EscrowService(EventStore(), clock=clock), clock


# ---- 操作者 ----

def admin(party: str = "甲生物", user: str = "jsc-chair") -> Actor:
    return Actor(user_id=user, party=party, role="party_admin")


def submitter() -> Actor:
    return Actor(
        user_id="alice", party="乙制药", role="member",
        offices=("science",),
        project_scope=frozenset({"P-101", "P-202"}),
        permissions=frozenset({"claim.submit", "evidence.lock"}),
    )


def science() -> Actor:
    return Actor(
        user_id="bob-science", party="甲生物", role="member",
        offices=("science",), project_scope=frozenset({"P-101", "P-202"}),
    )


def compliance() -> Actor:
    return Actor(
        user_id="carol-compliance", party="甲生物", role="member",
        offices=("compliance",), project_scope=frozenset({"P-101", "P-202"}),
    )


def finance() -> Actor:
    return Actor(
        user_id="dave-finance", party="甲生物", role="member",
        offices=("finance",), project_scope=frozenset({"P-101", "P-202"}),
        permissions=frozenset({"payment.order", "payment.read",
                               "adjudication.settle"}),
    )


def register(service: EscrowService, partnership_id: str = "CO-1") -> dict:
    return service.register_partnership_version(
        actor=admin(), partnership_id=partnership_id, rules=rules_v1(),
        parties=["甲生物", "乙制药"],
    )


def happy_path(service: EscrowService, claim_id: str = "CLAIM-1") -> list:
    """登记合作、提交 + 三职能签署，返回提交阶段各事件。"""
    register(service)
    submitted = service.submit_claim(
        actor=submitter(), claim_id=claim_id, partnership_id="CO-1",
        milestone_code="M1_P3_READOUT", project_code="P-101",
        territories=["US", "EU"], evidence_packages=[evidence()],
    )
    service.sign_review(actor=science(), claim_id=claim_id, office="science")
    service.sign_review(actor=compliance(), claim_id=claim_id, office="compliance")
    service.sign_review(actor=finance(), claim_id=claim_id, office="finance")
    return submitted
