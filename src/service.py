"""里程碑托管应用服务。

职责：把操作者命令翻译为领域事件、补齐信封字段并只追加写入；
从事件流重建聚合与证据全局登记表；提供付款/未通过里程碑的反向核对。
服务不保存命令侧状态，所有结论都能从事件流重放得到。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Optional

from .access import Actor, redact
from .canonical import fingerprint
from .domain import Adjudication, Claim, Partnership, Payment, utc_now
from .errors import DomainError, PermissionDenied
from .events import EventStore
from .money import Money


class EscrowService:
    def __init__(self, store: Optional[EventStore] = None, clock=utc_now) -> None:
        self.store = store or EventStore()
        self._clock = clock

    # ---------------------------------------------------------- 合作版本

    def register_partnership_version(
        self,
        *,
        actor: Actor,
        partnership_id: str,
        rules: dict,
        parties: Optional[list[str]] = None,
        effective_from: Optional[datetime] = None,
    ) -> dict:
        actor.require_permission("partnership.register")
        partnership = self._partnership(partnership_id)
        if partnership.parties and actor.party not in partnership.parties:
            raise PermissionDenied("非合作主体不能登记合作版本")
        event = partnership.register_version(
            rules=rules, parties=parties,
            effective_from=effective_from or self._clock(), now=self._clock(),
        )
        return self._append(partnership_id, event)

    # ---------------------------------------------------------- 候选提交

    def submit_claim(
        self,
        *,
        actor: Actor,
        claim_id: str,
        partnership_id: str,
        milestone_code: str,
        project_code: str,
        territories: list[str],
        evidence_packages: list[dict],
    ) -> list[dict]:
        """临床结果提交：只是候选达成，尚不产生任何付款结论。

        证据包形如 {evidence_id, evidence_type, content}。
        以 claim_id 为幂等键：重复提交原样返回既有事实。
        """
        actor.require_permission("claim.submit")
        if self.store.exists(claim_id):
            self._assert_identical_resubmission(
                claim_id=claim_id, partnership_id=partnership_id,
                milestone_code=milestone_code, project_code=project_code,
                territories=territories, evidence_packages=evidence_packages,
            )
            return [
                e for e in self.store.stream(claim_id)
                if e["event_type"] in ("CLAIM_SUBMITTED", "EVIDENCE_LOCKED",
                                       "EVIDENCE_CONFLICT_FLAGGED")
            ]
        partnership = self._partnership(partnership_id)
        claim = Claim(claim_id)
        manifests = [
            {
                "evidence_id": p["evidence_id"],
                "evidence_type": p["evidence_type"],
                "submitted_by": actor.user_id,
            }
            for p in evidence_packages
        ]
        event = claim.submit(
            partnership=partnership, milestone_code=milestone_code,
            project_code=project_code, territories=territories,
            submitter=actor.user_id, manifests=manifests, now=self._clock(),
        )
        stored = [self._append(claim_id, event,
                               idempotency_key=f"submit:{claim_id}")]
        # 提交与证据锁定同属一个命令：候选登记后立即按内容锁定。
        stored.extend(self._lock_evidence(
            claim_id=claim_id,
            bundles=[
                {
                    "evidence_id": p["evidence_id"],
                    "content": p["content"],
                }
                for p in evidence_packages
            ],
        ))
        return stored

    def lock_evidence(
        self, *, actor: Actor, claim_id: str, bundles: list[dict],
    ) -> list[dict]:
        """供重开裁决轮次锁定更正证据；同编号包按幂等键去重。"""
        actor.require_permission("evidence.lock")
        return self._lock_evidence(claim_id=claim_id, bundles=bundles)

    def _lock_evidence(self, *, claim_id: str, bundles: list[dict]) -> list[dict]:
        claim = self._claim(claim_id)
        partnership = self._partnership(claim.partnership_id)
        events = claim.lock_evidence(
            bundles=bundles, registry=self._evidence_registry(),
            partnership=partnership, now=self._clock(),
        )
        stored: list[dict] = []
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "EVIDENCE_LOCKED":
                key = (f"lock:{claim_id}:{payload['round']}:"
                       f"{payload['evidence_id']}:{payload['fingerprint']}")
            else:
                key = (f"conflict:{claim_id}:{payload['evidence_id']}:"
                       f"{payload['submitted_fingerprint']}")
            stored.append(self._append(claim_id, event, idempotency_key=key))
        return stored

    def resolve_conflict(
        self,
        *,
        actor: Actor,
        claim_id: str,
        evidence_id: str,
        accepted_content: object,
        note: str,
    ) -> list[dict]:
        """联合管理委员会对同标识不同内容作出裁决后，按采纳内容锁定。

        采纳内容的锁定事件带 conflict_resolution；批准人不得是
        冲突任一方的提交者。
        """
        actor.require_permission("conflict.resolve")
        claim = self._claim(claim_id)
        if not claim.conflict or claim.conflict["evidence_id"] != evidence_id:
            raise DomainError("该证据不存在待解决的争议")
        registry = self._evidence_registry()
        prior = registry.get(evidence_id, [])
        involved = {rec["submitted_by"] for rec in prior}
        pending_submitter = claim.pending.get(evidence_id, {}).get("submitted_by")
        if pending_submitter:
            involved.add(pending_submitter)
        if actor.user_id in involved:
            raise PermissionDenied("争议材料的提交者不能裁决自己的证据冲突")
        return self._lock_evidence(claim_id=claim_id, bundles=[{
            "evidence_id": evidence_id,
            "content": accepted_content,
            "resolution": {"resolved_by": actor.user_id, "note": note},
        }])

    # ---------------------------------------------------------- 独立签署

    def sign_review(self, *, actor: Actor, claim_id: str, office: str) -> list[dict]:
        actor.require_office(office)
        claim = self._claim(claim_id)
        partnership = self._partnership(claim.partnership_id)
        events = claim.sign(
            office=office, signer=actor.user_id,
            partnership=partnership, now=self._clock(),
        )
        return self._append_claim_events(claim_id, events)

    def object_review(
        self, *, actor: Actor, claim_id: str, office: str, reason: str,
    ) -> list[dict]:
        actor.require_office(office)
        claim = self._claim(claim_id)
        partnership = self._partnership(claim.partnership_id)
        events = claim.object(
            office=office, signer=actor.user_id, reason=reason,
            partnership=partnership, now=self._clock(),
        )
        return self._append_claim_events(claim_id, events)

    def expire_due(self, *, now: Optional[datetime] = None) -> list[dict]:
        """系统巡检：证据齐备但期限届满仍未签齐的候选按未通过处理。"""
        moment = now or self._clock()
        result: list[dict] = []
        for claim_id in self._claim_ids():
            claim = self._claim(claim_id)
            partnership = self._partnership(claim.partnership_id)
            event = claim.timeout_decision(partnership=partnership, now=moment)
            if event is not None:
                result.append(self._append_claim_events(claim_id, [event])[0])
        return result

    # ---------------------------------------------------------- 付款指令

    def order_payment(self, *, actor: Actor, claim_id: str, payment_id: str) -> dict:
        actor.require_permission("payment.order")
        claim = self._claim(claim_id)
        if not claim.is_decided:
            raise DomainError("候选尚未裁决，不能产生付款指令")
        decision = claim.latest_decision
        if decision["outcome"] != "achieved":
            raise DomainError("未通过里程碑不产生付款指令")
        if self._payment_for_round(claim_id, claim.round) is not None:
            raise DomainError("本轮付款指令已存在，付款事实不可重复或改写")
        amount = claim.payable_for(self._partnership(claim.partnership_id), decision)
        payment = Payment(payment_id)
        event = payment.order(
            claim=claim, decision=decision, amount=amount,
            kind="milestone", adjudication_id=None, now=self._clock(),
        )
        return self._append(payment_id, event,
                            idempotency_key=f"payment:{payment_id}")

    # ---------------------------------------------------------- 新裁决与差额

    def request_adjudication(
        self,
        *,
        actor: Actor,
        claim_id: str,
        adjudication_id: str,
        reason: str,
        correction_packages: Optional[list[dict]] = None,
        territories: Optional[list[str]] = None,
        new_rules_version: Optional[int] = None,
        codev_share_bp: Optional[int] = None,
    ) -> dict:
        """数据纠正、区域退出或共同开发选择权变化：创建新裁决轮次。

        不改写既有裁决与付款；差额在新一轮签署后清算。
        """
        actor.require_permission("adjudication.request")
        if self.store.exists(adjudication_id):
            raise DomainError(f"裁决 {adjudication_id} 已登记")
        claim = self._claim(claim_id)
        partnership = self._partnership(claim.partnership_id)
        adjudication = Adjudication(adjudication_id)
        created = adjudication.create(
            claim=claim, reason=reason,
            requested_by=actor.user_id, now=self._clock(),
        )
        manifests = [
            {
                "evidence_id": p["evidence_id"],
                "evidence_type": p["evidence_type"],
                "submitted_by": p.get("submitted_by", actor.user_id),
            }
            for p in (correction_packages or [])
        ]
        reopened = claim.request_adjudication(
            adjudication_id=adjudication_id, reason=reason,
            partnership=partnership, now=self._clock(),
            correction_manifests=manifests,
            territories=territories, new_rules_version=new_rules_version,
            codev_share_bp=codev_share_bp,
        )
        self._append(adjudication_id, created,
                     idempotency_key=f"adjudication:{adjudication_id}")
        self._append(claim_id, reopened,
                     idempotency_key=f"reopen:{claim_id}:{adjudication_id}")
        locked: list[dict] = []
        if correction_packages:
            locked = self._lock_evidence(claim_id=claim_id, bundles=[
                {"evidence_id": p["evidence_id"], "content": p["content"]}
                for p in correction_packages
            ])
        return {"reopened": self.store.stream(claim_id)[-1], "evidence_locked": locked}

    def settle_adjudication(
        self,
        *,
        actor: Actor,
        adjudication_id: str,
        adjustment_payment_id: Optional[str] = None,
    ) -> dict:
        """新裁决完成签署后计算差额并清算。

        已发出的付款不被改写：正差额产生新的补付指令，
        负差额登记为应追回（clawback），零差额仅留痕。
        """
        actor.require_permission("adjudication.settle")
        adjudication = self._adjudication(adjudication_id)
        if adjudication.is_settled:
            raise DomainError("裁决差额已清算，不可重复处理")
        claim = self._claim(adjudication.claim_id)
        prior_round, new_round = adjudication.rounds
        prior_decision = claim.decisions[prior_round - 1]
        if not claim.is_decided or claim.round != new_round:
            raise DomainError("新裁决轮次尚未裁决，不能清算差额")
        prior_payment = self._payment_for_round(claim.claim_id, prior_round)
        prior_paid = Money(
            prior_payment["amount_minor"] if prior_payment else 0,
            prior_decision["currency"], prior_decision["fx_basis"],
        )
        decision = claim.latest_decision
        adjustment_event_id: Optional[str] = None
        if (
            decision["outcome"] == "achieved"
            and decision["payable_amount_minor"] > prior_paid.amount_minor
        ):
            if adjustment_payment_id is None:
                raise DomainError("存在正差额，必须提供补付指令编号")
            delta = Money(
                decision["payable_amount_minor"] - prior_paid.amount_minor,
                decision["currency"], decision["fx_basis"],
            )
            payment = Payment(adjustment_payment_id)
            event = payment.order(
                claim=claim, decision=decision, amount=delta,
                kind="adjustment_top_up",
                adjudication_id=adjudication_id, now=self._clock(),
            )
            stored = self._append(
                adjustment_payment_id, event,
                idempotency_key=f"payment:{adjustment_payment_id}",
            )
            adjustment_event_id = stored["event_id"]
        elif adjustment_payment_id is not None:
            raise DomainError("非正差额不得发起补付指令；负差额登记为追回")
        event = adjudication.settle(
            claim=claim, prior_paid=prior_paid,
            adjustment_payment_id=adjustment_event_id,
            now=self._clock(),
        )
        return self._append(adjudication_id, event,
                            idempotency_key=f"settle:{adjudication_id}")

    # ---------------------------------------------------------- 对外披露

    def disclosure_policy(self, *, claim_id: str) -> dict:
        claim = self._claim(claim_id)
        decision = claim.latest_decision
        if decision is None:
            raise DomainError("候选尚未裁决，无披露结论")
        return {
            "claim_id": claim_id,
            "policy": decision["disclosure"]["external"],
            "rules_version": decision["rules_version"],
            "achieved": decision["outcome"] == "achieved",
        }

    def assert_external_disclosure(
        self, *, actor: Actor, claim_id: str, jsc_approved: bool = False,
    ) -> None:
        """按冻结在裁决上的披露口径校验对外发布；系统只判定不代发。"""
        actor.require_permission("disclosure.request")
        policy = self.disclosure_policy(claim_id=claim_id)["policy"]
        if policy == "prohibited":
            raise PermissionDenied("冻结规则禁止该里程碑对外披露")
        if policy == "jsc_approval":
            if not jsc_approved:
                raise PermissionDenied("该披露须附联合管理委员会批准记录")
            if not (actor.is_party_admin or "disclosure.approve" in actor.permissions):
                raise PermissionDenied("批准记录须来自授权双方代表")

    # ---------------------------------------------------------- 反向核对

    def trace_from_payment(self, *, payment_id: str, actor: Actor) -> dict:
        payment = self._payment(payment_id)
        trace = self.trace_claim(claim_id=payment["claim_id"], actor=actor)
        trace["entry_point"] = {
            "payment_id": payment_id, "payment_event_id": payment["event_id"],
        }
        return trace

    def trace_from_failed_milestone(self, *, claim_id: str, actor: Actor) -> dict:
        trace = self.trace_claim(claim_id=claim_id, actor=actor)
        if trace["rounds"][-1]["outcome"] != "not_achieved":
            raise DomainError("该候选最新一轮并非未通过")
        trace["entry_point"] = {"failed_claim_id": claim_id}
        return trace

    def trace_claim(self, *, claim_id: str, actor: Actor) -> dict:
        """从任一里程碑（通过或未通过）反向核对权利、证据、签署与纠正历史。"""
        claim = self._claim(claim_id)
        if not actor.can_see_project(claim.project_code):
            raise PermissionDenied("该候选项目超出操作者职责范围")
        partnership = self._partnership(claim.partnership_id)
        payments = [
            self._payment(e["aggregate_id"])
            for e in self.store.all_events()
            if (e["event_type"] == "PAYMENT_ORDERED"
                and e["payload"]["claim_id"] == claim_id)
        ]
        reopen_events = {
            e["payload"]["adjudication_id"]: e
            for e in self.store.stream(claim_id)
            if e["event_type"] == "CLAIM_REOPENED"
        }
        adjudications = []
        for aid, reopen in reopen_events.items():
            adj = self._adjudication(aid)
            adjudications.append({
                "adjudication_id": aid,
                "reason": adj.reason,
                "requested_by": adj.requested_by,
                "rounds": adj.rounds,
                "reopen_event_id": reopen["event_id"],
                "settlement": adj.settlement,
            })
        view = {
            "claim_id": claim_id,
            "partnership_id": claim.partnership_id,
            "project_code": claim.project_code,
            "milestone_code": claim.milestone_code,
            "submitted_by": claim.submitted_by,
            "submit_event_id": claim.submit_event_id,
            "rights": {
                "parties": partnership.parties,
                "rules_versions_used": [
                    {
                        "round": round_no,
                        "rules_version": rv,
                        "territories": claim.territories_by_round[round_no],
                        "codev_share_bp": claim.codev_by_round[round_no],
                        "snapshot_effective_from":
                            partnership.rules_at(rv)["effective_from"],
                    }
                    for round_no, rv in sorted(claim.rules_version_by_round.items())
                ],
            },
            "evidence": [
                {
                    key: lock.get(key)
                    for key in ("evidence_id", "evidence_type", "fingerprint",
                                "submitted_by", "locked_at", "round",
                                "reused_from", "correction_of",
                                "conflict_resolution")
                    if lock.get(key) is not None
                }
                for lock in claim.locks
            ],
            "conflict": claim.conflict,
            "rounds": [
                {
                    "round": d["round"],
                    "outcome": d["outcome"],
                    "territories": list(d["territories"]),
                    "reason_code": d["reason_code"],
                    "reason": d["reason"],
                    "decided_at": d["decided_at"],
                    "rules_version": d["rules_version"],
                    "currency": d["currency"],
                    "fx_basis": d["fx_basis"],
                    "gross_amount_minor": d["gross_amount_minor"],
                    "factor_bp": d["factor_bp"],
                    "payable_amount_minor": d["payable_amount_minor"],
                    "decision_event_id": claim.decision_event_ids.get(d["round"]),
                    "deadline_at": (
                        claim.deadline_at(partnership, d["round"]).isoformat()
                        if claim.locks else None
                    ),
                    "signatures": d["signatures"],
                    "objections": d["objections"],
                    "evidence_fingerprints": d["evidence_fingerprints"],
                    "disclosure": d["disclosure"],
                }
                for d in claim.decisions
            ],
            "payments": sorted(payments, key=lambda p: p["ordered_at"]),
            "adjudications": adjudications,
        }
        return redact(view, actor)

    def list_visible_claims(self, *, actor: Actor) -> list[dict]:
        items = []
        for claim_id in self._claim_ids():
            claim = self._claim(claim_id)
            if not actor.can_see_project(claim.project_code):
                continue
            latest = claim.latest_decision
            items.append({
                "claim_id": claim_id,
                "project_code": claim.project_code,
                "milestone_code": claim.milestone_code,
                "round": claim.round,
                "state": (
                    "conflict" if claim.conflict else
                    "decided" if claim.is_decided else
                    "awaiting_review" if claim.locks and not claim.pending else
                    "evidence_incomplete"
                ),
                "outcome": latest["outcome"] if latest else None,
            })
        return items

    # ---------------------------------------------------------- 重建辅助

    def _append(
        self, aggregate_id: str, event: dict,
        *, idempotency_key: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> dict:
        if expected_version is None:
            expected_version = self.store.stream_version(aggregate_id)
        version = expected_version + 1
        stamped = {
            "event_id": f"{aggregate_id}:{version}",
            "event_type": event["event_type"],
            "occurred_at": self._clock().astimezone().isoformat(),
            "aggregate_id": aggregate_id,
            "version": version,
            "payload": event["payload"],
        }
        return self.store.append(
            stamped, expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def _append_claim_events(self, claim_id: str, events: list[dict]) -> list[dict]:
        stored = []
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "REVIEW_SIGNED":
                key = f"sign:{claim_id}:{payload['round']}:{payload['office']}"
            elif event["event_type"] == "REVIEW_OBJECTED":
                key = f"object:{claim_id}:{payload['round']}:{payload['office']}"
            else:
                key = f"decide:{claim_id}:{payload['round']}"
            stored.append(self._append(claim_id, event, idempotency_key=key))
        return stored

    def _partnership(self, partnership_id: str) -> Partnership:
        partnership = Partnership(partnership_id)
        for event in self.store.stream(partnership_id):
            partnership.apply(event)
        return partnership

    def _claim(self, claim_id: str) -> Claim:
        claim = Claim(claim_id)
        for event in self.store.stream(claim_id):
            claim.apply(event)
        if not claim.round:
            raise DomainError(f"候选 {claim_id} 不存在")
        return claim

    def _adjudication(self, adjudication_id: str) -> Adjudication:
        adjudication = Adjudication(adjudication_id)
        for event in self.store.stream(adjudication_id):
            adjudication.apply(event)
        if adjudication.rounds is None:
            raise DomainError(f"裁决 {adjudication_id} 不存在")
        return adjudication

    def _payment(self, payment_id: str) -> dict:
        for event in self.store.query("PAYMENT_ORDERED"):
            if event["aggregate_id"] == payment_id:
                return {**event["payload"], "event_id": event["event_id"]}
        raise DomainError(f"付款指令 {payment_id} 不存在")

    def _payment_for_round(self, claim_id: str, round_no: int) -> Optional[dict]:
        for event in self.store.query("PAYMENT_ORDERED"):
            payload = event["payload"]
            if payload["claim_id"] == claim_id and payload["round"] == round_no:
                return {**payload, "event_id": event["event_id"]}
        return None

    def _claim_ids(self) -> list[str]:
        return [
            e["aggregate_id"]
            for e in self.store.all_events()
            if e["event_type"] == "CLAIM_SUBMITTED"
        ]

    def _evidence_registry(self) -> dict[str, list[dict]]:
        """全局证据登记表：证据编号 -> 历次锁定记录（证据锁事件属于候选流）。"""
        registry: dict[str, list[dict]] = defaultdict(list)
        for event in self.store.query("EVIDENCE_LOCKED"):
            payload = event["payload"]
            registry[payload["evidence_id"]].append({
                "fingerprint": payload["fingerprint"],
                "claim_id": event["aggregate_id"],
                "round": payload["round"],
                "submitted_by": payload["submitted_by"],
                "event_id": event["event_id"],
            })
        return registry

    def _assert_identical_resubmission(
        self, *, claim_id: str, partnership_id: str, milestone_code: str,
        project_code: str, territories: list[str], evidence_packages: list[dict],
    ) -> None:
        claim = self._claim(claim_id)
        if claim.partnership_id != partnership_id:
            raise DomainError("候选编号已存在但合作不一致，禁止覆盖既有候选")
        if claim.milestone_code != milestone_code or claim.project_code != project_code:
            raise DomainError("候选编号已存在但里程碑/项目不一致，禁止覆盖既有候选")
        submitted = next(
            e for e in self.store.stream(claim_id)
            if e["event_type"] == "CLAIM_SUBMITTED"
        )["payload"]
        if submitted["territories"] != list(territories):
            raise DomainError("候选编号已存在但地区范围不一致，禁止覆盖既有候选")
        existing = {
            lk["evidence_id"]: lk["fingerprint"] for lk in claim.locks
        }
        incoming = {p["evidence_id"]: fingerprint(p["content"])
                    for p in evidence_packages}
        if set(existing) != set(incoming) or any(
            existing[k] != incoming[k] for k in existing
        ):
            raise DomainError("候选编号已存在但证据不一致；同标识不同内容须走证据争议")
