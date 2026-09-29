"""里程碑托管领域不变量测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.escrow import MilestoneEscrow
from src.errors import (
    AuthorizationError,
    ConflictError,
    ValidationError,
    WorkflowError,
)
from src.projection import MemberView, ReconciliationReport
from src.store import EventStore

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 3, 24, 9, 0, tzinfo=CST)


def spec_v1() -> dict:
    return {
        "parties": ["PARTY_A", "PARTY_B"],
        "projects": ["P-1"],
        "members": {
            "u-submit": {"party": "PARTY_A", "name": "甲临床"},
            "u-sci": {"party": "PARTY_A", "name": "甲科学"},
            "u-comp": {"party": "PARTY_B", "name": "乙合规"},
            "u-fin": {"party": "PARTY_B", "name": "乙财务"},
            "u-dual": {"party": "PARTY_A", "name": "甲双签"},
            "u-observer": {"party": "PARTY_B", "name": "乙普通成员"},
        },
        "decision_rights": {
            "scientific": ["u-sci", "u-dual"],
            "compliance": ["u-comp", "u-dual"],
            "finance": ["u-fin"],
        },
        "review_deadline_days": {"scientific": 10, "compliance": 15, "finance": 20},
        "billing": {"fiscal_year_start_month": 1},
        "disclosure": {"external_release_requires": ["compliance"], "embargo_until": "PAYMENT"},
        "milestones": {
            "M-P3": {
                "projects": ["P-1"],
                "region_amounts": {"CN": "100", "US": "200"},
                "currency": "USD",
                "currency_basis": "FX_RATE_AT_FREEZE:7.10",
                "co_dev_payee_share": "1",
                "scopes": ["scientific", "compliance", "finance"],
                "evidence_kinds": ["PRIMARY_ENDPOINT", "SAFETY"],
            }
        },
    }


def build_service(registered_version: str = "partnership_version-v1", spec: dict | None = None) -> MilestoneEscrow:
    escrow = MilestoneEscrow(EventStore())
    escrow.register_partnership_version(
        event_id="ev-v1",
        partnership_version_id=registered_version,
        occurred_at=T0 - timedelta(days=30),
        spec=spec or spec_v1(),
    )
    return escrow


def pay_full_claim(escrow: MilestoneEscrow, claim_id: str = "claim-1", *, start: int = 0) -> str:
    escrow.submit_claim(
        event_id=f"ev-sub-{start}",
        claim_id=claim_id,
        milestone_code="M-P3",
        project_id="P-1",
        achieved_regions=["CN", "US"],
        submitter="u-submit",
        evidence_package_id="pkg-1",
        content_hash="hash-aaa",
        occurred_at=T0,
    )
    escrow.lock_evidence(event_id=f"ev-lock-{start}", claim_id=claim_id, occurred_at=T0)
    escrow.record_signature(event_id=f"ev-sig-s-{start}", claim_id=claim_id,
                            scope="scientific", signer="u-sci", occurred_at=T0 + timedelta(days=5))
    escrow.record_signature(event_id=f"ev-sig-c-{start}", claim_id=claim_id,
                            scope="compliance", signer="u-comp", occurred_at=T0 + timedelta(days=10))
    payment_id = escrow.record_signature(
        event_id=f"ev-sig-f-{start}", claim_id=claim_id,
        scope="finance", signer="u-fin", occurred_at=T0 + timedelta(days=15),
    )
    assert payment_id is not None
    return payment_id


class HappyPathTest(unittest.TestCase):
    def test_independent_signatures_produce_payment(self) -> None:
        escrow = build_service()
        payment_id = pay_full_claim(escrow)
        payment = escrow.get_payment(payment_id)
        self.assertEqual(payment["payload"]["kind"], "INITIAL")
        self.assertEqual(payment["payload"]["amount"], "300.00")
        self.assertEqual(payment["payload"]["currency"], "USD")
        self.assertEqual(escrow.get_claim("claim-1").status, "PAID")

    def test_submission_is_only_candidate_before_signatures(self) -> None:
        escrow = build_service()
        escrow.submit_claim(
            event_id="ev-sub", claim_id="claim-1", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-1",
            content_hash="h", occurred_at=T0,
        )
        self.assertEqual(escrow.get_claim("claim-1").status, "SUBMITTED")
        escrow.lock_evidence(event_id="ev-lock", claim_id="claim-1", occurred_at=T0)
        self.assertEqual(escrow.get_claim("claim-1").status, "UNDER_REVIEW")

    def test_scope_outside_contract_rejected(self) -> None:
        escrow = build_service()
        escrow.submit_claim(
            event_id="ev-sub", claim_id="claim-1", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-1",
            content_hash="h", occurred_at=T0,
        )
        with self.assertRaises(ValidationError):
            escrow.submit_claim(
                event_id="ev-sub2", claim_id="claim-2", milestone_code="M-P3", project_id="P-9",
                achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-2",
                content_hash="h", occurred_at=T0,
            )
        with self.assertRaises(ValidationError):
            escrow.submit_claim(
                event_id="ev-sub3", claim_id="claim-3", milestone_code="M-P3", project_id="P-1",
                achieved_regions=["EU"], submitter="u-submit", evidence_package_id="pkg-3",
                content_hash="h", occurred_at=T0,
            )


class SignatureIndependenceTest(unittest.TestCase):
    def _submitted_locked(self, escrow: MilestoneEscrow) -> None:
        escrow.submit_claim(
            event_id="ev-sub", claim_id="claim-1", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-1",
            content_hash="h", occurred_at=T0,
        )
        escrow.lock_evidence(event_id="ev-lock", claim_id="claim-1", occurred_at=T0)

    def test_submitter_cannot_approve_own_material(self) -> None:
        escrow = build_service()
        self._submitted_locked(escrow)
        with self.assertRaises(AuthorizationError):
            escrow.record_signature(
                event_id="ev-s1", claim_id="claim-1", scope="scientific",
                signer="u-submit", occurred_at=T0 + timedelta(days=1),
            )

    def test_member_without_scope_right_rejected(self) -> None:
        escrow = build_service()
        self._submitted_locked(escrow)
        with self.assertRaises(AuthorizationError):
            escrow.record_signature(
                event_id="ev-s1", claim_id="claim-1", scope="scientific",
                signer="u-observer", occurred_at=T0 + timedelta(days=1),
            )

    def test_same_member_cannot_sign_two_scopes(self) -> None:
        escrow = build_service()
        self._submitted_locked(escrow)
        escrow.record_signature(
            event_id="ev-s1", claim_id="claim-1", scope="scientific",
            signer="u-dual", occurred_at=T0 + timedelta(days=1),
        )
        with self.assertRaises(AuthorizationError):
            escrow.record_signature(
                event_id="ev-s2", claim_id="claim-1", scope="compliance",
                signer="u-dual", occurred_at=T0 + timedelta(days=1),
            )

    def test_scope_cannot_be_signed_twice(self) -> None:
        escrow = build_service()
        self._submitted_locked(escrow)
        escrow.record_signature(
            event_id="ev-s1", claim_id="claim-1", scope="scientific",
            signer="u-sci", occurred_at=T0 + timedelta(days=1),
        )
        with self.assertRaises(ConflictError):
            escrow.record_signature(
                event_id="ev-s2", claim_id="claim-1", scope="scientific",
                signer="u-sci", occurred_at=T0 + timedelta(days=2),
            )

    def test_one_rejection_closes_claim_without_payment(self) -> None:
        escrow = build_service()
        self._submitted_locked(escrow)
        result = escrow.record_signature(
            event_id="ev-s1", claim_id="claim-1", scope="scientific",
            signer="u-sci", occurred_at=T0 + timedelta(days=1),
        )
        self.assertIsNone(result)
        result = escrow.record_signature(
            event_id="ev-s2", claim_id="claim-1", scope="compliance",
            signer="u-comp", occurred_at=T0 + timedelta(days=2),
            decision="REJECT", reason="数据合规不满足",
        )
        self.assertIsNone(result)
        self.assertEqual(escrow.get_claim("claim-1").status, "REJECTED")
        self.assertEqual(escrow.claim_payments("claim-1"), [])


class EvidenceIdempotencyTest(unittest.TestCase):
    def _submit(self, escrow: MilestoneEscrow, hash_value: str, event_suffix: str) -> str:
        return escrow.submit_claim(
            event_id=f"ev-sub-{event_suffix}", claim_id=f"claim-{event_suffix}",
            milestone_code="M-P3", project_id="P-1", achieved_regions=["CN"],
            submitter="u-submit", evidence_package_id="pkg-1",
            content_hash=hash_value, occurred_at=T0,
        )

    def test_same_package_same_content_is_idempotent(self) -> None:
        escrow = build_service()
        first = self._submit(escrow, "hash-aaa", "1")
        second = self._submit(escrow, "hash-aaa", "2")
        self.assertEqual(first, second)
        self.assertEqual(len(escrow.store.events("claim-1")), 1)

    def test_same_package_different_content_opens_dispute(self) -> None:
        escrow = build_service()
        self._submit(escrow, "hash-aaa", "1")
        owner = self._submit(escrow, "hash-bbb", "2")
        self.assertEqual(owner, "claim-1")
        self.assertEqual(escrow.get_claim("claim-1").status, "IN_DISPUTE")
        with self.assertRaises(WorkflowError):
            escrow.lock_evidence(event_id="ev-lock", claim_id="claim-1", occurred_at=T0)

    def test_dispute_confirm_original_resumes_flow(self) -> None:
        escrow = build_service()
        self._submit(escrow, "hash-aaa", "1")
        self._submit(escrow, "hash-bbb", "2")
        escrow.resolve_dispute(
            event_id="ev-res", claim_id="claim-1", resolution="CONFIRM_ORIGINAL",
            resolved_by="u-comp", occurred_at=T0 + timedelta(days=1),
        )
        escrow.lock_evidence(event_id="ev-lock", claim_id="claim-1", occurred_at=T0 + timedelta(days=1))
        self.assertEqual(escrow.get_claim("claim-1").evidence["content_hash"], "hash-aaa")

    def test_dispute_replace_rejects_claim(self) -> None:
        escrow = build_service()
        self._submit(escrow, "hash-aaa", "1")
        self._submit(escrow, "hash-bbb", "2")
        escrow.resolve_dispute(
            event_id="ev-res", claim_id="claim-1", resolution="REPLACE_WITH_CORRECTED",
            resolved_by="u-comp", occurred_at=T0 + timedelta(days=1),
        )
        self.assertEqual(escrow.get_claim("claim-1").status, "REJECTED")


class FrozenRulesTest(unittest.TestCase):
    def test_inflight_claim_uses_frozen_version_after_new_registration(self) -> None:
        escrow = build_service()
        escrow.submit_claim(
            event_id="ev-sub-1", claim_id="claim-1", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN", "US"], submitter="u-submit", evidence_package_id="pkg-1",
            content_hash="h1", occurred_at=T0,
        )
        changed = spec_v1()
        changed["milestones"]["M-P3"]["region_amounts"] = {"CN": "999", "US": "999"}
        changed["review_deadline_days"] = {"scientific": 1, "compliance": 1, "finance": 1}
        escrow.register_partnership_version(
            event_id="ev-v2", partnership_version_id="partnership_version-v2",
            occurred_at=T0 + timedelta(days=1), spec=changed,
        )
        escrow.lock_evidence(event_id="ev-lock", claim_id="claim-1", occurred_at=T0)
        # 新版本只有 1 天期限；冻结版本仍是 10/15/20 天，第 5 天签署科学应有效。
        escrow.record_signature(
            event_id="ev-s1", claim_id="claim-1", scope="scientific",
            signer="u-sci", occurred_at=T0 + timedelta(days=5),
        )

    def test_signature_after_frozen_deadline_rejected(self) -> None:
        escrow = build_service()
        escrow.submit_claim(
            event_id="ev-sub", claim_id="claim-1", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-1",
            content_hash="h", occurred_at=T0,
        )
        escrow.lock_evidence(event_id="ev-lock", claim_id="claim-1", occurred_at=T0)
        # T0 09:00+08 == 01:00Z；财务冻结期限为 +20 天即 04-13 01:00Z
        with self.assertRaises(WorkflowError):
            escrow.record_signature(
                event_id="ev-s-late", claim_id="claim-1", scope="finance",
                signer="u-fin", occurred_at=datetime(2026, 4, 14, 2, 0, tzinfo=timezone.utc),
            )
        escrow.expire_claim(
            event_id="ev-exp", claim_id="claim-1",
            at=datetime(2026, 4, 14, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(escrow.get_claim("claim-1").status, "REJECTED")

    def test_billing_period_frozen_across_timezone_and_cycle_boundary(self) -> None:
        escrow = build_service()
        # 提交时刻在北京时间仍属 Q1，换算 UTC 已进入 4 月 1 日 Q2。
        submit_at = datetime(2026, 3, 31, 23, 30, tzinfo=CST)
        escrow.submit_claim(
            event_id="ev-sub", claim_id="claim-q1", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-q1",
            content_hash="h", occurred_at=submit_at,
        )
        claim = escrow.get_claim("claim-q1")
        self.assertEqual(claim.submitted["billing_period"], "FY2026-Q1")
        escrow.lock_evidence(
            event_id="ev-lock", claim_id="claim-q1",
            occurred_at=datetime(2026, 4, 2, tzinfo=CST),
        )
        # 付款事件跨入新账期，但账期归属仍冻结为提交时的 Q1。
        payment_id = pay_after_lock(escrow, "claim-q1", datetime(2026, 4, 5, tzinfo=CST))
        self.assertEqual(escrow.get_payment(payment_id)["payload"]["billing_period"], "FY2026-Q1")


def pay_after_lock(escrow: MilestoneEscrow, claim_id: str, when: datetime) -> str:
    escrow.record_signature(event_id="es1", claim_id=claim_id, scope="scientific",
                            signer="u-sci", occurred_at=when)
    escrow.record_signature(event_id="es2", claim_id=claim_id, scope="compliance",
                            signer="u-comp", occurred_at=when)
    payment_id = escrow.record_signature(event_id="es3", claim_id=claim_id, scope="finance",
                                         signer="u-fin", occurred_at=when)
    assert payment_id is not None
    return payment_id


class AdjudicationTest(unittest.TestCase):
    def test_regional_exit_creates_recovery_without_touching_original(self) -> None:
        escrow = build_service()
        payment_id = pay_full_claim(escrow)
        before = escrow.get_payment(payment_id)["payload"]["amount"]

        escrow.open_adjudication(
            event_id="ev-adj-open", adjudication_id="adj-1", claim_id="claim-1",
            reason="REGIONAL_EXIT", initiated_by="u-comp", occurred_at=T0 + timedelta(days=40),
            achieved_regions=["CN"],
        )
        recovery_id = escrow.settle_adjudication(
            event_id="ev-adj-set", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-comp"],
        )
        recovery = escrow.get_payment(recovery_id)
        self.assertEqual(recovery["payload"]["kind"], "RECOVERY")
        self.assertEqual(recovery["payload"]["amount"], "200.00")
        # 原始付款事实不变。
        self.assertEqual(escrow.get_payment(payment_id)["payload"]["amount"], before)
        self.assertEqual(escrow.get_payment(payment_id)["payload"]["kind"], "INITIAL")
        report = ReconciliationReport(escrow).from_payment(payment_id)
        self.assertEqual({p["kind"] for p in report["payments"]}, {"INITIAL", "RECOVERY"})

    def test_data_correction_ineligible_recovers_full_amount(self) -> None:
        escrow = build_service()
        payment_id = pay_full_claim(escrow)
        escrow.open_adjudication(
            event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
            reason="DATA_CORRECTION", initiated_by="u-sci",
            occurred_at=T0 + timedelta(days=40), eligible=False,
        )
        recovery_id = escrow.settle_adjudication(
            event_id="ev-s", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-comp"],
        )
        self.assertEqual(escrow.get_payment(recovery_id)["payload"]["amount"], "300.00")
        self.assertEqual(escrow.get_payment(recovery_id)["payload"]["kind"], "RECOVERY")

    def test_codevelopment_share_reduction_creates_recovery(self) -> None:
        escrow = build_service()
        pay_full_claim(escrow)  # 300 已付，份额 1
        escrow.open_adjudication(
            event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
            reason="CODEVELOPMENT_OPTION_CHANGE", initiated_by="u-fin",
            occurred_at=T0 + timedelta(days=40), co_dev_payee_share="0.5",
        )
        # 份额下调 → 应付 150，已付 300 → 追回 150。
        recovery_id = escrow.settle_adjudication(
            event_id="ev-s", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-fin"],
        )
        self.assertEqual(escrow.get_payment(recovery_id)["payload"]["amount"], "150.00")
        self.assertEqual(escrow.get_payment(recovery_id)["payload"]["kind"], "RECOVERY")

    def test_codevelopment_share_increase_creates_additional(self) -> None:
        changed = spec_v1()
        changed["milestones"]["M-P3"]["co_dev_payee_share"] = "0.5"
        escrow = build_service(spec=changed)
        pay_full_claim(escrow)  # 已付 300*0.5 = 150
        escrow.open_adjudication(
            event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
            reason="CODEVELOPMENT_OPTION_CHANGE", initiated_by="u-fin",
            occurred_at=T0 + timedelta(days=40), co_dev_payee_share="1",
        )
        additional_id = escrow.settle_adjudication(
            event_id="ev-s", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-fin"],
        )
        self.assertEqual(escrow.get_payment(additional_id)["payload"]["amount"], "150.00")
        self.assertEqual(escrow.get_payment(additional_id)["payload"]["kind"], "ADDITIONAL")

    def test_zero_delta_settles_without_payment(self) -> None:
        escrow = build_service()
        pay_full_claim(escrow)
        escrow.open_adjudication(
            event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
            reason="DATA_CORRECTION", initiated_by="u-sci",
            occurred_at=T0 + timedelta(days=40), achieved_regions=["CN", "US"],
        )
        result = escrow.settle_adjudication(
            event_id="ev-s", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-comp"],
        )
        self.assertIsNone(result)

    def test_settlement_requires_both_parties_and_excludes_submitter(self) -> None:
        escrow = build_service()
        pay_full_claim(escrow)
        escrow.open_adjudication(
            event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
            reason="REGIONAL_EXIT", initiated_by="u-comp",
            occurred_at=T0 + timedelta(days=40), achieved_regions=["CN"],
        )
        with self.assertRaises(AuthorizationError):
            escrow.settle_adjudication(
                event_id="ev-s1", adjudication_id="adj-1",
                occurred_at=T0 + timedelta(days=41), approved_by=["u-sci"],  # 仅甲方
            )
        with self.assertRaises(AuthorizationError):
            escrow.settle_adjudication(
                event_id="ev-s2", adjudication_id="adj-1",
                occurred_at=T0 + timedelta(days=41),
                approved_by=["u-sci", "u-submit"],  # 含提交者
            )

    def test_chained_adjudications_net_recoveries_and_additions(self) -> None:
        escrow = build_service()
        pay_full_claim(escrow)  # 已付 300（CN100 + US200）
        # 第一次裁决：美国退出 → 追回 200，净额 100。
        escrow.open_adjudication(
            event_id="ev-o1", adjudication_id="adj-1", claim_id="claim-1",
            reason="REGIONAL_EXIT", initiated_by="u-comp",
            occurred_at=T0 + timedelta(days=40), achieved_regions=["CN"],
        )
        first = escrow.settle_adjudication(
            event_id="ev-s1", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-comp"],
        )
        self.assertEqual(escrow.get_payment(first)["payload"]["amount"], "200.00")
        # 第二次裁决：中国数据纠正后也不满足 → 应再追回 100，而非 300。
        escrow.open_adjudication(
            event_id="ev-o2", adjudication_id="adj-2", claim_id="claim-1",
            reason="DATA_CORRECTION", initiated_by="u-sci",
            occurred_at=T0 + timedelta(days=50), eligible=False,
        )
        second = escrow.settle_adjudication(
            event_id="ev-s2", adjudication_id="adj-2",
            occurred_at=T0 + timedelta(days=51), approved_by=["u-sci", "u-comp"],
        )
        self.assertEqual(escrow.get_payment(second)["payload"]["amount"], "100.00")
        kinds = {p["payload"]["kind"] for p in escrow.claim_payments("claim-1")}
        self.assertEqual(kinds, {"INITIAL", "RECOVERY"})

    def test_adjudication_only_after_payment(self) -> None:
        escrow = build_service()
        escrow.submit_claim(
            event_id="ev-sub", claim_id="claim-1", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-1",
            content_hash="h", occurred_at=T0,
        )
        with self.assertRaises(WorkflowError):
            escrow.open_adjudication(
                event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
                reason="DATA_CORRECTION", initiated_by="u-sci", occurred_at=T0,
            )


class ReconciliationTest(unittest.TestCase):
    def test_trace_back_from_payment(self) -> None:
        escrow = build_service()
        payment_id = pay_full_claim(escrow)
        report = ReconciliationReport(escrow).from_payment(payment_id)
        self.assertEqual(report["entry_point"], {"type": "PAYMENT", "id": payment_id})
        self.assertEqual(report["rights"]["partnership_version_id"], "partnership_version-v1")
        self.assertEqual(report["rights"]["achieved_regions"], ["CN", "US"])
        self.assertIsNotNone(report["evidence"])
        self.assertEqual(report["evidence"]["evidence_package_id"], "pkg-1")
        scopes = {s["scope"] for s in report["review"]["signatures"]}
        self.assertEqual(scopes, {"scientific", "compliance", "finance"})
        self.assertGreaterEqual(report["integrity"]["event_count"], 5)
        # 追加新裁决后，旧付款仍可反向追溯，链变长但旧事实不变。
        chain_before = report["integrity"]["append_only_chain"]
        escrow.open_adjudication(
            event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
            reason="REGIONAL_EXIT", initiated_by="u-comp",
            occurred_at=T0 + timedelta(days=40), achieved_regions=["CN"],
        )
        escrow.settle_adjudication(
            event_id="ev-s", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-comp"],
        )
        report2 = ReconciliationReport(escrow).from_payment(payment_id)
        self.assertEqual(len(report2["corrections"]), 1)
        self.assertNotEqual(chain_before, report2["integrity"]["append_only_chain"])

    def test_trace_back_from_failed_milestone(self) -> None:
        escrow = build_service()
        escrow.submit_claim(
            event_id="ev-sub", claim_id="claim-x", milestone_code="M-P3", project_id="P-1",
            achieved_regions=["CN"], submitter="u-submit", evidence_package_id="pkg-x",
            content_hash="h", occurred_at=T0,
        )
        escrow.lock_evidence(event_id="ev-lock", claim_id="claim-x", occurred_at=T0)
        escrow.record_signature(
            event_id="ev-s1", claim_id="claim-x", scope="scientific",
            signer="u-sci", occurred_at=T0 + timedelta(days=2),
        )
        escrow.record_signature(
            event_id="ev-s2", claim_id="claim-x", scope="compliance",
            signer="u-comp", occurred_at=T0 + timedelta(days=3),
            decision="REJECT", reason="入组数据不可核验",
        )
        report = ReconciliationReport(escrow).from_claim("claim-x")
        self.assertEqual(report["status"], "REJECTED")
        self.assertIsNotNone(report["review"]["rejection"])
        self.assertEqual(report["payments"], [])
        self.assertEqual(len(report["review"]["signatures"]), 2)


class MemberViewTest(unittest.TestCase):
    def test_scoped_redaction(self) -> None:
        escrow = build_service()
        pay_full_claim(escrow)
        views = MemberView(escrow)

        sci = views.claim_view("u-sci", "claim-1")
        self.assertNotEqual(sci["evidence"], "REDACTED")
        self.assertEqual(sci["payments"], "REDACTED")

        fin = views.claim_view("u-fin", "claim-1")
        self.assertEqual(fin["evidence"], "REDACTED")
        self.assertNotEqual(fin["payments"], "REDACTED")
        self.assertEqual(fin["payments"][0]["amount"], "300.00")
        self.assertEqual(fin["disclosure_restrictions"], "REDACTED")

        comp = views.claim_view("u-comp", "claim-1")
        self.assertNotEqual(comp["disclosure_restrictions"], "REDACTED")
        self.assertEqual(comp["achieved_regions"], ["CN", "US"])

        submitter = views.claim_view("u-submit", "claim-1")
        self.assertNotEqual(submitter["evidence"], "REDACTED")
        self.assertEqual(submitter["payments"], "REDACTED")

    def test_outside_scope_member_sees_nothing(self) -> None:
        escrow = build_service()
        pay_full_claim(escrow)
        views = MemberView(escrow)
        self.assertNotIn("claim-1", views.visible_claims("u-observer"))
        with self.assertRaises(AuthorizationError):
            views.claim_view("u-observer", "claim-1")

    def test_dashboard_only_lists_duty_related_claims(self) -> None:
        escrow = build_service()
        pay_full_claim(escrow)
        views = MemberView(escrow)
        dashboard = views.dashboard("u-fin")
        self.assertEqual([c["claim_id"] for c in dashboard["claims"]], ["claim-1"])
        self.assertEqual(dashboard["claims"][0]["evidence"], "REDACTED")


class EventStoreTest(unittest.TestCase):
    def test_duplicate_event_id_and_version_conflict_rejected(self) -> None:
        store = EventStore()
        event = {
            "event_id": "e1", "event_type": "CLAIM_SUBMITTED",
            "occurred_at": T0.isoformat(), "aggregate_id": "claim-9", "version": 1,
            "payload": {},
        }
        store.append(event, 0)
        with self.assertRaises(ValueError):
            store.append(dict(event), 0)
        with self.assertRaises(ValueError):
            store.append(
                {**event, "event_id": "e2", "version": 3}, 1,
            )

    def test_payment_events_are_append_only(self) -> None:
        escrow = build_service()
        payment_id = pay_full_claim(escrow)
        escrow.open_adjudication(
            event_id="ev-o", adjudication_id="adj-1", claim_id="claim-1",
            reason="REGIONAL_EXIT", initiated_by="u-comp",
            occurred_at=T0 + timedelta(days=40), achieved_regions=["CN"],
        )
        escrow.settle_adjudication(
            event_id="ev-s", adjudication_id="adj-1",
            occurred_at=T0 + timedelta(days=41), approved_by=["u-sci", "u-comp"],
        )
        payment_stream = escrow.store.events(payment_id)
        self.assertEqual(len(payment_stream), 1)
        self.assertEqual(payment_stream[0]["event_type"], "PAYMENT_ORDERED")


if __name__ == "__main__":
    unittest.main()
