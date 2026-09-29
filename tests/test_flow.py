"""候选达成、独立签署与付款指令主链路。"""
from __future__ import annotations

import unittest

from fixtures import (
    admin, compliance, evidence, finance, happy_path, rules_v1,
    science, submitter, build_service,
)
from src.errors import DomainError, PermissionDenied


class HappyPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = build_service()
        happy_path(self.service)

    def test_submission_is_only_candidate_before_signatures(self) -> None:
        service, _ = build_service()
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v1(),
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-X", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        with self.assertRaisesRegex(DomainError, "尚未裁决"):
            service.order_payment(actor=finance(), claim_id="CLAIM-X",
                                  payment_id="PAY-X")
        [item] = service.list_visible_claims(actor=submitter())
        self.assertEqual(item["state"], "awaiting_review")
        self.assertIsNone(item["outcome"])

    def test_achieved_amount_uses_territory_weight_and_codev_share(self) -> None:
        # US 6000 + EU 4000 = 10000，共同开发分成 5000bp -> 应付 50%
        payment = self.service.order_payment(
            actor=finance(), claim_id="CLAIM-1", payment_id="PAY-1",
        )
        self.assertEqual(payment["payload"]["amount_minor"], 5_000_000_00)
        self.assertEqual(payment["payload"]["currency"], "USD")
        self.assertEqual(payment["payload"]["fx_basis"], "ECB_MID_2026-09-01")

    def test_trace_from_payment_links_rights_evidence_signatures(self) -> None:
        self.service.order_payment(
            actor=finance(), claim_id="CLAIM-1", payment_id="PAY-1",
        )
        trace = self.service.trace_from_payment(
            payment_id="PAY-1", actor=finance(),
        )
        self.assertEqual(trace["entry_point"]["payment_id"], "PAY-1")
        self.assertEqual(trace["rights"]["rules_versions_used"][0]["rules_version"], 1)
        self.assertEqual(trace["evidence"][0]["fingerprint"][:7], "sha256:")
        offices = {s["office"] for s in trace["rounds"][0]["signatures"]}
        self.assertEqual(offices, {"science", "compliance", "finance"})
        self.assertEqual(trace["payments"][0]["event_id"], "PAY-1:1")

    def test_trace_from_failed_milestone(self) -> None:
        service, _ = build_service()
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v1(),
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-F", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        service.sign_review(actor=science(), claim_id="CLAIM-F", office="science")
        service.object_review(
            actor=compliance(), claim_id="CLAIM-F", office="compliance",
            reason="数据采集偏离 GCP",
        )
        trace = service.trace_from_failed_milestone(
            claim_id="CLAIM-F", actor=finance(),
        )
        self.assertEqual(trace["rounds"][-1]["outcome"], "not_achieved")
        self.assertEqual(trace["rounds"][-1]["reason_code"], "objection")
        with self.assertRaises(DomainError):
            service.order_payment(actor=finance(), claim_id="CLAIM-F",
                                  payment_id="PAY-F")

    def test_submitter_cannot_approve_own_material(self) -> None:
        service, _ = build_service()
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v1(),
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-S", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        with self.assertRaisesRegex(DomainError, "不能批准自己"):
            service.sign_review(actor=submitter(), claim_id="CLAIM-S",
                                office="science")

    def test_office_outside_contract_scope_is_rejected(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.service.sign_review(
                actor=submitter(), claim_id="CLAIM-1", office="compliance",
            )

    def test_missing_office_signature_keeps_candidate_open(self) -> None:
        service, _ = build_service()
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v1(),
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-O", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        service.sign_review(actor=science(), claim_id="CLAIM-O", office="science")
        service.sign_review(actor=compliance(), claim_id="CLAIM-O",
                            office="compliance")
        claim = service._claim("CLAIM-O")
        self.assertFalse(claim.is_decided)


if __name__ == "__main__":
    unittest.main()
