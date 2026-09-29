"""新裁决：数据纠正、区域退出、共同开发选择权变化与差额处理。"""
from __future__ import annotations

import copy
import unittest

from fixtures import (
    admin, build_service, compliance, evidence, finance, happy_path,
    rules_v1, science, submitter,
)
from src.access import Actor
from src.errors import DomainError


def correction_submitter() -> Actor:
    return Actor(
        user_id="erin-cra", party="乙制药", role="member",
        offices=(), project_scope=frozenset({"P-101", "P-202"}),
        permissions=frozenset({"claim.submit", "evidence.lock",
                               "adjudication.request"}),
    )


def rules_v2() -> dict:
    rules = copy.deepcopy(rules_v1())
    rules["codev_share_bp"] = 7000  # 选择权行使后分成提高
    return rules


def sign_round(service, claim_id) -> None:
    service.sign_review(actor=science(), claim_id=claim_id, office="science")
    service.sign_review(actor=compliance(), claim_id=claim_id, office="compliance")
    service.sign_review(actor=finance(), claim_id=claim_id, office="finance")


class AdjustmentTest(unittest.TestCase):
    def _achieved_with_payment(self, claim_id: str, payment_id: str):
        service, clock = build_service()
        happy_path(service, claim_id)
        service.order_payment(actor=finance(), claim_id=claim_id,
                              payment_id=payment_id)
        return service, clock

    def test_territory_exit_creates_clawback_without_rewriting_payment(self) -> None:
        service, _ = self._achieved_with_payment("CLAIM-1", "PAY-1")
        original = service._payment("PAY-1")
        self.assertEqual(original["amount_minor"], 5_000_000_00)

        result = service.request_adjudication(
            actor=admin(), claim_id="CLAIM-1", adjudication_id="ADJ-EXIT",
            reason="territory_exit", territories=["US"],
        )
        self.assertEqual(result["reopened"]["event_type"], "CLAIM_REOPENED")
        # 仅退出地区、无新证据：沿用第一轮证据，立即进入签署窗口
        sign_round(service, "CLAIM-1")
        decision2 = service._claim("CLAIM-1").latest_decision
        # US 权重 6000 × 分成 5000bp = 3000bp -> 应付 30%
        self.assertEqual(decision2["payable_amount_minor"], 3_000_000_00)
        # 负差额不得发补付
        with self.assertRaisesRegex(DomainError, "负差额"):
            service.settle_adjudication(
                actor=finance(), adjudication_id="ADJ-EXIT",
                adjustment_payment_id="PAY-BAD",
            )
        settled = service.settle_adjudication(
            actor=finance(), adjudication_id="ADJ-EXIT",
        )
        payload = settled["payload"]
        self.assertEqual(payload["direction"], "clawback")
        self.assertEqual(payload["delta_amount_minor"], -2_000_000_00)
        self.assertEqual(payload["prior_paid_amount_minor"], 5_000_000_00)
        self.assertEqual(payload["new_payable_amount_minor"], 3_000_000_00)
        # 原付款事实原样保留，反向核对可见两轮
        self.assertEqual(service._payment("PAY-1")["amount_minor"], 5_000_000_00)
        trace = service.trace_from_payment(payment_id="PAY-1", actor=finance())
        self.assertEqual([r["outcome"] for r in trace["rounds"]],
                         ["achieved", "achieved"])
        self.assertEqual([r["territories"] for r in trace["rounds"]],
                         [["US", "EU"], ["US"]])
        self.assertEqual(trace["adjudications"][0]["settlement"]["direction"],
                         "clawback")

    def test_codevelopment_change_issues_top_up_payment(self) -> None:
        service, _ = self._achieved_with_payment("CLAIM-1", "PAY-1")
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v2(),
        )
        service.request_adjudication(
            actor=admin(), claim_id="CLAIM-1", adjudication_id="ADJ-CODEV",
            reason="co_development_change", new_rules_version=2,
            codev_share_bp=7000,
        )
        sign_round(service, "CLAIM-1")
        # 10000 × 7000bp = 7000bp -> 应付 70%
        self.assertEqual(
            service._claim("CLAIM-1").latest_decision["payable_amount_minor"],
            7_000_000_00,
        )
        with self.assertRaisesRegex(DomainError, "正差额"):
            service.settle_adjudication(actor=finance(),
                                        adjudication_id="ADJ-CODEV")
        settled = service.settle_adjudication(
            actor=finance(), adjudication_id="ADJ-CODEV",
            adjustment_payment_id="PAY-TOP",
        )
        self.assertEqual(settled["payload"]["direction"], "top_up")
        self.assertEqual(settled["payload"]["delta_amount_minor"], 2_000_000_00)
        top_up = service._payment("PAY-TOP")
        self.assertEqual(top_up["amount_minor"], 2_000_000_00)
        self.assertEqual(top_up["payment_kind"], "adjustment_top_up")
        self.assertEqual(top_up["adjudication_id"], "ADJ-CODEV")
        # 两笔付款并存、互不覆盖
        self.assertEqual(service._payment("PAY-1")["amount_minor"], 5_000_000_00)
        # 清算幂等
        with self.assertRaisesRegex(DomainError, "不可重复"):
            service.settle_adjudication(
                actor=finance(), adjudication_id="ADJ-CODEV",
                adjustment_payment_id="PAY-TOP2",
            )

    def test_data_correction_records_correction_chain_and_zero_delta(self) -> None:
        service, _ = self._achieved_with_payment("CLAIM-1", "PAY-1")
        corrected = evidence(orr=0.38, extra="数据库锁定 2026-09-20；勘误 v2")
        service.request_adjudication(
            actor=correction_submitter(), claim_id="CLAIM-1",
            adjudication_id="ADJ-CORR", reason="data_correction",
            correction_packages=[corrected],
        )
        claim = service._claim("CLAIM-1")
        new_lock = next(lk for lk in claim.locks if lk["round"] == 2)
        self.assertIn("correction_of", new_lock)
        self.assertEqual(new_lock["correction_of"]["round"], 1)
        sign_round(service, "CLAIM-1")
        settled = service.settle_adjudication(
            actor=finance(), adjudication_id="ADJ-CORR",
        )
        self.assertEqual(settled["payload"]["direction"], "none")
        self.assertEqual(settled["payload"]["delta_amount_minor"], 0)
        trace = service.trace_claim(claim_id="CLAIM-1", actor=finance())
        self.assertEqual(len(trace["evidence"]), 2)
        self.assertIsNotNone(trace["evidence"][1].get("correction_of"))

    def test_settle_before_new_round_decided_is_rejected(self) -> None:
        service, _ = self._achieved_with_payment("CLAIM-1", "PAY-1")
        service.request_adjudication(
            actor=admin(), claim_id="CLAIM-1", adjudication_id="ADJ-EXIT",
            reason="territory_exit", territories=["US"],
        )
        with self.assertRaisesRegex(DomainError, "尚未裁决"):
            service.settle_adjudication(actor=finance(),
                                        adjudication_id="ADJ-EXIT")

    def test_only_decided_claim_can_open_new_adjudication(self) -> None:
        service, _ = build_service()
        service.register_partnership_version(
            actor=admin(), partnership_id="CO-1", rules=rules_v1(),
            parties=["甲生物", "乙制药"],
        )
        service.submit_claim(
            actor=submitter(), claim_id="CLAIM-N", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        with self.assertRaisesRegex(DomainError, "已裁决"):
            service.request_adjudication(
                actor=admin(), claim_id="CLAIM-N",
                adjudication_id="ADJ-X", reason="data_correction",
                correction_packages=[evidence()],
            )


if __name__ == "__main__":
    unittest.main()
