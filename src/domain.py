"""领域聚合：合作版本、里程碑候选、裁决、付款指令。

聚合方法是纯命令：只读折叠状态、返回事件列表，不自行改写状态；
状态一律由 apply() 从事件流重建。所有时间为带时区绝对时刻，
审阅截止时刻由“本轮证据齐备时刻 + 冻结规则中的期限天数”确定，
可随时从事件重算，跨时区与计费周期不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .canonical import fingerprint
from .errors import DomainError
from .money import Money

OFFICES = ("science", "compliance", "finance")
REASONS = ("data_correction", "territory_exit", "co_development_change")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise DomainError("时间必须包含时区")
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise DomainError("存储的时间缺少时区")
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------- 合作版本

@dataclass
class Partnership:
    partnership_id: str
    version: int = 0
    parties: list[str] = field(default_factory=list)
    versions: list[dict] = field(default_factory=list)

    @property
    def current(self) -> dict:
        if not self.versions:
            raise DomainError(f"合作 {self.partnership_id} 尚未登记任何版本")
        return self.versions[-1]

    def exists(self, version: int) -> bool:
        return any(item["version"] == version for item in self.versions)

    def rules_at(self, version: Optional[int] = None) -> dict:
        """取冻结的规则版本快照；默认当前版本。"""
        target = self.version if version is None else version
        for item in self.versions:
            if item["version"] == target:
                return item
        raise DomainError(f"合作规则版本 {target} 不存在")

    def register_version(
        self,
        *,
        rules: dict,
        parties: Optional[list[str]],
        effective_from: datetime,
        now: datetime,
    ) -> dict:
        self._validate_rules(rules)
        next_version = self.version + 1
        final_parties = self.parties or parties or []
        if next_version == 1:
            if not parties:
                raise DomainError("首个合作版本必须登记双方主体")
        elif parties is not None:
            raise DomainError("合作主体在首个版本之后不可变更")
        if self.versions and (
            rules["currency"], rules["fx_basis"]
        ) != (
            self.versions[0]["rules"]["currency"],
            self.versions[0]["rules"]["fx_basis"],
        ):
            raise DomainError("币种与口径在合作版本之间不可变更，差额须按冻结口径清算")
        snapshot = {
            "version": next_version,
            "effective_from": _iso(effective_from),
            "registered_at": _iso(now),
            "rules": rules,
        }
        return {
            "event_type": "PARTNERSHIP_VERSION_REGISTERED",
            "payload": {"parties": list(final_parties), **snapshot},
        }

    def apply(self, event: dict) -> None:
        if event["event_type"] != "PARTNERSHIP_VERSION_REGISTERED":
            return
        payload = event["payload"]
        self.version = event["version"]
        if not self.parties:
            self.parties = list(payload["parties"])
        self.versions.append(
            {"version": payload["version"],
             "effective_from": payload["effective_from"],
             "registered_at": payload["registered_at"],
             "rules": payload["rules"]}
        )

    @staticmethod
    def _validate_rules(rules: dict) -> None:
        for key in ("currency", "fx_basis", "review_deadline_days", "milestones",
                    "territory_weights_bp", "codev_share_bp", "disclosure",
                    "payer_party", "payee_party"):
            if key not in rules:
                raise DomainError(f"规则缺少字段: {key}")
        if int(rules["review_deadline_days"]) < 1:
            raise DomainError("审阅期限至少为 1 天")
        weights = rules["territory_weights_bp"]
        if not weights or sum(int(v) for v in weights.values()) != 10_000:
            raise DomainError("地区权益权重合计必须为 10000 基点")
        if not 0 <= int(rules["codev_share_bp"]) <= 10_000:
            raise DomainError("共同开发分成必须在 0..10000 基点之间")
        disclosure = rules["disclosure"]
        if disclosure.get("external") not in ("prohibited", "jsc_approval", "allowed"):
            raise DomainError("对外披露口径必须是 prohibited/jsc_approval/allowed")
        for code, milestone in rules["milestones"].items():
            for key in ("name", "project_codes", "territories",
                        "required_evidence", "offices", "amount_minor"):
                if key not in milestone:
                    raise DomainError(f"里程碑 {code} 规则缺少字段: {key}")
            unknown = set(milestone["offices"]) - set(OFFICES)
            if unknown:
                raise DomainError(f"里程碑 {code} 存在未知签署职能: {sorted(unknown)}")
            if not milestone["required_evidence"]:
                raise DomainError(f"里程碑 {code} 至少需要一种证据")
            if int(milestone["amount_minor"]) < 0:
                raise DomainError(f"里程碑 {code} 金额不能为负")


# ---------------------------------------------------------------- 里程碑候选

@dataclass
class Claim:
    claim_id: str
    version: int = 0
    partnership_id: Optional[str] = None
    milestone_code: Optional[str] = None
    project_code: Optional[str] = None
    submitted_by: Optional[str] = None
    submit_event_id: Optional[str] = None
    round: int = 0
    rules_version_by_round: dict[int, int] = field(default_factory=dict)
    territories_by_round: dict[int, list[str]] = field(default_factory=dict)
    codev_by_round: dict[int, Optional[int]] = field(default_factory=dict)
    # 本轮待锁定证据清单: evidence_id -> {evidence_type, submitted_by}
    pending: dict[str, dict] = field(default_factory=dict)
    # 证据锁定事实（追加），同一证据后一轮更正产生新记录
    locks: list[dict] = field(default_factory=list)
    conflict: Optional[dict] = None
    signatures_by_round: dict[int, dict[str, dict]] = field(default_factory=dict)
    objections_by_round: dict[int, list[dict]] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    decision_event_ids: dict[int, str] = field(default_factory=dict)
    reopened_at_by_round: dict[int, str] = field(default_factory=dict)

    # ---- 状态派生 ----

    @property
    def is_decided(self) -> bool:
        return len(self.decisions) == self.round and self.round > 0

    @property
    def latest_decision(self) -> Optional[dict]:
        return self.decisions[-1] if self.decisions else None

    def milestone(self, partnership: Partnership, round_no: Optional[int] = None) -> dict:
        snapshot = partnership.rules_at(self.rules_version_by_round[round_no or self.round])
        return snapshot["rules"]["milestones"][self.milestone_code]

    def effective_evidence(self, round_no: int) -> dict[str, dict]:
        """截至某轮次有效的证据：每个编号取最近一次锁定（含沿用与更正）。"""
        result: dict[str, dict] = {}
        for lock in self.locks:
            if lock["round"] <= round_no:
                result[lock["evidence_id"]] = lock
        return result

    def evidence_complete(self, partnership: Partnership, round_no: int) -> bool:
        required = set(self.milestone(partnership, round_no)["required_evidence"])
        present = {item["evidence_type"] for item in self.effective_evidence(round_no).values()}
        return required <= present

    def ready_at(self, round_no: int) -> datetime:
        """本轮证据齐备时刻：新锁时刻的最大值；无新证据的轮次取重开时刻。"""
        candidates = [
            _parse(lock["locked_at"]) for lock in self.locks if lock["round"] == round_no
        ]
        if round_no > 1:
            candidates.append(_parse(self.reopened_at_by_round[round_no]))
        return max(candidates)

    def deadline_at(self, partnership: Partnership, round_no: int) -> datetime:
        snapshot = partnership.rules_at(self.rules_version_by_round[round_no])
        days = int(snapshot["rules"]["review_deadline_days"])
        return self.ready_at(round_no) + timedelta(days=days)

    def is_past_deadline(self, partnership: Partnership, now: datetime) -> bool:
        return self.deadline_at(partnership, self.round) < now

    def payable_for(self, partnership: Partnership, decision: dict) -> Money:
        return Money(decision["payable_amount_minor"],
                     decision["currency"], decision["fx_basis"])

    # ---- 命令 ----

    def submit(
        self,
        *,
        partnership: Partnership,
        milestone_code: str,
        project_code: str,
        territories: list[str],
        submitter: str,
        manifests: list[dict],
        now: datetime,
    ) -> dict:
        if self.version:
            raise DomainError(f"候选 {self.claim_id} 已存在")
        rules = partnership.current["rules"]
        milestone = rules["milestones"].get(milestone_code)
        if milestone is None:
            raise DomainError(f"冻结规则中不存在里程碑 {milestone_code}")
        if project_code not in milestone["project_codes"]:
            raise DomainError(f"项目 {project_code} 不在里程碑 {milestone_code} 范围")
        if not territories:
            raise DomainError("至少登记一个主张地区")
        extra = set(territories) - set(milestone["territories"])
        if extra:
            raise DomainError(f"地区超出合同范围: {sorted(extra)}")
        if not manifests:
            raise DomainError("临床结果提交必须附带证据包清单")
        ids = [m["evidence_id"] for m in manifests]
        if len(ids) != len(set(ids)):
            raise DomainError("同一候选内证据编号重复")
        return {
            "event_type": "CLAIM_SUBMITTED",
            "payload": {
                "partnership_id": partnership.partnership_id,
                "milestone_code": milestone_code,
                "project_code": project_code,
                "territories": list(territories),
                "submitted_by": submitter,
                "submitted_at": _iso(now),
                "rules_version": partnership.version,
                "packages": [
                    {
                        "evidence_id": m["evidence_id"],
                        "evidence_type": m["evidence_type"],
                        "submitted_by": m.get("submitted_by", submitter),
                    }
                    for m in manifests
                ],
            },
        }

    def lock_evidence(
        self,
        *,
        bundles: list[dict],
        registry: dict[str, list[dict]],
        partnership: Partnership,
        now: datetime,
    ) -> list[dict]:
        """锁定证据内容，每个包产生一个事件。

        - 同编号同指纹：幂等沿用，事件带 reused_from；
        - 同编号不同指纹：EVIDENCE_CONFLICT_FLAGGED，候选进入争议；
        - 本候选后续裁决轮次中声明为更正的同编号新指纹：correction_of。
        """
        if not self.round or self.is_decided:
            raise DomainError("候选状态不允许锁定证据")
        events: list[dict] = []
        for bundle in bundles:
            evidence_id = bundle["evidence_id"]
            if evidence_id not in self.pending:
                raise DomainError(f"证据 {evidence_id} 不在本轮待锁定清单")
            resolution = bundle.get("resolution")
            digest = fingerprint(bundle["content"])
            if self.conflict and self.conflict["evidence_id"] != evidence_id and not resolution:
                raise DomainError("存在未解决的证据争议，其余证据暂停锁定")
            already = next(
                (lk for lk in self.locks
                 if lk["round"] == self.round and lk["evidence_id"] == evidence_id),
                None,
            )
            if already is not None:
                if already["fingerprint"] != digest:
                    raise DomainError(f"证据 {evidence_id} 本轮已锁定不同内容，进入争议需新裁决")
                continue  # 命令重试：事实已存在
            prior = registry.get(evidence_id, [])
            same = next((lk for lk in prior if lk["fingerprint"] == digest), None)
            different = next((lk for lk in prior if lk["fingerprint"] != digest), None)
            is_declared_correction = (
                different is not None
                and different["claim_id"] == self.claim_id
                and different["round"] < self.round
            )
            if (
                different is not None
                and not is_declared_correction
                and resolution is None
            ):
                events.append({
                    "event_type": "EVIDENCE_CONFLICT_FLAGGED",
                    "payload": {
                        "evidence_id": evidence_id,
                        "submitted_fingerprint": digest,
                        "existing_fingerprint": different["fingerprint"],
                        "existing_claim_id": different["claim_id"],
                        "existing_round": different["round"],
                        "flagged_at": _iso(now),
                    },
                })
                continue
            if resolution is not None and not (
                self.conflict and self.conflict["evidence_id"] == evidence_id
            ):
                raise DomainError("只有处于争议中的证据才能附委员会裁决")
            lock = {
                "evidence_id": evidence_id,
                "evidence_type": self.pending[evidence_id]["evidence_type"],
                "fingerprint": digest,
                "submitted_by": self.pending[evidence_id]["submitted_by"],
                "locked_at": _iso(now),
                "round": self.round,
            }
            if same is not None:
                lock["reused_from"] = {
                    "claim_id": same["claim_id"],
                    "round": same["round"],
                    "event_id": same.get("event_id"),
                    "fingerprint": digest,
                }
            if is_declared_correction:
                lock["correction_of"] = {
                    "fingerprint": different["fingerprint"],
                    "round": different["round"],
                }
            if resolution is not None:
                lock["conflict_resolution"] = {
                    "resolved_by": resolution["resolved_by"],
                    "note": resolution["note"],
                    "resolved_at": _iso(now),
                    "existing_claim_id": different["claim_id"] if different else None,
                }
            events.append({"event_type": "EVIDENCE_LOCKED", "payload": lock})
        return events

    def sign(
        self,
        *,
        office: str,
        signer: str,
        partnership: Partnership,
        now: datetime,
    ) -> list[dict]:
        self._assert_signable(partnership, office, signer, now)
        deadline = self.deadline_at(partnership, self.round)
        signature = {
            "round": self.round,
            "office": office,
            "signed_by": signer,
            "signed_at": _iso(now),
            "deadline_at": _iso(deadline),
            "rules_version": self.rules_version_by_round[self.round],
            "evidence_fingerprints": sorted(
                item["fingerprint"] for item in self.effective_evidence(self.round).values()
            ),
        }
        events = [{"event_type": "REVIEW_SIGNED", "payload": signature}]
        decision = self._maybe_decide(
            partnership, now, extra_office=office, extra_signature=signature,
        )
        if decision is not None:
            events.append(decision)
        return events

    def object(
        self,
        *,
        office: str,
        signer: str,
        reason: str,
        partnership: Partnership,
        now: datetime,
    ) -> list[dict]:
        self._assert_signable(partnership, office, signer, now, veto=True)
        if not reason.strip():
            raise DomainError("异议必须说明理由")
        record = {
            "round": self.round,
            "office": office,
            "objected_by": signer,
            "objected_at": _iso(now),
            "reason": reason,
            "rules_version": self.rules_version_by_round[self.round],
        }
        events = [{"event_type": "REVIEW_OBJECTED", "payload": record}]
        events.append(self._decision(
            partnership, now, outcome="not_achieved",
            reason_code="objection", reason=f"{office}: {reason}", by_office=office,
            extra_objections=[record],
        ))
        return events

    def timeout_decision(self, *, partnership: Partnership, now: datetime) -> Optional[dict]:
        """证据齐备但期限届满仍未签齐：按未通过处理。"""
        if not self.round or self.conflict or self.is_decided:
            return None
        if not self.evidence_complete(partnership, self.round) or self.pending:
            return None
        offices = self.milestone(partnership)["offices"]
        signed = set(self.signatures_by_round.get(self.round, {}))
        if set(offices) <= signed:
            return None
        if not self.is_past_deadline(partnership, now):
            return None
        return self._decision(
            partnership, now, outcome="not_achieved",
            reason_code="review_timeout",
            reason=f"审阅期限届满仍未签署: {sorted(set(offices) - signed)}",
            by_office=None,
        )

    def request_adjudication(
        self,
        *,
        adjudication_id: str,
        reason: str,
        partnership: Partnership,
        now: datetime,
        correction_manifests: Optional[list[dict]] = None,
        territories: Optional[list[str]] = None,
        new_rules_version: Optional[int] = None,
        codev_share_bp: Optional[int] = None,
    ) -> dict:
        if reason not in REASONS:
            raise DomainError(f"未知裁决原因: {reason}")
        if not self.is_decided:
            raise DomainError("只有已裁决候选才能创建新裁决")
        if self.conflict:
            raise DomainError("争议未解决的候选不能发起新裁决")
        new_round = self.round + 1
        rules_version = new_rules_version or self.rules_version_by_round[self.round]
        if not partnership.exists(rules_version):
            raise DomainError(f"新规则版本 {rules_version} 尚未登记")
        milestone = partnership.rules_at(rules_version)["rules"]["milestones"].get(self.milestone_code)
        if milestone is None:
            raise DomainError(f"里程碑 {self.milestone_code} 在新版本中不存在，需另行登记合作变更")
        next_territories = (
            list(territories) if territories is not None
            else list(self.territories_by_round[self.round])
        )
        extra = set(next_territories) - set(milestone["territories"])
        if extra:
            raise DomainError(f"地区超出新版本合同范围: {sorted(extra)}")
        if reason == "data_correction" and not correction_manifests:
            raise DomainError("数据纠正必须提供更正证据清单")
        if reason == "territory_exit" and territories is None:
            raise DomainError("区域退出必须声明裁决后地区范围")
        if reason == "co_development_change" and codev_share_bp is None:
            raise DomainError("共同开发选择权变化必须声明新的分成基点")
        if codev_share_bp is not None and not 0 <= codev_share_bp <= 10_000:
            raise DomainError("共同开发分成必须在 0..10000 基点之间")
        ids = [m["evidence_id"] for m in correction_manifests or []]
        if len(ids) != len(set(ids)):
            raise DomainError("更正证据编号重复")
        return {
            "event_type": "CLAIM_REOPENED",
            "payload": {
                "adjudication_id": adjudication_id,
                "reason": reason,
                "prior_round": self.round,
                "new_round": new_round,
                "prior_rules_version": self.rules_version_by_round[self.round],
                "new_rules_version": rules_version,
                "territories": next_territories,
                "codev_share_bp": codev_share_bp,
                "corrections": [
                    {
                        "evidence_id": m["evidence_id"],
                        "evidence_type": m["evidence_type"],
                        "submitted_by": m.get("submitted_by", self.submitted_by),
                    }
                    for m in (correction_manifests or [])
                ],
                "reopened_at": _iso(now),
            },
        }

    # ---- 折叠 ----

    def apply(self, event: dict) -> None:
        etype = event["event_type"]
        payload = event["payload"]
        if etype == "CLAIM_SUBMITTED":
            self.partnership_id = payload["partnership_id"]
            self.milestone_code = payload["milestone_code"]
            self.project_code = payload["project_code"]
            self.submitted_by = payload["submitted_by"]
            self.submit_event_id = event["event_id"]
            self.round = 1
            self.rules_version_by_round[1] = payload["rules_version"]
            self.territories_by_round[1] = list(payload["territories"])
            self.codev_by_round[1] = None
            self.signatures_by_round[1] = {}
            self.objections_by_round[1] = []
            self.pending = {
                p["evidence_id"]: {
                    "evidence_type": p["evidence_type"],
                    "submitted_by": p["submitted_by"],
                }
                for p in payload["packages"]
            }
        elif etype == "EVIDENCE_LOCKED":
            self.locks.append({**payload, "event_id": event["event_id"]})
            self.pending.pop(payload["evidence_id"], None)
            if "conflict_resolution" in payload and (
                self.conflict
                and self.conflict["evidence_id"] == payload["evidence_id"]
            ):
                self.conflict = None
        elif etype == "EVIDENCE_CONFLICT_FLAGGED":
            self.conflict = {
                "round": self.round,
                "evidence_id": payload["evidence_id"],
                "submitted_fingerprint": payload["submitted_fingerprint"],
                "existing_fingerprint": payload["existing_fingerprint"],
                "existing_claim_id": payload["existing_claim_id"],
                "flagged_event_id": event["event_id"],
            }
        elif etype == "REVIEW_SIGNED":
            self.signatures_by_round.setdefault(payload["round"], {})[payload["office"]] = payload
        elif etype == "REVIEW_OBJECTED":
            self.objections_by_round.setdefault(payload["round"], []).append(payload)
        elif etype == "CLAIM_DECIDED":
            self.decisions.append(payload)
            self.decision_event_ids[payload["round"]] = event["event_id"]
        elif etype == "CLAIM_REOPENED":
            new_round = payload["new_round"]
            self.round = new_round
            self.rules_version_by_round[new_round] = payload["new_rules_version"]
            self.territories_by_round[new_round] = list(payload["territories"])
            self.codev_by_round[new_round] = payload.get("codev_share_bp")
            self.signatures_by_round[new_round] = {}
            self.objections_by_round[new_round] = []
            self.reopened_at_by_round[new_round] = payload["reopened_at"]
            self.conflict = None
            self.pending = {
                p["evidence_id"]: {
                    "evidence_type": p["evidence_type"],
                    "submitted_by": p["submitted_by"],
                }
                for p in payload["corrections"]
            }

    # ---- 内部 ----

    def _assert_signable(
        self, partnership: Partnership, office: str, signer: str,
        now: datetime, *, veto: bool = False,
    ) -> None:
        if not self.round:
            raise DomainError("候选尚未提交")
        if self.conflict:
            raise DomainError("证据争议未解决前不得签署或作出否决")
        if self.pending or not self.evidence_complete(partnership, self.round):
            raise DomainError("证据尚未锁定完整")
        if self.is_decided:
            raise DomainError("本轮已有裁决，结论不可改写；更正须发起新裁决")
        offices = self.milestone(partnership)["offices"]
        if office not in offices:
            raise DomainError(f"{office} 不在本里程碑合同签署范围")
        existing = self.signatures_by_round.get(self.round, {})
        if not veto and office in existing:
            raise DomainError(f"{office} 已签署，签署不可撤回或改写")
        if veto and any(
            item["office"] == office
            for item in self.objections_by_round.get(self.round, [])
        ):
            raise DomainError(f"{office} 已提出异议，不可重复登记")
        if veto and office in existing:
            raise DomainError(f"{office} 已签署，签署不可撤回或改写")
        if signer == self.submitted_by:
            raise DomainError("提交者不能批准自己的材料")
        current_submitters = {
            item["submitted_by"]
            for item in self.effective_evidence(self.round).values()
            if item["round"] == self.round or item.get("reused_from")
        }
        if signer in current_submitters:
            raise DomainError("证据提交者不能批准自己参与提交的材料")
        if self.deadline_at(partnership, self.round) < now:
            raise DomainError(f"{office} 审阅期限已过，应按未通过处理")

    def _factor_bp(self, partnership: Partnership) -> int:
        snapshot = partnership.rules_at(self.rules_version_by_round[self.round])
        rules = snapshot["rules"]
        weights = rules["territory_weights_bp"]
        retained = sum(int(weights[t]) for t in self.territories_by_round[self.round])
        share = self.codev_by_round[self.round]
        share = int(rules["codev_share_bp"] if share is None else share)
        return retained * share // 10_000

    def _maybe_decide(
        self, partnership: Partnership, now: datetime, *,
        extra_office: str, extra_signature: dict,
    ) -> Optional[dict]:
        offices = self.milestone(partnership)["offices"]
        signed = set(self.signatures_by_round.get(self.round, {})) | {extra_office}
        if all(o in signed for o in offices):
            return self._decision(
                partnership, now, outcome="achieved",
                reason_code="all_offices_signed",
                reason="科学、合规、财务按合同范围独立签署完成",
                by_office=None, factor_bp=self._factor_bp(partnership),
                extra_signatures=[extra_signature],
            )
        return None

    def _decision(
        self, partnership: Partnership, now: datetime, *,
        outcome: str, reason_code: str, reason: str,
        by_office: Optional[str], factor_bp: Optional[int] = None,
        extra_objections: Optional[list[dict]] = None,
        extra_signatures: Optional[list[dict]] = None,
    ) -> dict:
        snapshot = partnership.rules_at(self.rules_version_by_round[self.round])
        rules = snapshot["rules"]
        gross = int(rules["milestones"][self.milestone_code]["amount_minor"])
        factor = self._factor_bp(partnership) if factor_bp is None else factor_bp
        payable = 0 if outcome == "not_achieved" else gross * factor // 10_000
        signatures = dict(self.signatures_by_round.get(self.round, {}))
        for extra in extra_signatures or []:
            signatures.setdefault(extra["office"], extra)
        objections = list(self.objections_by_round.get(self.round, []))
        for extra in extra_objections or []:
            objections.append(extra)
        return {
            "event_type": "CLAIM_DECIDED",
            "payload": {
                "round": self.round,
                "outcome": outcome,
                "reason_code": reason_code,
                "reason": reason,
                "by_office": by_office,
                "decided_at": _iso(now),
                "project_code": self.project_code,
                "milestone_code": self.milestone_code,
                "territories": list(self.territories_by_round[self.round]),
                "rules_version": snapshot["version"],
                "rules_effective_from": snapshot["effective_from"],
                "currency": rules["currency"],
                "fx_basis": rules["fx_basis"],
                "payer_party": rules["payer_party"],
                "payee_party": rules["payee_party"],
                "gross_amount_minor": gross,
                "codev_share_bp": self.codev_by_round[self.round],
                "factor_bp": factor,
                "payable_amount_minor": payable,
                "disclosure": rules["disclosure"],
                "signatures": [
                    {"office": o, "signed_by": s["signed_by"],
                     "signed_at": s["signed_at"], "deadline_at": s["deadline_at"]}
                    for o, s in sorted(signatures.items())
                ],
                "objections": objections,
                "evidence_fingerprints": sorted(
                    item["fingerprint"]
                    for item in self.effective_evidence(self.round).values()
                ),
            },
        }


# ---------------------------------------------------------------- 裁决

@dataclass
class Adjudication:
    adjudication_id: str
    version: int = 0
    claim_id: Optional[str] = None
    partnership_id: Optional[str] = None
    reason: Optional[str] = None
    requested_by: Optional[str] = None
    rounds: Optional[tuple[int, int]] = None
    settlement: Optional[dict] = None

    @property
    def is_settled(self) -> bool:
        return self.settlement is not None

    def create(
        self, *, claim: Claim, reason: str, requested_by: str, now: datetime,
    ) -> dict:
        if self.version:
            raise DomainError(f"裁决 {self.adjudication_id} 已存在")
        prior_round = claim.round
        return {
            "event_type": "ADJUDICATION_CREATED",
            "payload": {
                "claim_id": claim.claim_id,
                "partnership_id": claim.partnership_id,
                "reason": reason,
                "requested_by": requested_by,
                "prior_round": prior_round,
                "new_round": prior_round + 1,
                "prior_decision_event_id": claim.decision_event_ids.get(prior_round),
                "created_at": _iso(now),
            },
        }

    def settle(
        self,
        *,
        claim: Claim,
        prior_paid: Money,
        adjustment_payment_id: Optional[str],
        now: datetime,
    ) -> dict:
        if self.settlement is not None:
            raise DomainError("裁决差额已清算，不可重复处理")
        decision = claim.latest_decision
        if decision is None or self.rounds is None or decision["round"] != self.rounds[1]:
            raise DomainError("新裁决轮次尚未裁决，不能计算差额")
        new_payable = Money(
            decision["payable_amount_minor"],
            decision["currency"], decision["fx_basis"],
        )
        delta = new_payable.minus(prior_paid)
        if delta.amount_minor > 0:
            direction = "top_up"
        elif delta.amount_minor < 0:
            direction = "clawback"
        else:
            direction = "none"
        return {
            "event_type": "ADJUSTMENT_SETTLED",
            "payload": {
                "adjudication_id": self.adjudication_id,
                "claim_id": claim.claim_id,
                "round": decision["round"],
                "reason": self.reason,
                "direction": direction,
                "prior_paid_amount_minor": prior_paid.amount_minor,
                "new_payable_amount_minor": new_payable.amount_minor,
                "delta_amount_minor": delta.amount_minor,
                "currency": delta.currency,
                "fx_basis": delta.fx_basis,
                "adjustment_payment_id": adjustment_payment_id,
                "settled_at": _iso(now),
            },
        }

    def apply(self, event: dict) -> None:
        payload = event["payload"]
        if event["event_type"] == "ADJUDICATION_CREATED":
            self.claim_id = payload["claim_id"]
            self.partnership_id = payload["partnership_id"]
            self.reason = payload["reason"]
            self.requested_by = payload["requested_by"]
            self.rounds = (payload["prior_round"], payload["new_round"])
        elif event["event_type"] == "ADJUSTMENT_SETTLED":
            self.settlement = payload


# ---------------------------------------------------------------- 付款指令

@dataclass
class Payment:
    payment_id: str
    record: Optional[dict] = None

    def order(
        self,
        *,
        claim: Claim,
        decision: dict,
        amount: Money,
        kind: str,
        adjudication_id: Optional[str],
        now: datetime,
    ) -> dict:
        if self.record is not None:
            raise DomainError(f"付款指令 {self.payment_id} 已发出，事实不可改写")
        if amount.amount_minor <= 0:
            raise DomainError("付款指令金额必须为正；负差额应使用裁决追回")
        return {
            "event_type": "PAYMENT_ORDERED",
            "payload": {
                "payment_kind": kind,
                "claim_id": claim.claim_id,
                "partnership_id": claim.partnership_id,
                "round": decision["round"],
                "decision_rules_version": decision["rules_version"],
                "decision_event_id": claim.decision_event_ids.get(decision["round"]),
                "adjudication_id": adjudication_id,
                "amount_minor": amount.amount_minor,
                "currency": amount.currency,
                "fx_basis": amount.fx_basis,
                "project_code": decision["project_code"],
                "milestone_code": decision["milestone_code"],
                "territories": list(decision["territories"]),
                "payer_party": decision["payer_party"],
                "payee_party": decision["payee_party"],
                "ordered_at": _iso(now),
            },
        }

    def apply(self, event: dict) -> None:
        if event["event_type"] == "PAYMENT_ORDERED":
            self.record = {**event["payload"], "event_id": event["event_id"]}
