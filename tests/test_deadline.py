"""审阅期限、跨时区/计费周期与冻结规则版本。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from fixtures import (
    admin, build_service, compliance, evidence, finance, happy_path,
    rules_v1, science, submitter,
)
from src.domain import Claim, Partnership
from src.errors import DomainError


def rules_v2(*, deadline_days: int = 30) -> dict:
    rules = rules_v1()
    rules["review_deadline_days"] = deadline_days  # 新版本放宽期限
    return rules


class DeadlineAndFrozenRulesTest(unittest.TestCase):
    def test_review_uses_frozen_deadline_across_timezone_and_billing_cycle(self) -> None:
        service, clock = build_service()
        happy_path(service, "CLAIM-1")
        claim = service._claim("CLAIM-1")
        partnership = service._partnership("CO-1")
        deadline = claim.deadline_at(partnership, 1)
        # 证据锁于 2026-09-24 09:00 +08:00，10 天后截止
        self.assertEqual(deadline.isoformat(), "2026-10-04T01:00:00+00:00")
        # 在另一时区（纽约）看截止时刻是同一绝对瞬间
        ny = deadline.astimezone(timezone(timedelta(hours=-4)))
        self.assertEqual(ny.isoformat(), "2026-10-03T21:00:00-04:00")
        # 跨越月度计费周期（10 月 1 日）登记新版本，期限不变
        clock.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v2(),
        )
        self.assertEqual(claim.deadline_at(partnership, 1), deadline)

    def test_signature_after_deadline_is_rejected_and_timeout_decides_failure(self) -> None:
        service, clock = build_service()
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v1(),
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-T", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        service.sign_review(actor=science(), claim_id="CLAIM-T", office="science")
        # 越过 10 天期限
        clock.advance(days=11)
        with self.assertRaisesRegex(DomainError, "期限已过"):
            service.sign_review(actor=compliance(), claim_id="CLAIM-T",
                                office="compliance")
        expired = service.expire_due()
        self.assertEqual(len(expired), 1)
        claim = service._claim("CLAIM-T")
        self.assertEqual(claim.latest_decision["outcome"], "not_achieved")
        self.assertEqual(claim.latest_decision["reason_code"], "review_timeout")
        # 巡检幂等：不会重复产生裁决
        self.assertEqual(service.expire_due(), [])

    def test_deadline_does_not_fire_early(self) -> None:
        service, clock = build_service()
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v1(),
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-E", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        clock.advance(days=9)
        self.assertEqual(service.expire_due(), [])
        service.sign_review(actor=science(), claim_id="CLAIM-E", office="science")
        service.sign_review(actor=compliance(), claim_id="CLAIM-E",
                            office="compliance")
        service.sign_review(actor=finance(), claim_id="CLAIM-E", office="finance")
        self.assertEqual(service._claim("CLAIM-E").latest_decision["outcome"],
                         "achieved")


if __name__ == "__main__":
    unittest.main()
