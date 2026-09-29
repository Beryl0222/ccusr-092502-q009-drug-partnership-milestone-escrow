"""创新药合作里程碑托管的领域核心。

设计要点：
- 事件溯源：所有业务事实只追加，付款指令一旦发出即不可变；
  数据纠正、区域退出、共同开发选择改变只能开新裁决、产生差额付款。
- 规则冻结：主张提交时冻结合作版本，证据截止时间、币种口径、计费周期
  在之后一律取冻结版本，跨时区/跨账期不受新版本登记影响。
- 候选达成：临床结果提交只是候选；科学、合规、财务在合同范围内独立签署，
  且提交者不得批准自己的材料，全部通过后才产生付款指令。
- 证据幂等：同一证据包标识且内容一致直接去重；同标识不同内容进入争议。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from .errors import (
    AuthorizationError,
    ConflictError,
    ValidationError,
    WorkflowError,
)
from .store import EventStore

SCOPES = ("scientific", "compliance", "finance")
ZERO = Decimal("0")


def _event(
    event_id: str,
    event_type: str,
    aggregate_id: str,
    version: int,
    payload: dict[str, Any],
    occurred_at: datetime | str,
) -> dict[str, Any]:
    if isinstance(occurred_at, datetime):
        if occurred_at.tzinfo is None:
            raise ValidationError("occurred_at 必须包含时区")
        occurred_at = occurred_at.isoformat()
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "aggregate_id": aggregate_id,
        "version": version,
        "payload": payload,
    }


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValidationError("时间必须包含时区")
    return parsed


def billing_period(occurred_at: datetime, fiscal_year_start_month: int) -> str:
    """按冻结版本的财年起始月计算计费周期标签（财年-季度）。"""
    month = occurred_at.month
    fiscal_year = occurred_at.year if month >= fiscal_year_start_month else occurred_at.year - 1
    shifted = (month - fiscal_year_start_month) % 12
    quarter = shifted // 3 + 1
    return f"FY{fiscal_year}-Q{quarter}"


class _Partnership:
    def __init__(self, event: dict[str, Any]) -> None:
        self.id = event["aggregate_id"]
        self.registered_at = event["occurred_at"]
        self.spec = event["payload"]["spec"]


class _Claim:
    """主张聚合的折叠状态。"""

    def __init__(self, events: Iterable[dict[str, Any]]) -> None:
        self.signatures: dict[str, dict[str, Any]] = {}
        self.all_signatures: list[dict[str, Any]] = []
        self.disputes: list[dict[str, Any]] = []
        self.disputed = False
        self.rejected: dict[str, Any] | None = None
        self.payment_id: str | None = None
        self.evidence: dict[str, Any] | None = None
        for event in events:
            self._apply(event)

    def _apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        etype = event["event_type"]
        if etype == "CLAIM_SUBMITTED":
            self.id = event["aggregate_id"]
            self.submitted = p
            self.created_at = event["occurred_at"]
        elif etype == "EVIDENCE_LOCKED":
            self.evidence = p
        elif etype == "DISPUTE_OPENED":
            self.disputed = True
            self.disputes.append(p)
        elif etype == "DISPUTE_RESOLVED":
            self.disputed = False
            self.disputes[-1]["resolution"] = p
        elif etype == "SIGNATURE_RECORDED":
            self.all_signatures.append(p)
            if p.get("decision", "APPROVE") == "REJECT":
                self.rejected = {"reason": p.get("reason", "签署未通过"), "at": event["occurred_at"]}
            else:
                self.signatures[p["scope"]] = p
        elif etype == "CLAIM_REJECTED":
            self.rejected = {"reason": p["reason"], "at": event["occurred_at"]}
        elif etype == "PAYMENT_ORDERED":
            self.payment_id = p["payment_instruction_id"]

    @property
    def status(self) -> str:
        if self.rejected is not None:
            return "REJECTED"
        if self.payment_id is not None:
            return "PAID"
        if self.disputed:
            return "IN_DISPUTE"
        if self.evidence is None:
            return "SUBMITTED"
        if len(self.signatures) < len(self.required_scopes):
            return "UNDER_REVIEW"
        return "APPROVED_PENDING_PAYMENT"

    @property
    def required_scopes(self) -> tuple[str, ...]:
        return tuple(self.submitted["frozen_milestone"]["scopes"])


class _Adjudication:
    def __init__(self, events: Iterable[dict[str, Any]]) -> None:
        self.settled: dict[str, Any] | None = None
        self.opened: dict[str, Any] | None = None
        for event in events:
            if event["event_type"] == "ADJUDICATION_OPENED":
                self.id = event["aggregate_id"]
                self.opened = event["payload"]
            elif event["event_type"] == "ADJUDICATION_SETTLED":
                self.settled = event["payload"]


class MilestoneEscrow:
    """托管服务的应用门面；方法返回新事件的关键标识。"""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()
        self._refresh_indices()

    # ---- 读模型索引（每次命令前从只追加事件重建，保证判定基于事实）----

    def _refresh_indices(self) -> None:
        self._versions: dict[str, _Partnership] = {}
        self._latest_version: str | None = None
        self._claims: dict[str, _Claim] = {}
        self._adjudications: dict[str, _Adjudication] = {}
        self._package_owner: dict[str, str] = {}
        self._payments: dict[str, dict[str, Any]] = {}
        self._claim_payments: dict[str, list[dict[str, Any]]] = {}
        buckets: dict[str, list[dict[str, Any]]] = {}
        for event in self.store.all_events():
            agg = event["aggregate_id"]
            etype = event["event_type"]
            if etype == "PARTNERSHIP_VERSION_REGISTERED":
                self._versions[agg] = _Partnership(event)
                self._latest_version = agg
            elif etype == "PAYMENT_ORDERED" and agg.startswith("payment_instruction-"):
                self._payments[agg] = event
                self._claim_payments.setdefault(event["payload"]["root_claim_id"], []).append(event)
            else:
                buckets.setdefault(agg, []).append(event)
                if etype == "CLAIM_SUBMITTED":
                    self._package_owner[event["payload"]["evidence_package_id"]] = agg
        for agg_id, stream in buckets.items():
            if stream and stream[0]["event_type"] == "CLAIM_SUBMITTED":
                claim = _Claim(stream)
                paid = self._claim_payments.get(agg_id, ())
                if paid:
                    claim.payment_id = paid[0]["payload"]["payment_instruction_id"]
                self._claims[agg_id] = claim
            elif stream and stream[0]["event_type"] == "ADJUDICATION_OPENED":
                self._adjudications[agg_id] = _Adjudication(stream)

    def _append(self, event: dict[str, Any]) -> None:
        self.store.append(event, self.store.version_of(event["aggregate_id"]))
        self._refresh_indices()

    # ---- 合作版本登记 ----

    def register_partnership_version(
        self,
        *,
        event_id: str,
        partnership_version_id: str,
        occurred_at: datetime | str,
        spec: dict[str, Any],
    ) -> str:
        self._validate_spec(spec)
        if partnership_version_id in self._versions:
            raise ConflictError(f"合作版本已登记: {partnership_version_id}")
        self._append(
            _event(
                event_id,
                "PARTNERSHIP_VERSION_REGISTERED",
                partnership_version_id,
                1,
                {"spec": spec},
                occurred_at,
            )
        )
        return partnership_version_id

    @staticmethod
    def _validate_spec(spec: dict[str, Any]) -> None:
        for field in ("parties", "projects", "members", "decision_rights",
                      "review_deadline_days", "billing", "milestones", "disclosure"):
            if field not in spec:
                raise ValidationError(f"合作版本缺少条款: {field}")
        if len(spec["parties"]) < 2:
            raise ValidationError("合作版本须至少登记双方")
        for member_id, member in spec["members"].items():
            if member.get("party") not in spec["parties"]:
                raise ValidationError(f"成员 {member_id} 未归属到已登记合作方")
        billing = spec["billing"]
        if not (1 <= int(billing.get("fiscal_year_start_month", 0)) <= 12):
            raise ValidationError("计费条款须给出 1-12 月的财年起始月")
        for scope in SCOPES:
            if scope not in spec["decision_rights"]:
                raise ValidationError(f"决策配置缺少签署范围: {scope}")
            if not spec["decision_rights"][scope]:
                raise ValidationError(f"签署范围 {scope} 必须至少有一名授权成员")
            if scope not in spec["review_deadline_days"]:
                raise ValidationError(f"审阅期限缺少范围: {scope}")
        for code, ms in spec["milestones"].items():
            for field in ("projects", "region_amounts", "currency", "scopes"):
                if field not in ms:
                    raise ValidationError(f"里程碑 {code} 缺少 {field}")
            for scope in ms["scopes"]:
                if scope not in SCOPES:
                    raise ValidationError(f"里程碑 {code} 含未知签署范围: {scope}")

    # ---- 候选主张与证据 ----

    def submit_claim(
        self,
        *,
        event_id: str,
        claim_id: str,
        partnership_version_id: str | None = None,
        milestone_code: str,
        project_id: str,
        achieved_regions: list[str],
        submitter: str,
        evidence_package_id: str,
        content_hash: str,
        occurred_at: datetime | str,
    ) -> str:
        """登记候选达成。返回 claim_id；证据包重复时幂等返回既有主张。"""
        ts = occurred_at if isinstance(occurred_at, datetime) else _parse_ts(occurred_at)
        version_id = partnership_version_id or self._latest_version
        if version_id is None or version_id not in self._versions:
            raise ValidationError("合作版本不存在，无法提交主张")
        version = self._versions[version_id]
        spec = version.spec
        if submitter not in spec["members"]:
            raise AuthorizationError(f"未知提交成员: {submitter}")
        milestone = spec["milestones"].get(milestone_code)
        if milestone is None:
            raise ValidationError(f"冻结版本 {version_id} 无此里程碑: {milestone_code}")
        if project_id not in milestone["projects"]:
            raise ValidationError(f"里程碑 {milestone_code} 不覆盖项目 {project_id}")
        if not achieved_regions or any(r not in milestone["region_amounts"] for r in achieved_regions):
            raise ValidationError("达成地区为空或超出该里程碑的地区权利范围")

        # 证据包幂等：同标同内容去重，同标不同内容进争议。
        existing_id = self._package_owner.get(evidence_package_id)
        if existing_id is not None:
            existing = self._claims[existing_id]
            if existing.rejected is not None:
                raise ConflictError("证据包已随被驳回主张失效，请使用新证据包标识重新提交")
            locked_hash = (existing.evidence or existing.submitted)["content_hash"]
            if locked_hash == content_hash:
                return existing_id
            if existing.disputed:
                raise ConflictError("同标识证据包内容冲突的争议尚未解决")
            self._append(
                _event(
                    event_id,
                    "DISPUTE_OPENED",
                    existing_id,
                    self.store.version_of(existing_id) + 1,
                    {
                        "evidence_package_id": evidence_package_id,
                        "original_hash": locked_hash,
                        "conflicting_hash": content_hash,
                        "reason": "SAME_PACKAGE_ID_DIFFERENT_CONTENT",
                        "submitted_by": submitter,
                    },
                    ts,
                )
            )
            return existing_id

        period = billing_period(ts, spec["billing"]["fiscal_year_start_month"])
        payload = {
            "partnership_version_id": version_id,  # 规则冻结点
            "milestone_code": milestone_code,
            "project_id": project_id,
            "achieved_regions": list(achieved_regions),
            "submitter": submitter,
            "evidence_package_id": evidence_package_id,
            "content_hash": content_hash,
            "billing_period": period,
            "frozen_milestone": {
                "region_amounts": dict(milestone["region_amounts"]),
                "currency": milestone["currency"],
                "currency_basis": milestone.get("currency_basis", "STANDARD"),
                "co_dev_payee_share": str(milestone.get("co_dev_payee_share", "1")),
                "scopes": list(milestone["scopes"]),
                "evidence_kinds": list(milestone.get("evidence_kinds", ())),
            },
        }
        self._append(_event(event_id, "CLAIM_SUBMITTED", claim_id, 1, payload, ts))
        return claim_id

    def lock_evidence(
        self,
        *,
        event_id: str,
        claim_id: str,
        occurred_at: datetime | str,
        evidence_kinds: list[str] | None = None,
    ) -> None:
        claim = self._require_open_claim(claim_id)
        if claim.evidence is not None:
            raise ConflictError("证据已锁定，不可重复锁定")
        ts = occurred_at if isinstance(occurred_at, datetime) else _parse_ts(occurred_at)
        version = self._versions[claim.submitted["partnership_version_id"]]
        days = version.spec["review_deadline_days"]
        frozen = claim.submitted["frozen_milestone"]
        required_kinds = frozen["evidence_kinds"]
        kinds = evidence_kinds if evidence_kinds is not None else required_kinds
        missing = set(required_kinds) - set(kinds)
        if missing:
            raise ValidationError(f"证据种类不满足冻结条款: {sorted(missing)}")
        deadlines = {
            scope: (ts + timedelta(days=days[scope])).isoformat()
            for scope in frozen["scopes"]
        }
        self._append(
            _event(
                event_id,
                "EVIDENCE_LOCKED",
                claim_id,
                self.store.version_of(claim_id) + 1,
                {
                    "evidence_package_id": claim.submitted["evidence_package_id"],
                    "content_hash": claim.submitted["content_hash"],
                    "evidence_kinds": kinds,
                    "locked_at": ts.isoformat(),
                    "deadlines": deadlines,  # 按冻结期限与时区计算
                    "rule_version": claim.submitted["partnership_version_id"],
                    "billing_period": claim.submitted["billing_period"],
                },
                ts,
            )
        )

    def resolve_dispute(
        self,
        *,
        event_id: str,
        claim_id: str,
        resolution: str,
        resolved_by: str,
        occurred_at: datetime | str,
        note: str = "",
    ) -> None:
        """CONFIRM_ORIGINAL 维持原证据；REPLACE_WITH_CORRECTED 驳回候选，须重新提交。"""
        claim = self._claims.get(claim_id)
        if claim is None:
            raise ValidationError(f"主张不存在: {claim_id}")
        if claim.rejected is not None or claim.payment_id is not None:
            raise WorkflowError("主张已终结，不能再处理争议")
        if not claim.disputed:
            raise WorkflowError("该主张不存在待决争议")
        if resolution not in ("CONFIRM_ORIGINAL", "REPLACE_WITH_CORRECTED"):
            raise ValidationError("未知争议裁决方式")
        ts = occurred_at if isinstance(occurred_at, datetime) else _parse_ts(occurred_at)
        next_version = self.store.version_of(claim_id) + 1
        self._append(
            _event(
                event_id,
                "DISPUTE_RESOLVED",
                claim_id,
                next_version,
                {"resolution": resolution, "resolved_by": resolved_by, "note": note},
                ts,
            )
        )
        if resolution == "REPLACE_WITH_CORRECTED":
            self._append(
                _event(
                    event_id + "-reject",
                    "CLAIM_REJECTED",
                    claim_id,
                    next_version + 1,
                    {"reason": "SUPERSEDED_AFTER_DISPUTE"},
                    ts,
                )
            )

    # ---- 独立签署 ----

    def record_signature(
        self,
        *,
        event_id: str,
        claim_id: str,
        scope: str,
        signer: str,
        occurred_at: datetime | str,
        decision: str = "APPROVE",
        reason: str = "",
    ) -> str | None:
        """记录一个合同范围的独立签署；全部通过时产生并返回付款指令标识。"""
        claim = self._require_open_claim(claim_id)
        p = claim.submitted
        version = self._versions[p["partnership_version_id"]]
        spec = version.spec
        if scope not in claim.required_scopes:
            raise ValidationError(f"该里程碑不要求此签署范围: {scope}")
        if scope in claim.signatures:
            raise ConflictError(f"{scope} 已签署，不可重复签署")
        if signer not in spec["members"]:
            raise AuthorizationError(f"未知签署成员: {signer}")
        if signer == p["submitter"]:
            raise AuthorizationError("提交者不得批准自己提交的材料")
        if signer not in spec["decision_rights"][scope]:
            raise AuthorizationError(f"{signer} 不具备 {scope} 范围的签署权")
        used = {s["signer"] for s in claim.signatures.values()}
        if signer in used:
            raise AuthorizationError("同一成员不得跨范围重复签署，各范围须独立")
        if claim.evidence is None:
            raise WorkflowError("证据尚未锁定，不能签署")
        ts = occurred_at if isinstance(occurred_at, datetime) else _parse_ts(occurred_at)
        deadline = _parse_ts(claim.evidence["deadlines"][scope])
        if ts > deadline:
            raise WorkflowError(f"{scope} 审阅期限已过（冻结期限 {deadline.isoformat()}）")

        next_version = self.store.version_of(claim_id) + 1
        self._append(
            _event(
                event_id,
                "SIGNATURE_RECORDED",
                claim_id,
                next_version,
                {"scope": scope, "signer": signer, "decision": decision,
                 "reason": reason, "signed_at": ts.isoformat()},
                ts,
            )
        )
        if decision == "REJECT":
            self._append(
                _event(
                    event_id + "-reject",
                    "CLAIM_REJECTED",
                    claim_id,
                    next_version + 1,
                    {"reason": reason or f"{scope}_DENIED", "denied_by_scope": scope},
                    ts,
                )
            )
            return None
        if len(self._claims[claim_id].signatures) == len(claim.required_scopes):
            return self._pay_initial(event_id + "-payment", claim_id, ts)
        return None

    def expire_claim(self, *, event_id: str, claim_id: str, at: datetime | str) -> None:
        """超过冻结审阅期限仍有范围未签署的候选，标记未通过。"""
        claim = self._require_open_claim(claim_id)
        ts = at if isinstance(at, datetime) else _parse_ts(at)
        if claim.evidence is None:
            raise WorkflowError("证据尚未锁定")
        pending = [s for s in claim.required_scopes if s not in claim.signatures]
        overdue = [s for s in pending if ts > _parse_ts(claim.evidence["deadlines"][s])]
        if not overdue:
            raise WorkflowError("仍在冻结审阅期限内，不能过期处理")
        self._append(
            _event(
                event_id,
                "CLAIM_REJECTED",
                claim_id,
                self.store.version_of(claim_id) + 1,
                {"reason": "REVIEW_DEADLINE_EXCEEDED", "pending_scopes": overdue},
                ts,
            )
        )

    def _require_open_claim(self, claim_id: str) -> _Claim:
        claim = self._claims.get(claim_id)
        if claim is None:
            raise ValidationError(f"主张不存在: {claim_id}")
        if claim.rejected is not None:
            raise WorkflowError(f"主张已被驳回: {claim.rejected['reason']}")
        if claim.disputed:
            raise WorkflowError("主张处于争议中，须先解决争议")
        if claim.payment_id is not None:
            raise WorkflowError("主张已产生付款指令，后续变更须走裁决")
        return claim

    # ---- 付款（不可变事实）----

    @staticmethod
    def _payable(milestone: dict[str, Any], regions: list[str], share: Decimal | None = None) -> Decimal:
        ratio = share if share is not None else Decimal(milestone["co_dev_payee_share"])
        total = sum((Decimal(milestone["region_amounts"][r]) for r in regions), ZERO)
        return (total * ratio).quantize(Decimal("0.01"))

    def _pay_initial(self, event_id: str, claim_id: str, ts: datetime) -> str:
        claim = self._claims[claim_id]
        frozen = claim.submitted["frozen_milestone"]
        amount = self._payable(frozen, claim.submitted["achieved_regions"])
        payment_id = f"payment_instruction-{event_id}"
        self._append(
            _event(
                event_id,
                "PAYMENT_ORDERED",
                payment_id,
                1,
                {
                    "payment_instruction_id": payment_id,
                    "origin": "CLAIM",
                    "root_claim_id": claim_id,
                    "source_claim_id": claim_id,
                    "kind": "INITIAL",
                    "amount": str(amount),
                    "currency": frozen["currency"],
                    "currency_basis": frozen["currency_basis"],
                    "billing_period": claim.submitted["billing_period"],
                    "rule_version": claim.submitted["partnership_version_id"],
                },
                ts,
            )
        )
        return payment_id

    # ---- 裁决：数据纠正 / 区域退出 / 共同开发选择改变 ----

    def open_adjudication(
        self,
        *,
        event_id: str,
        adjudication_id: str,
        claim_id: str,
        reason: str,
        initiated_by: str,
        occurred_at: datetime | str,
        achieved_regions: list[str] | None = None,
        co_dev_payee_share: str | None = None,
        eligible: bool = True,
        note: str = "",
    ) -> str:
        if reason not in ("DATA_CORRECTION", "REGIONAL_EXIT", "CODEVELOPMENT_OPTION_CHANGE"):
            raise ValidationError(f"未知裁决原因: {reason}")
        claim = self._claims.get(claim_id)
        if claim is None:
            raise ValidationError(f"主张不存在: {claim_id}")
        if claim.payment_id is None:
            raise WorkflowError("只有已付款的里程碑才需要裁决差额")
        for adj in self._adjudications.values():
            if adj.opened and adj.opened["claim_id"] == claim_id and adj.settled is None:
                raise ConflictError("该主张已有未结清裁决")
        ts = occurred_at if isinstance(occurred_at, datetime) else _parse_ts(occurred_at)
        change: dict[str, Any] = {"eligible": eligible}
        if achieved_regions is not None:
            frozen = claim.submitted["frozen_milestone"]
            if any(r not in frozen["region_amounts"] for r in achieved_regions):
                raise ValidationError("纠正后的地区超出冻结权利范围")
            change["achieved_regions"] = achieved_regions
        if co_dev_payee_share is not None:
            ratio = Decimal(co_dev_payee_share)
            if not ZERO <= ratio <= 1:
                raise ValidationError("共同开发收款份额必须在 0 到 1 之间")
            change["co_dev_payee_share"] = str(ratio)
        self._append(
            _event(
                event_id,
                "ADJUDICATION_OPENED",
                adjudication_id,
                1,
                {
                    "claim_id": claim_id,
                    "reason": reason,
                    "initiated_by": initiated_by,
                    "change": change,
                    "note": note,
                },
                ts,
            )
        )
        return adjudication_id

    def settle_adjudication(
        self,
        *,
        event_id: str,
        adjudication_id: str,
        occurred_at: datetime | str,
        approved_by: list[str],
    ) -> str | None:
        adj = self._adjudications.get(adjudication_id)
        if adj is None or adj.opened is None:
            raise ValidationError(f"裁决不存在: {adjudication_id}")
        if adj.settled is not None:
            raise ConflictError("裁决已结清")
        claim = self._claims[adj.opened["claim_id"]]
        version = self._versions[claim.submitted["partnership_version_id"]]
        if len(approved_by) != len(set(approved_by)):
            raise AuthorizationError("裁决批准人不得重复")
        parties = set()
        for member_id in approved_by:
            if member_id not in version.spec["members"]:
                raise AuthorizationError(f"未知裁决成员: {member_id}")
            if member_id == claim.submitted["submitter"]:
                raise AuthorizationError("提交者不得批准自己材料引发的裁决")
            parties.add(version.spec["members"][member_id]["party"])
        if parties != set(version.spec["parties"]):
            raise AuthorizationError("差额裁决须经双方各自独立授权成员确认")

        ts = occurred_at if isinstance(occurred_at, datetime) else _parse_ts(occurred_at)
        frozen = claim.submitted["frozen_milestone"]
        change = adj.opened["change"]
        regions = change.get("achieved_regions", claim.submitted["achieved_regions"])
        share = Decimal(change["co_dev_payee_share"]) if "co_dev_payee_share" in change else None
        new_payable = ZERO if not change["eligible"] else self._payable(frozen, regions, share)
        paid = sum(
            (
                Decimal(e["payload"]["amount"])
                if e["payload"]["kind"] != "RECOVERY"
                else -Decimal(e["payload"]["amount"])
                for e in self._claim_payments.get(claim.id, ())
            ),
            ZERO,
        )
        delta = (new_payable - paid).quantize(Decimal("0.01"))
        if delta > ZERO:
            kind = "ADDITIONAL"
        elif delta < ZERO:
            kind = "RECOVERY"
        else:
            kind = "NONE"
        next_version = self.store.version_of(adjudication_id) + 1
        self._append(
            _event(
                event_id,
                "ADJUDICATION_SETTLED",
                adjudication_id,
                next_version,
                {
                    "claim_id": claim.id,
                    "rule_version": claim.submitted["partnership_version_id"],
                    "recomputed_payable": str(new_payable),
                    "previously_paid": str(paid),
                    "delta": str(delta),
                    "currency": frozen["currency"],
                    "currency_basis": frozen["currency_basis"],
                    "kind": kind,
                    "billing_period": claim.submitted["billing_period"],
                    "approved_by": list(approved_by),
                },
                ts,
            )
        )
        if kind == "NONE":
            return None
        payment_id = f"payment_instruction-{event_id}"
        self._append(
            _event(
                event_id + "-payment",
                "PAYMENT_ORDERED",
                payment_id,
                1,
                {
                    "payment_instruction_id": payment_id,
                    "origin": "ADJUDICATION",
                    "root_claim_id": claim.id,
                    "source_adjudication_id": adjudication_id,
                    "kind": kind,
                    "amount": str(abs(delta)),
                    "currency": frozen["currency"],
                    "currency_basis": frozen["currency_basis"],
                    "billing_period": claim.submitted["billing_period"],
                    "rule_version": claim.submitted["partnership_version_id"],
                },
                ts,
            )
        )
        return payment_id

    # ---- 查询 ----

    def get_claim(self, claim_id: str) -> _Claim:
        if claim_id not in self._claims:
            raise ValidationError(f"主张不存在: {claim_id}")
        return self._claims[claim_id]

    def get_payment(self, payment_instruction_id: str) -> dict[str, Any]:
        if payment_instruction_id not in self._payments:
            raise ValidationError(f"付款指令不存在: {payment_instruction_id}")
        return self._payments[payment_instruction_id]

    def claim_payments(self, claim_id: str) -> list[dict[str, Any]]:
        return list(self._claim_payments.get(claim_id, ()))

    def claim_adjudications(self, claim_id: str) -> list[_Adjudication]:
        return [a for a in self._adjudications.values() if a.opened and a.opened["claim_id"] == claim_id]
