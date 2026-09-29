"""证据幂等、同标识不同内容争议与委员会裁决。"""
from __future__ import annotations

import unittest

from fixtures import (
    admin, build_service, compliance, evidence, finance, register, rules_v1,
    science, submitter,
)
from src.errors import DomainError, PermissionDenied


def _submit(service, claim_id, ev, *, territories=("US",)):
    return service.submit_claim(
        actor=submitter(), claim_id=claim_id, partnership_id="CO-1",
        milestone_code="M1_P3_READOUT", project_code="P-101",
        territories=list(territories), evidence_packages=[ev],
    )


def _register_and_submit(service, claim_id, ev, *, territories=("US",)):
    if not service.store.exists("CO-1"):
        register(service)
    return _submit(service, claim_id, ev, territories=territories)


class EvidenceIdempotencyTest(unittest.TestCase):
    def test_duplicate_submission_is_idempotent(self) -> None:
        service, _ = build_service()
        _register_and_submit(service, "CLAIM-D", evidence())
        before = len(service.store.all_events())
        again = service.submit_claim(
            actor=submitter(), claim_id="CLAIM-D", partnership_id="CO-1",
            milestone_code="M1_P3_READOUT", project_code="P-101",
            territories=["US"], evidence_packages=[evidence()],
        )
        self.assertEqual(len(service.store.all_events()), before)
        self.assertEqual({e["event_type"] for e in again},
                         {"CLAIM_SUBMITTED", "EVIDENCE_LOCKED"})

    def test_duplicate_claim_id_with_different_content_is_rejected(self) -> None:
        service, _ = build_service()
        _register_and_submit(service, "CLAIM-D", evidence(orr=0.42))
        with self.assertRaisesRegex(DomainError, "证据不一致"):
            service.submit_claim(
                actor=submitter(), claim_id="CLAIM-D", partnership_id="CO-1",
                milestone_code="M1_P3_READOUT", project_code="P-101",
                territories=["US"], evidence_packages=[evidence(orr=0.99)],
            )

    def test_same_id_same_content_reuses_lock(self) -> None:
        service, _ = build_service()
        _register_and_submit(service, "CLAIM-A", evidence())
        other = dict(evidence())
        _register_and_submit(service, "CLAIM-B", other)
        locks_b = [
            e for e in service.store.stream("CLAIM-B")
            if e["event_type"] == "EVIDENCE_LOCKED"
        ]
        self.assertEqual(len(locks_b), 1)
        self.assertEqual(
            locks_b[0]["payload"]["reused_from"]["claim_id"], "CLAIM-A"
        )

    def test_same_id_different_content_enters_conflict(self) -> None:
        service, _ = build_service()
        _register_and_submit(service, "CLAIM-A", evidence(orr=0.42))
        _register_and_submit(service, "CLAIM-B", evidence(orr=0.71))
        claim_b = service._claim("CLAIM-B")
        self.assertIsNotNone(claim_b.conflict)
        self.assertEqual(claim_b.conflict["existing_claim_id"], "CLAIM-A")
        flagged = [
            e for e in service.store.stream("CLAIM-B")
            if e["event_type"] == "EVIDENCE_CONFLICT_FLAGGED"
        ]
        self.assertEqual(len(flagged), 1)
        # 争议未解决：不能签署
        with self.assertRaisesRegex(DomainError, "争议"):
            service.sign_review(actor=science(), claim_id="CLAIM-B",
                                office="science")

    def test_conflict_resolution_locks_accepted_content(self) -> None:
        service, _ = build_service()
        _register_and_submit(service, "CLAIM-A", evidence(orr=0.42))
        _register_and_submit(service, "CLAIM-B", evidence(orr=0.71))
        # 提交者不能裁决自己的冲突
        with self.assertRaises(PermissionDenied):
            service.resolve_conflict(
                actor=submitter(), claim_id="CLAIM-B",
                evidence_id="EV-P3-001",
                accepted_content=evidence(orr=0.42)["content"],
                note="采纳原始数据库读数",
            )
        resolved = service.resolve_conflict(
            actor=admin(), claim_id="CLAIM-B",
            evidence_id="EV-P3-001",
            accepted_content=evidence(orr=0.42)["content"],
            note="采纳原始数据库读数",
        )
        self.assertEqual(resolved[0]["event_type"], "EVIDENCE_LOCKED")
        self.assertIn("conflict_resolution", resolved[0]["payload"])
        claim_b = service._claim("CLAIM-B")
        self.assertIsNone(claim_b.conflict)
        # 采纳后可正常走完签署
        service.sign_review(actor=science(), claim_id="CLAIM-B", office="science")
        service.sign_review(actor=compliance(), claim_id="CLAIM-B",
                            office="compliance")
        service.sign_review(actor=finance(), claim_id="CLAIM-B", office="finance")
        claim_b = service._claim("CLAIM-B")
        self.assertEqual(claim_b.latest_decision["outcome"], "achieved")


if __name__ == "__main__":
    unittest.main()
