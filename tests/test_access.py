"""职责范围、敏感管线脱敏与对外披露限制。"""
from __future__ import annotations

import unittest

from fixtures import (
    admin, build_service, compliance, evidence, finance, happy_path,
    rules_v1, science, submitter,
)
from src.access import Actor
from src.errors import PermissionDenied


def member(office: str, projects: frozenset[str], perms: frozenset[str] = frozenset()):
    return Actor(
        user_id=f"member-{office}", party="甲生物", role="member",
        offices=(office,), project_scope=projects, permissions=perms,
    )


class AccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, _ = build_service()
        happy_path(self.service, "CLAIM-1")
        self.service.order_payment(
            actor=finance(), claim_id="CLAIM-1", payment_id="PAY-1",
        )

    def test_member_cannot_see_claims_outside_project_scope(self) -> None:
        outsider = member("science", frozenset({"P-999"}))
        self.assertEqual(self.service.list_visible_claims(actor=outsider), [])
        with self.assertRaises(PermissionDenied):
            self.service.trace_claim(claim_id="CLAIM-1", actor=outsider)

    def test_non_finance_member_sees_pipeline_but_money_is_masked(self) -> None:
        scientist = member("science", frozenset({"P-101"}))
        trace = self.service.trace_claim(claim_id="CLAIM-1", actor=scientist)
        # 管线与签署事实可见
        self.assertEqual(trace["project_code"], "P-101")
        self.assertEqual(len(trace["rounds"][0]["signatures"]), 3)
        # 金额/币种口径被掩码
        self.assertIn("***", str(trace["rounds"][0]["payable_amount_minor"]))
        self.assertIn("***", str(trace["payments"][0]["amount_minor"]))
        # 财务看得到真实金额
        finance_view = self.service.trace_claim(
            claim_id="CLAIM-1", actor=member(
                "finance", frozenset({"P-101"}), frozenset({"payment.read"})),
        )
        self.assertEqual(
            finance_view["rounds"][0]["payable_amount_minor"], 5_000_000_00
        )

    def test_member_cannot_register_versions_or_order_payment(self) -> None:
        scientist = member("science", frozenset({"P-101"}))
        with self.assertRaises(PermissionDenied):
            self.service.register_partnership_version(
                actor=scientist, partnership_id="CO-9", rules=rules_v1(),
                parties=["甲生物", "乙制药"],
            )
        with self.assertRaises(PermissionDenied):
            self.service.order_payment(
                actor=scientist, claim_id="CLAIM-1", payment_id="PAY-X",
            )

    def test_jsc_approval_required_for_disclosure(self) -> None:
        policy = self.service.disclosure_policy(claim_id="CLAIM-1")
        self.assertEqual(policy["policy"], "jsc_approval")
        with self.assertRaises(PermissionDenied):
            self.service.assert_external_disclosure(
                actor=member("science", frozenset({"P-101"}),
                             frozenset({"disclosure.request"})),
                claim_id="CLAIM-1",
            )
        # 附委员会批准记录、由双方代表放行
        self.service.assert_external_disclosure(
            actor=admin(), claim_id="CLAIM-1", jsc_approved=True,
        )

    def test_prohibited_disclosure_is_blocked_even_for_admin(self) -> None:
        service, _ = build_service()
        rules = rules_v1()
        rules["disclosure"] = {"external": "prohibited"}
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-P", rules=rules,
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-P", partnership_id="CO-P",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        service.sign_review(actor=science(), claim_id="CLAIM-P", office="science")
        service.sign_review(actor=compliance(), claim_id="CLAIM-P",
                            office="compliance")
        service.sign_review(actor=finance(), claim_id="CLAIM-P", office="finance")
        with self.assertRaises(PermissionDenied):
            service.assert_external_disclosure(
                actor=admin(), claim_id="CLAIM-P", jsc_approved=True,
            )


if __name__ == "__main__":
    unittest.main()
