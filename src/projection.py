"""反向核对投影与按职责的脱敏视图。

授权双方可从任一付款指令或任一未通过里程碑出发，反向核对：
权利（冻结的合作版本与条款）→ 证据（包标识/内容哈希/锁定事实）→
签署（各范围独立签署与期限）→ 纠正历史（争议、裁决与差额付款）。

普通成员只能看到与其签署职责相关的主张，且按范围屏蔽敏感字段：
科学/合规可见证据与权利地区但看不到金额，财务可见金额但看不到临床证据明细。
"""
from __future__ import annotations

import hashlib
from typing import Any

from .escrow import MilestoneEscrow, _Claim, _Partnership
from .errors import AuthorizationError


class ReconciliationReport:
    """从付款或里程碑反向重建完整证据链，并校验只追加完整性。"""

    def __init__(self, escrow: MilestoneEscrow) -> None:
        self.escrow = escrow

    def _root_events(self, claim_id: str) -> list[dict[str, Any]]:
        """按全局追加顺序收集根主张闭环内的全部事件（含付款与裁决）。"""
        keep = {claim_id}
        for adj in self.escrow.claim_adjudications(claim_id):
            keep.add(adj.id)
        for payment in self.escrow.claim_payments(claim_id):
            keep.add(payment["aggregate_id"])
        return [e for e in self.escrow.store.all_events() if e["aggregate_id"] in keep]

    @staticmethod
    def _chain_hash(events: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for event in events:  # 仅按追加顺序串联 event_id，校验链不依赖负载可变性
            digest.update(event["event_id"].encode("utf-8"))
        return digest.hexdigest()

    def build(self, claim_id: str) -> dict[str, Any]:
        claim: _Claim = self.escrow.get_claim(claim_id)
        submitted = claim.submitted
        version: _Partnership = self.escrow._versions[submitted["partnership_version_id"]]
        frozen = submitted["frozen_milestone"]

        evidence: dict[str, Any] | None = None
        if claim.evidence is not None:
            evidence = {
                "evidence_package_id": claim.evidence["evidence_package_id"],
                "content_hash": claim.evidence["content_hash"],
                "evidence_kinds": claim.evidence["evidence_kinds"],
                "locked_at": claim.evidence["locked_at"],
                "deadlines": claim.evidence["deadlines"],
            }

        signatures = [
            {
                "scope": payload["scope"],
                "signer": payload["signer"],
                "decision": payload.get("decision", "APPROVE"),
                "signed_at": payload["signed_at"],
                "reason": payload.get("reason", ""),
            }
            for payload in sorted(claim.all_signatures, key=lambda p: p["signed_at"])
        ]

        corrections = []
        for adj in self.escrow.claim_adjudications(claim_id):
            entry: dict[str, Any] = {
                "adjudication_id": adj.id,
                "reason": adj.opened["reason"],
                "change": adj.opened["change"],
                "opened_by": adj.opened["initiated_by"],
                "status": "SETTLED" if adj.settled else "OPEN",
            }
            if adj.settled is not None:
                entry.update(
                    {
                        "recomputed_payable": adj.settled["recomputed_payable"],
                        "previously_paid": adj.settled["previously_paid"],
                        "delta": adj.settled["delta"],
                        "approved_by": adj.settled["approved_by"],
                    }
                )
            corrections.append(entry)

        payments = [
            {
                "payment_instruction_id": e["aggregate_id"],
                "origin": e["payload"]["origin"],
                "kind": e["payload"]["kind"],
                "amount": e["payload"]["amount"],
                "currency": e["payload"]["currency"],
                "currency_basis": e["payload"]["currency_basis"],
                "billing_period": e["payload"]["billing_period"],
                "ordered_at": e["occurred_at"],
                "immutable": True,
            }
            for e in self.escrow.claim_payments(claim_id)
        ]

        root_events = self._root_events(claim_id)
        return {
            "claim_id": claim_id,
            "status": claim.status,
            "rights": {
                "partnership_version_id": submitted["partnership_version_id"],
                "registered_at": version.registered_at,
                "milestone_code": submitted["milestone_code"],
                "project_id": submitted["project_id"],
                "achieved_regions": submitted["achieved_regions"],
                "currency": frozen["currency"],
                "currency_basis": frozen["currency_basis"],
                "co_dev_payee_share": frozen["co_dev_payee_share"],
                "disclosure": version.spec["disclosure"],
                "rule_frozen_at_submission": True,
            },
            "evidence": evidence,
            "review": {
                "required_scopes": list(claim.required_scopes),
                "signatures": signatures,
                "submitter": submitted["submitter"],
                "rejection": claim.rejected,
            },
            "disputes": claim.disputes,
            "corrections": corrections,
            "payments": payments,
            "integrity": {
                "event_count": len(root_events),
                "append_only_chain": self._chain_hash(root_events),
            },
        }

    def from_payment(self, payment_instruction_id: str) -> dict[str, Any]:
        payment = self.escrow.get_payment(payment_instruction_id)
        report = self.build(payment["payload"]["root_claim_id"])
        report["entry_point"] = {"type": "PAYMENT", "id": payment_instruction_id}
        return report

    def from_claim(self, claim_id: str) -> dict[str, Any]:
        report = self.build(claim_id)
        report["entry_point"] = {"type": "CLAIM", "id": claim_id}
        return report


# 各签署范围可见的敏感字段类别：
# 科学看临床证据、合规看证据与地区权利、财务只看金额口径（看不到临床明细）。
_SCOPE_VISIBLE_FIELDS = {
    "scientific": {"evidence"},
    "compliance": {"evidence", "regions"},
    "finance": {"financial"},
}


class MemberView:
    """按成员职责投影主张清单，超出职责的管线与金额字段一律屏蔽。"""

    def __init__(self, escrow: MilestoneEscrow) -> None:
        self.escrow = escrow
        self.report = ReconciliationReport(escrow)

    def _member_scopes(self, version: _Partnership, member_id: str) -> set[str]:
        return {
            scope
            for scope, holders in version.spec["decision_rights"].items()
            if member_id in holders
        }

    def visible_claims(self, member_id: str) -> list[str]:
        visible: list[str] = []
        for claim_id, claim in self.escrow._claims.items():
            version = self.escrow._versions[claim.submitted["partnership_version_id"]]
            if member_id not in version.spec["members"]:
                continue
            if claim.submitted["submitter"] == member_id:
                visible.append(claim_id)
                continue
            if self._member_scopes(version, member_id) & set(claim.required_scopes):
                visible.append(claim_id)
        return sorted(visible)

    def claim_view(self, member_id: str, claim_id: str) -> dict[str, Any]:
        claim = self.escrow.get_claim(claim_id)
        version = self.escrow._versions[claim.submitted["partnership_version_id"]]
        if member_id not in version.spec["members"]:
            raise AuthorizationError("未知成员")
        if claim_id not in self.visible_claims(member_id):
            raise AuthorizationError("该主张不在成员职责范围内")

        scopes = self._member_scopes(version, member_id)
        classes: set[str] = (
            set().union(*(_SCOPE_VISIBLE_FIELDS[s] for s in scopes)) if scopes else set()
        )
        is_submitter = claim.submitted["submitter"] == member_id
        if is_submitter:
            # 提交者可见自己的证据与地区，但仍看不到金额。
            classes |= {"evidence", "regions"}

        full = self.report.build(claim_id)
        view: dict[str, Any] = {
            "claim_id": claim_id,
            "status": full["status"],
            "project_id": full["rights"]["project_id"],
            "milestone_code": full["rights"]["milestone_code"],
            "partnership_version_id": full["rights"]["partnership_version_id"],
            "billing_period": claim.submitted["billing_period"],
            "my_scopes": sorted(s for s in scopes if s in claim.required_scopes),
        }

        if "regions" in classes:
            view["achieved_regions"] = full["rights"]["achieved_regions"]
        else:
            view["achieved_regions"] = "REDACTED"

        if "compliance" in scopes:
            # 对外披露限制由合规执行，只对该职责暴露冻结条款。
            view["disclosure_restrictions"] = full["rights"]["disclosure"]
        else:
            view["disclosure_restrictions"] = "REDACTED"

        if "evidence" in classes:
            view["evidence"] = full["evidence"]
        else:
            view["evidence"] = "REDACTED"

        if "financial" in classes:
            view["payments"] = full["payments"]
            view["corrections"] = [
                {k: v for k, v in c.items() if k in ("adjudication_id", "reason", "status",
                                                      "recomputed_payable", "previously_paid", "delta")}
                for c in full["corrections"]
            ]
        else:
            view["payments"] = "REDACTED"
            view["corrections"] = [
                {"adjudication_id": c["adjudication_id"], "reason": c["reason"], "status": c["status"]}
                for c in full["corrections"]
            ]
        return view

    def dashboard(self, member_id: str) -> dict[str, Any]:
        return {
            "member_id": member_id,
            "claims": [self.claim_view(member_id, cid) for cid in self.visible_claims(member_id)],
        }
