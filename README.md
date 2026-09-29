# 创新药合作里程碑托管

本项目实现创新药合作（单个分子授权、技术平台、成组管线、共同开发）的服务端里程碑托管领域核心：登记合作版本与合同口径，管理临床结果候选达成、独立签署、付款指令，以及数据纠正、区域退出与共同开发选择权变化引发的新裁决和差额清算。

只使用 Python 标准库，可在单个 Linux 应用容器中直接运行。

## 领域规则

- **合作版本冻结**：`Partnership` 登记双方主体、项目与地区范围、所需证据、签署职能、审阅期限（天）、对外披露口径、里程碑金额、币种与汇率口径（fx_basis）、地区权益权重、共同开发分成。币种与口径在版本之间不可变更；候选每一轮使用的规则版本号随裁决永久留痕。
- **候选达成 ≠ 付款**：`CLAIM_SUBMITTED` 只产生候选；证据锁定完整后，科学、合规、财务按里程碑合同范围**独立签署**，签齐才裁决 `achieved`；任一职能提出异议或审阅期限届满未签齐，裁决 `not_achieved`。
- **利益回避**：候选提交者不能批准自己的材料；本轮证据提交者同样不能签署；争议材料的提交者不能裁决自己的证据冲突。
- **证据幂等与争议**：证据内容以规范化 JSON 的 SHA-256 为指纹。同编号同指纹幂等沿用（`reused_from`）；同编号不同指纹产生 `EVIDENCE_CONFLICT_FLAGGED` 并冻结签署，联合管理委员会采纳后以 `conflict_resolution` 锁定。本候选后续轮次的同编号新内容按更正留痕（`correction_of`）。
- **期限与时区**：截止时刻 = 本轮证据齐备时刻 + 冻结规则的期限天数，存为带时区绝对时刻；跨时区显示是同一瞬间，跨计费周期登记新规则版本不影响在途候选。
- **付款事实不可改写**：`PAYMENT_ORDERED` 一经发出不可修改。数据纠正、区域退出、共同开发选择权变化创建 `Adjudication` 与新裁决轮次；清算差额时，正差额发新的补付指令（`adjustment_top_up`），负差额登记应追回（`clawback`），零差额留痕（`none`）。
- **反向核对**：可从任一付款指令或未通过里程碑回溯权利版本、地区范围、证据指纹、各职能签署/异议、截止时刻、全部付款与纠正历史。
- **职责可见性**：普通成员只能看到职责项目范围内的候选；金额与币种口径仅财务（或具 `payment.read`）可见，其他成员看到掩码；合作版本登记、争议裁决等仅授权双方代表。

## 代码布局

| 文件 | 职责 |
| --- | --- |
| `contracts/domain.json` | 聚合类型、事件名称与枚举 |
| `src/envelope.py` | 共用事件信封字段与时间格式校验 |
| `src/events.py` | 只追加事件存储：聚合版本递增、event_id 唯一、幂等键 |
| `src/canonical.py` | 证据规范化 JSON 与指纹 |
| `src/money.py` | 整数最小单位金额、币种与 fx_basis 口径、差额 |
| `src/domain.py` | 聚合：`Partnership` / `Claim` / `Adjudication` / `Payment` |
| `src/access.py` | 操作者、职能、项目范围与敏感字段脱敏投影 |
| `src/service.py` | 应用服务：命令→事件、巡检超时、付款、差额清算、反向核对 |
| `data/sample.json` | 基础信封联调样例 |

## 事件流

```
PARTNERSHIP_VERSION_REGISTERED
CLAIM_SUBMITTED
EVIDENCE_LOCKED                 # 含 reused_from / correction_of / conflict_resolution
EVIDENCE_CONFLICT_FLAGGED
REVIEW_SIGNED / REVIEW_OBJECTED
CLAIM_DECIDED                   # achieved / not_achieved，含冻结规则版本与金额测算
PAYMENT_ORDERED                 # milestone / adjustment_top_up，事实不可改写
ADJUDICATION_CREATED
CLAIM_REOPENED                  # 新裁决轮次
ADJUSTMENT_SETTLED              # top_up / clawback / none
```

## 测试与构建

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

测试覆盖主链路金额测算、提交者回避、证据幂等与同标识争议、异议与期限超时（跨时区/计费周期的冻结版本）、区域退出追回、共同开发选择补付、数据纠正链与零差额、付款不可改写、双向反向核对，以及普通成员脱敏。

## 持久化边界

当前 `EventStore` 为内存实现，接口只暴露追加与按流读取；正式服务可替换为数据库支持的只追加日志，领域与服务层无需改动。
