# 创新药合作里程碑托管

面向联合管理委员会的服务端里程碑托管领域内核。它登记合作版本、项目与地区范围、
决策权、所需证据、审阅期限、异议、对外披露限制、里程碑金额与币种口径；临床结果提交后
只产生**候选达成**，科学、合规、财务按合同范围独立签署后才会产生付款指令。

当前仓库实现的是事件溯源的领域内核与读模型，只依赖 Python 标准库，便于后续服务
（持久化、对外 API、工作流界面）在相同语义上演进。

## 资料结构

- `contracts/domain.json`：聚合类型、事件名称、签署范围、裁决原因与基础信封，并声明
  规则冻结点（`CLAIM_SUBMITTED`）与只追加原则。
- `src/envelope.py`：共用事件信封与时间格式校验。
- `src/store.py`：只追加事件存储；保证信封合法、聚合版本严格递增、`event_id` 全局唯一。
- `src/escrow.py`：托管领域写模型（合作版本、候选主张、证据、争议、签署、裁决、付款）。
- `src/projection.py`：反向核对报告（`ReconciliationReport`）与按职责脱敏的成员视图（`MemberView`）。
- `src/errors.py`：`ValidationError / ConflictError / AuthorizationError / WorkflowError`。
- `data/sample.json`：可直接校验基础信封的中文联调样例。
- `tests/test_escrow.py`：32 条领域不变量测试。

## 关键不变量

1. **候选 ≠ 达成**：`CLAIM_SUBMITTED` 只是候选；证据锁定后进入独立审阅，
   合同要求的签署范围（科学/合规/财务）全部通过才发出 `PAYMENT_ORDERED`。
   任一方 `REJECT` 或超过冻结审阅期限，主张记为未通过，不产生付款。
2. **回避**：提交者不得签署自己的材料；同一成员不得跨范围重复签署；
   签署人必须在冻结版本 `decision_rights` 的该范围名单内。
3. **规则冻结**：提交时固化合作版本、地区金额、币种口径、所需证据与各范围审阅期限。
   之后即使登记新版本，在途主张的截止时间、计费周期归属与金额口径都取冻结版本，
   因此跨时区、跨计费周期结果不变。
4. **证据幂等与争议**：同一证据包标识 + 同一内容哈希直接去重（返回原主张）；
   同标识不同内容开 `DISPUTE_OPENED`，主张冻结，争议解决前不能锁定证据或签署。
5. **付款不可变**：付款指令是只追加事实。数据纠正、区域退出、共同开发选择改变只能
   开新裁决（`ADJUDICATION_OPENED/SETTLED`），按冻结口径重算应付并给出差额：
   `ADDITIONAL` 补付、`RECOVERY` 追回（追回金额按正数记账，净额计算时取负）、
   差额为零则不产生付款。裁决结清须经双方各自独立授权成员确认，且排除提交者。
6. **反向核对**：从任一付款或未通过里程碑可反向核对权利快照、证据锁定事实、
   各范围签署（含拒绝理由）、争议与裁决差额历史，并附按追加顺序计算的完整性链摘要。
7. **按职责脱敏**：普通成员只看到自己提交或自己有签署权的主张；科学可见临床证据但
   不见金额，财务可见金额口径但不见临床证据明细，合规另可见冻结的对外披露限制与地区权利。

## 最小流程

```python
from datetime import datetime, timedelta, timezone
from src.escrow import MilestoneEscrow
from src.store import EventStore
from src.projection import ReconciliationReport, MemberView

escrow = MilestoneEscrow(EventStore())
escrow.register_partnership_version(
    event_id="ev-v1", partnership_version_id="partnership_version-v1",
    occurred_at=datetime(2026, 2, 22, tzinfo=timezone.utc), spec=spec,
)
escrow.submit_claim(..., evidence_package_id="pkg-1", content_hash="hash-aaa")
escrow.lock_evidence(event_id="ev-lock", claim_id="claim-1", occurred_at=t0)
escrow.record_signature(..., scope="scientific", signer="u-sci")
escrow.record_signature(..., scope="compliance", signer="u-comp")
payment_id = escrow.record_signature(..., scope="finance", signer="u-fin")

# 区域退出 → 新裁决 + 追回，原付款事实不变
escrow.open_adjudication(..., reason="REGIONAL_EXIT", achieved_regions=["CN"])
escrow.settle_adjudication(..., approved_by=["u-sci", "u-comp"])

ReconciliationReport(escrow).from_payment(payment_id)   # 付款反查全链
MemberView(escrow).claim_view("u-fin", "claim-1")       # 按职责脱敏
```

完整可运行示例见 `tests/test_escrow.py`（`spec_v1()` 给出合作版本条款结构）。

## 测试与构建

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

两条命令均只使用 Python 标准库，可在单个 Linux 应用容器中直接执行。

## 明确不在本内核范围内

持久化与事务隔离（`EventStore` 为内存实现，接口已预留期望版本的乐观并发）、
对外 HTTP/消息接口、证据文件本体存储（内核只登记包标识与内容哈希）、
付款的实际清结算执行（内核只产生不可变付款指令）。
