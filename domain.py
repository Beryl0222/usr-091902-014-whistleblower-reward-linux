"""市场举报奖励领域核心。

围绕“身份隔离”组织：举报中心把真实身份封存在身份库，对承办、审核、
财政各环节只发放别名；奖励金额在案件进入可奖励阶段时按违法类别、举报
等级、罚没结果与当时生效规则自动生成，超过权限自动转财政会签；撤回、
重复举报、复议与判决变化一律以追加决定调整，旧结论原样保留。
"""

import copy
import hashlib
import json
import os
from datetime import date

RULES_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reward_rules.json")

# 真实身份字段：只能存在于身份库，任何办案视图、对外材料都不得出现。
IDENTITY_FIELDS = ("姓名", "证件号码", "联系方式", "银行账户", "住址", "工作单位")

# 案件参考状态（与 fixtures/domain.json 保持一致）。
CASE_OPENED = "已登记"
CASE_INVESTIGATING = "调查中"
CASE_PENDING_RECOGNITION = "待认定"
CASE_REWARDABLE = "可奖励"
CASE_ADJUSTED = "已调整"

# 奖励决定状态机：待审核 →（待财政会签）→ 待支付 → 已支付。
REWARD_PENDING_REVIEW = "待审核"
REWARD_PENDING_FINANCE = "待财政会签"
REWARD_PENDING_PAYMENT = "待支付"
REWARD_PAID = "已支付"
REWARD_TERMINATED = "因撤回终止"
REWARD_SUPERSEDED = "已调整"
REWARD_PENDING_CLAWBACK = "待追缴"
REWARD_CLAWED_BACK = "已追缴"
REWARD_REJECTED = "已驳回"
REWARD_UPHELD = "维持原决定"

CONTRIBUTION_FIRST = "最先有效贡献"
CONTRIBUTION_INDEPENDENT = "独立关键贡献"
CONTRIBUTION_DUPLICATE = "重复举报"
CONTRIBUTION_NONE = "不予认定"
REWARDABLE_ROLES = (CONTRIBUTION_FIRST, CONTRIBUTION_INDEPENDENT)


class DomainError(ValueError):
    """业务规则冲突。"""


def load_rules(path=RULES_PATH):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    versions = sorted(data["规则版本"], key=lambda item: item["生效日"])
    for version in versions:
        if version["失效日"] is not None and version["失效日"] < version["生效日"]:
            raise DomainError(f"规则版本 {version['版本号']} 失效日早于生效日")
    return data


def rule_effective_on(rules, day):
    """返回某日（含）当时生效的规则版本。"""
    for version in rules["规则版本"]:
        end = version["失效日"]
        if version["生效日"] <= day and (end is None or day <= end):
            return version
    raise DomainError(f"{day} 没有生效的奖励规则版本")


def _money(value):
    return round(value + 1e-9, 2)


class IdentityVault:
    """身份封存库：真实身份只进不出，除非登记揭示理由并留下审计。"""

    def __init__(self):
        self._identities = {}          # reporter_id -> 真实身份
        self._alias_to_reporter = {}   # 举报人A -> reporter_id
        self._hash_to_reporter = {}    # 身份指纹 -> reporter_id（同一人稳定别名）
        self._seq = 0
        self.audit_trail = []

    @staticmethod
    def _fingerprint(identity):
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def register(self, identity):
        missing = [field for field in IDENTITY_FIELDS[:2] if not identity.get(field)]
        if missing:
            raise DomainError(f"身份信息缺少必填项：{'、'.join(missing)}")
        fingerprint = self._fingerprint(identity)
        reporter_id = self._hash_to_reporter.get(fingerprint)
        if reporter_id:
            return reporter_id, self._alias_of(reporter_id), False
        self._seq += 1
        reporter_id = f"R{self._seq:03d}"
        alias = f"举报人{chr(ord('A') + self._seq - 1)}"
        self._identities[reporter_id] = dict(identity)
        self._alias_to_reporter[alias] = reporter_id
        self._hash_to_reporter[fingerprint] = reporter_id
        return reporter_id, alias, True

    def _alias_of(self, reporter_id):
        for alias, rid in self._alias_to_reporter.items():
            if rid == reporter_id:
                return alias
        raise DomainError("身份库中不存在该举报人")

    def reveal(self, alias, actor, reason, at=None):
        """因法定需要（如奖励金发放）揭示身份，必须登记理由与审计。"""
        reporter_id = self._alias_to_reporter.get(alias)
        if reporter_id is None:
            raise DomainError(f"未知别名：{alias}")
        if not reason:
            raise DomainError("揭示身份必须说明理由")
        record = {
            "审计编号": f"A{len(self.audit_trail) + 1:04d}",
            "时间": at or date.today().isoformat(),
            "操作人": actor,
            "动作": "揭示举报人身份",
            "对象别名": alias,
            "理由": reason,
        }
        self.audit_trail.append(record)
        # 返回副本，避免调用方修改封存原件。
        return record["审计编号"], dict(self._identities[reporter_id])


def compute_reward(rule, violation_category, penalty_confiscation, grade, role, is_internal):
    """按一条生效规则计算物质奖励；无资格返回 None。

    物质奖励 = 罚没款 × 类别费率 × 等级系数 × 贡献顺序系数 ×（内部举报再上浮）；
    比例额低于该类别最低奖励的按最低奖励保底；无罚没款案件适用定额；
    任何结果不得超过最高限额。金额由系统计算，不接受人工填列。
    """
    if role not in REWARDABLE_ROLES:
        return None
    if violation_category not in rule["违法类别"]:
        raise DomainError(f"规则 {rule['版本号']} 未覆盖违法类别：{violation_category}")
    category_rule = rule["违法类别"][violation_category]
    grade_coef = rule["等级系数"][grade]
    order_coef = rule["贡献顺序系数"][role]
    uplift = rule["内部举报上浮"] if is_internal else 0.0
    multiplier = grade_coef * order_coef * (1 + uplift)

    no_penalty = not penalty_confiscation
    if no_penalty:
        base = category_rule["无罚没款定额"]
        amount = base * multiplier
        mode = "无罚没款定额"
    else:
        proportional = penalty_confiscation * category_rule["罚没款费率"] * multiplier
        amount = max(proportional, category_rule["最低奖励"])
        mode = "罚没款比例"
    capped = min(amount, rule["最高限额"])
    return {
        "适用版本": rule["版本号"],
        "计算方式": mode,
        "违法类别": violation_category,
        "举报等级": grade,
        "贡献顺序": role,
        "内部举报": bool(is_internal),
        "罚没款": penalty_confiscation or 0,
        "系数": {
            "等级系数": grade_coef,
            "顺序系数": order_coef,
            "内部上浮": uplift,
        },
        "计算金额": _money(amount),
        "触发上限": amount > rule["最高限额"],
        "建议金额": _money(capped),
        "需财政会签": _money(capped) >= rule["财政会签阈值"],
    }


class RewardCenter:
    """举报中心：登记线索、认定贡献、生成并流转奖励决定。"""

    def __init__(self, rules=None):
        self.rules = rules or load_rules()
        self.vault = IdentityVault()
        self.reports = {}        # report_id -> 举报记录
        self.cases = {}          # case_id -> 案件记录
        self.rewards = {}        # reward_id -> 奖励决定
        self._key_index = {}     # 违法行为标识 -> [report_id]（按接收时间）
        self._reward_seq = 0

    # ---- 线索接收 ------------------------------------------------------

    def receive_report(self, *, violation_key, violation_category, received_at,
                       identity, is_internal=False, content="", evidence_ids=None):
        """接收线索：身份封存后仅以别名进入办案链路；自动识别重复举报。"""
        reporter_id, alias, _ = self.vault.register(identity)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        duplicate_of = None
        for earlier_id in self._key_index.get(violation_key, []):
            earlier = self.reports[earlier_id]
            if earlier["内容指纹"] == digest and earlier["状态"] != "已撤回":
                duplicate_of = earlier_id
                break
        report_id = f"WB{len(self.reports) + 1:04d}"
        record = {
            "举报编号": report_id,
            "违法行为标识": violation_key,
            "违法类别": violation_category,
            "别名": alias,
            "举报人编号": reporter_id,
            "接收时间": received_at,
            "内部举报": bool(is_internal),
            "证据材料": list(evidence_ids or []),
            "关联案件": [],
            "内容指纹": digest,
            "状态": "重复举报" if duplicate_of else "已登记",
            "重复于": duplicate_of,
            "时间线": [{"时间": received_at, "事项": "中心登记线索（身份已封存）"}],
        }
        self.reports[report_id] = record
        self._key_index.setdefault(violation_key, []).append(report_id)
        return {"举报编号": report_id, "别名": alias, "状态": record["状态"], "重复于": duplicate_of}

    def add_evidence(self, report_id, evidence_id, summary, at):
        report = self._report(report_id)
        report["证据材料"].append(evidence_id)
        report["时间线"].append(
            {"时间": at, "事项": f"补充证据 {evidence_id}", "摘要": summary}
        )
        return {"举报编号": report_id, "证据材料": report["证据材料"]}

    def link_case(self, report_id, case_id, at=None):
        report = self._report(report_id)
        if case_id not in report["关联案件"]:
            report["关联案件"].append(case_id)
            report["时间线"].append({"时间": at or report["接收时间"], "事项": f"关联案件 {case_id}"})
        return {"举报编号": report_id, "关联案件": report["关联案件"]}

    def withdraw_report(self, report_id, actor, at):
        """撤回：支付前终止奖励；支付后生成追缴决定，旧结论保留。"""
        report = self._report(report_id)
        if report["状态"] == "已撤回":
            raise DomainError("该举报已撤回")
        report["状态"] = "已撤回"
        report["时间线"].append({"时间": at, "事项": f"举报人申请撤回（{actor} 办理）"})
        affected = [r for r in self.rewards.values()
                    if r["举报编号"] == report_id and r["案件编号"] in self.cases]
        for reward in affected:
            paid = self._paid_total(reward["奖励编号"])
            if paid > 0:
                self._create_clawback(reward, paid, at, "举报撤回，追回已发奖励")
            elif reward["状态"] in (REWARD_PENDING_REVIEW, REWARD_PENDING_FINANCE,
                                    REWARD_PENDING_PAYMENT):
                reward["状态"] = REWARD_TERMINATED
                reward["流转记录"].append({"时间": at, "事项": REWARD_TERMINATED, "操作人": actor})
        return {"举报编号": report_id, "状态": report["状态"]}

    # ---- 案件与贡献认定 ------------------------------------------------

    def open_case(self, *, case_id, violation_key, violation_category, opened_at, handlers):
        if case_id in self.cases:
            raise DomainError(f"案件已存在：{case_id}")
        if not handlers:
            raise DomainError("案件必须指定承办人员")
        linked = list(self._key_index.get(violation_key, []))
        case = {
            "案件编号": case_id,
            "违法行为标识": violation_key,
            "违法类别": violation_category,
            "立案时间": opened_at,
            "状态": CASE_INVESTIGATING,
            "承办人员": list(handlers),
            "关联举报": linked,
            "贡献认定": {},
            "罚没款": None,
            "无罚没款": None,
            "结案时间": None,
            "时间线": [{"时间": opened_at, "事项": f"立案调查，承办人：{'、'.join(handlers)}"}],
        }
        self.cases[case_id] = case
        for report_id in linked:
            self.link_case(report_id, case_id, opened_at)
        return self.case_brief(case_id)

    def assess_contributions(self, case_id, assignments, actor, at):
        """认定每条线索的贡献顺序与举报等级；最先有效贡献全案唯一。"""
        case = self._case(case_id)
        firsts = sum(1 for role, _grade in assignments.values() if role == CONTRIBUTION_FIRST)
        if firsts > 1:
            raise DomainError("一个案件只能有一个“最先有效贡献”")
        for report_id, (role, grade) in assignments.items():
            report = self._report(report_id)
            if report["违法行为标识"] != case["违法行为标识"]:
                raise DomainError(f"{report_id} 与案件不属于同一违法行为")
            if role not in (CONTRIBUTION_FIRST, CONTRIBUTION_INDEPENDENT,
                            CONTRIBUTION_DUPLICATE, CONTRIBUTION_NONE):
                raise DomainError(f"未知贡献类型：{role}")
            if role in REWARDABLE_ROLES and grade not in self.rules["规则版本"][0]["等级系数"]:
                raise DomainError(f"未知举报等级：{grade}")
            case["贡献认定"][report_id] = {"贡献顺序": role, "举报等级": grade}
        case["状态"] = CASE_PENDING_RECOGNITION
        case["时间线"].append({"时间": at, "事项": f"完成贡献认定（{actor}）"})
        return copy.deepcopy(case["贡献认定"])

    def close_case(self, *, case_id, actor, closed_at, penalty_confiscation=0, no_penalty=False):
        """结案并进入可奖励阶段：按结案当日生效规则自动生成建议金额。"""
        case = self._case(case_id)
        if case["状态"] not in (CASE_INVESTIGATING, CASE_PENDING_RECOGNITION):
            raise DomainError(f"案件状态 {case['状态']} 不可结案")
        if not case["贡献认定"]:
            raise DomainError("结案前必须完成贡献认定（案件应处于待认定环节）")
        if no_penalty:
            penalty_confiscation = 0
        case["罚没款"] = penalty_confiscation
        case["无罚没款"] = bool(no_penalty or penalty_confiscation == 0)
        case["结案时间"] = closed_at
        case["状态"] = CASE_REWARDABLE
        case["时间线"].append(
            {"时间": closed_at,
             "事项": "结案进入可奖励阶段（无罚没款）" if case["无罚没款"] else "结案进入可奖励阶段"}
        )
        rule = rule_effective_on(self.rules, closed_at)
        created = []
        for report_id, recognition in case["贡献认定"].items():
            report = self._report(report_id)
            if report["状态"] == "已撤回":
                continue
            result = compute_reward(
                rule, case["违法类别"], penalty_confiscation,
                recognition["举报等级"], recognition["贡献顺序"], report["内部举报"],
            )
            if result is None:
                continue  # 重复举报、不予认定：无奖励资格
            reward = self._build_reward(case, report, result, "初步物质奖励决定", closed_at)
            created.append(reward)
        return {"案件编号": case_id, "状态": case["状态"], "奖励决定": [r["奖励编号"] for r in created]}

    def _build_reward(self, case, report, result, kind, at, supersedes=None):
        self._reward_seq += 1
        reward_id = f"JL{self._reward_seq:04d}"
        amount = result["建议金额"]
        reward = {
            "奖励编号": reward_id,
            "案件编号": case["案件编号"],
            "举报编号": report["举报编号"],
            "别名": report["别名"],
            "决定类型": kind,
            "替代决定": supersedes,
            "建议金额": amount,
            "计算明细": result,
            "精神奖励": [],
            "生成时间": at,
            "状态": REWARD_PENDING_REVIEW,
            "需财政会签": result["需财政会签"],
            "流转记录": [{"时间": at, "事项": "系统按生效规则生成建议金额"}],
        }
        self.rewards[reward_id] = reward
        case["时间线"].append(
            {"时间": at, "事项": f"{report['别名']} 的{kind}生成，建议 {amount:,.2f} 元"}
        )
        return reward

    # ---- 审批、会签与支付（强制职责分离）------------------------------

    def _approvable(self, reward_id):
        reward = self.rewards.get(reward_id)
        if reward is None:
            raise DomainError(f"未知奖励决定：{reward_id}")
        return reward, self._case(reward["案件编号"])

    def approve_reward(self, reward_id, approver, at, spiritual=()):
        """审核批准。承办自己案件的建议一律禁止；金额超权限转财政会签。"""
        reward, case = self._approvable(reward_id)
        if approver in case["承办人员"]:
            raise DomainError("不得既承办案件又批准本案奖励建议")
        if reward["状态"] != REWARD_PENDING_REVIEW:
            raise DomainError(f"决定状态为 {reward['状态']}，不能审核批准")
        rule = rule_effective_on(self.rules, reward["生成时间"])
        for honor in spiritual:
            if honor not in rule["精神奖励"]:
                raise DomainError(f"{rule['版本号']} 未设置精神奖励：{honor}")
        reward["精神奖励"] = list(spiritual)
        if reward["需财政会签"]:
            reward["状态"] = REWARD_PENDING_FINANCE
            reward["流转记录"].append(
                {"时间": at, "事项": f"审核人 {approver} 通过，金额超权限，转财政会签"})
            return {"奖励编号": reward_id, "状态": reward["状态"], "待办": "财政会签"}
        reward["状态"] = REWARD_PENDING_PAYMENT
        reward["流转记录"].append({"时间": at, "事项": f"审核人 {approver} 批准，待支付"})
        return {"奖励编号": reward_id, "状态": reward["状态"], "待办": "支付"}

    def countersign_reward(self, reward_id, finance_actor, at):
        """财政会签：超过阈值的决定必经；财政人员同样不得是案件承办人。"""
        reward, case = self._approvable(reward_id)
        if finance_actor in case["承办人员"]:
            raise DomainError("案件承办人不得承担财政会签")
        if not reward["需财政会签"]:
            raise DomainError("该决定未达财政会签阈值，无需会签")
        if reward["状态"] != REWARD_PENDING_FINANCE:
            raise DomainError(f"决定状态为 {reward['状态']}，不能会签")
        reward["状态"] = REWARD_PENDING_PAYMENT
        reward["流转记录"].append({"时间": at, "事项": f"财政部门 {finance_actor} 会签通过"})
        return {"奖励编号": reward_id, "状态": reward["状态"], "待办": "支付"}

    def pay_reward(self, reward_id, actor, at):
        reward, _case = self._approvable(reward_id)
        if reward["状态"] != REWARD_PENDING_PAYMENT:
            raise DomainError(f"决定状态为 {reward['状态']}，不能支付")
        audit_id, _identity = self.vault.reveal(
            reward["别名"], actor, "奖励金发放需要核验收款人身份", at)
        reward["状态"] = REWARD_PAID
        reward["支付"] = {"支付时间": at, "支付金额": reward["建议金额"], "身份揭示审计": audit_id}
        reward["流转记录"].append({"时间": at, "事项": f"支付 {reward['建议金额']:,.2f} 元（审计 {audit_id}）"})
        return {"奖励编号": reward_id, "状态": reward["状态"], "实付金额": reward["建议金额"]}

    def clawback_reward(self, reward_id, actor, at):
        reward, _case = self._approvable(reward_id)
        if reward["状态"] != REWARD_PENDING_CLAWBACK:
            raise DomainError(f"决定状态为 {reward['状态']}，无需追缴")
        reward["状态"] = REWARD_CLAWED_BACK
        reward["追缴"] = {"追缴时间": at, "追缴金额": reward["建议金额"], "办理人": actor}
        reward["流转记录"].append({"时间": at, "事项": f"追缴 {reward['建议金额']:,.2f} 元"})
        return {"奖励编号": reward_id, "状态": reward["状态"], "追缴金额": reward["建议金额"]}

    # ---- 复议、判决变化：追加决定 -------------------------------------

    def review_case(self, *, case_id, actor, event_date, reason,
                    penalty_confiscation=0, no_penalty=False):
        """行政复议或判决改变罚没结果：按事件当日规则重算，追加决定调整差额。

        旧决定标记“已调整”但完整保留；补差为正走审批与会签，减少为负生成追缴。
        """
        case = self._case(case_id)
        if case["状态"] not in (CASE_REWARDABLE, CASE_ADJUSTED):
            raise DomainError("案件尚未进入可奖励阶段，不能发起复议调整")
        inflight = (REWARD_PENDING_REVIEW, REWARD_PENDING_FINANCE,
                    REWARD_PENDING_PAYMENT, REWARD_PENDING_CLAWBACK)
        pending = [r for r in self.rewards.values()
                   if r["案件编号"] == case_id and r["状态"] in inflight]
        if pending:
            raise DomainError("尚有奖励决定在审核、会签、支付或追缴流程中，应先办结再发起调整")
        if no_penalty:
            penalty_confiscation = 0
        rule = rule_effective_on(self.rules, event_date)
        case["罚没款"] = penalty_confiscation
        case["无罚没款"] = bool(no_penalty or penalty_confiscation == 0)
        case["状态"] = CASE_ADJUSTED
        case["时间线"].append({"时间": event_date, "事项": f"{reason}（{actor} 提起，适用 {rule['版本号']}）"})

        # 以每人当前最新（未被替代、非备查）的决定为基准，形成决定链条；初版始终留存。
        latest_by_reporter = {}
        for reward in self.rewards.values():
            if reward["案件编号"] != case_id or reward["状态"] in (REWARD_TERMINATED, REWARD_UPHELD):
                continue
            # 已撤回的举报人不再参与复议重算（其结论由撤回/追缴决定处理）。
            if self._report(reward["举报编号"])["状态"] == "已撤回":
                continue
            old = latest_by_reporter.get(reward["举报编号"])
            if old is None or reward["奖励编号"] > old["奖励编号"]:
                latest_by_reporter[reward["举报编号"]] = reward
        adjustments = []
        for old in latest_by_reporter.values():
            if old["状态"] == REWARD_SUPERSEDED:
                continue
            report = self._report(old["举报编号"])
            recognition = case["贡献认定"][report["举报编号"]]
            result = compute_reward(
                rule, case["违法类别"], penalty_confiscation,
                recognition["举报等级"], recognition["贡献顺序"], report["内部举报"],
            )
            new_amount = result["建议金额"] if result else 0.0
            delta = _money(new_amount - old["建议金额"])
            if delta != 0:
                old["状态"] = REWARD_SUPERSEDED
                old["流转记录"].append(
                    {"时间": event_date, "事项": f"因{reason}被追加决定调整，旧结论留存"})
            else:
                old["流转记录"].append(
                    {"时间": event_date, "事项": f"因{reason}复核，追加决定维持原结论"})
            self._reward_seq += 1
            new_id = f"JL{self._reward_seq:04d}"
            if delta > 0:
                kind, status, amount = "补差奖励决定（追加决定）", REWARD_PENDING_REVIEW, delta
            elif delta < 0:
                kind, status, amount = "追缴奖励决定（追加决定）", REWARD_PENDING_CLAWBACK, -delta
            else:
                kind, status, amount = "维持原决定（追加决定）", REWARD_UPHELD, old["建议金额"]
            adjustment = {
                "奖励编号": new_id,
                "案件编号": case_id,
                "举报编号": report["举报编号"],
                "别名": report["别名"],
                "决定类型": kind,
                "替代决定": old["奖励编号"],
                "建议金额": amount,
                "计算明细": result or {"适用版本": rule["版本号"], "建议金额": 0},
                "精神奖励": [],
                "生成时间": event_date,
                "状态": status,
                "需财政会签": delta > 0 and amount >= rule["财政会签阈值"],
                "差额": delta,
                "仅备查": delta == 0,
                "流转记录": [
                    {"时间": event_date,
                     "事项": f"追加决定：新标 {new_amount:,.2f} / 原标 {old['建议金额']:,.2f}，差额 {delta:,.2f}"},
                ],
            }
            self.rewards[new_id] = adjustment
            adjustments.append(new_id)
        return {"案件编号": case_id, "状态": case["状态"], "追加决定": adjustments}

    # ---- 逐人说明与脱敏投影 -------------------------------------------

    def reporter_explanations(self, case_id):
        """对同一违法行为的每名举报人逐人说明：资格、待办审批、实际支付。

        同一人可能先后提交多条线索（含其本人的重复举报），按稳定别名归并为一人。
        """
        case = self._case(case_id)
        people = {}
        order = []
        for report_id in case["关联举报"]:
            report = self._report(report_id)
            key = report["举报人编号"]
            if key not in people:
                people[key] = {"别名": report["别名"], "举报记录": []}
                order.append(key)
            people[key]["举报记录"].append(report_id)

        role_rank = {CONTRIBUTION_FIRST: 0, CONTRIBUTION_INDEPENDENT: 1,
                     CONTRIBUTION_NONE: 2, CONTRIBUTION_DUPLICATE: 3}
        rows = []
        for key in order:
            group = people[key]
            report_ids = group["举报记录"]
            reports = [self._report(rid) for rid in report_ids]
            alias = group["别名"]
            rewards = sorted(
                (r for r in self.rewards.values()
                 if r["案件编号"] == case_id and r["举报编号"] in report_ids),
                key=lambda r: r["奖励编号"])
            paid = sum(self._paid_total(r["奖励编号"]) for r in rewards)
            clawed = sum(r.get("追缴", {}).get("追缴金额", 0) for r in rewards
                         if r["状态"] == REWARD_CLAWED_BACK)

            recognitions = [case["贡献认定"].get(rid) for rid in report_ids]
            recognitions = [rec for rec in recognitions if rec]
            best = min(recognitions,
                       key=lambda rec: role_rank.get(rec["贡献顺序"], 9),
                       default=None)
            withdrawn = all(report["状态"] == "已撤回" for report in reports)
            rewardable = [self._report(rid) for rid in report_ids
                          if (case["贡献认定"].get(rid) or {}).get("贡献顺序") in REWARDABLE_ROLES
                          and self._report(rid)["状态"] != "已撤回"]
            if withdrawn:
                eligible, reason = False, "举报已撤回"
            elif rewardable:
                rec = case["贡献认定"][rewardable[0]["举报编号"]]
                eligible, reason = True, f"{rec['贡献顺序']}，{rec['举报等级']}举报"
            elif not recognitions:
                eligible, reason = False, "尚未完成贡献认定"
            elif best["贡献顺序"] == CONTRIBUTION_DUPLICATE:
                eligible, reason = False, "同一内容重复举报，不具备奖励资格"
            else:
                eligible, reason = False, "贡献不予认定"

            current = next((r for r in reversed(rewards)
                            if r["状态"] not in (REWARD_SUPERSEDED, REWARD_UPHELD)), None)
            rows.append({
                "别名": alias,
                "举报次数": len(report_ids),
                "举报记录": [{"举报编号": rid, "接收时间": self._report(rid)["接收时间"],
                             "举报状态": self._report(rid)["状态"],
                             "内部举报": self._report(rid)["内部举报"]} for rid in report_ids],
                "贡献顺序": best["贡献顺序"] if best else "待认定",
                "奖励资格": "具备" if eligible else "不具备",
                "资格说明": reason,
                "适用版本": current["计算明细"].get("适用版本") if current else None,
                "建议金额": current["建议金额"] if current else None,
                "待办审批": self._pending_action(current),
                "累计已付": _money(paid - clawed),
                "决定轨迹": [{"决定": r["奖励编号"], "类型": r["决定类型"], "状态": r["状态"],
                             "金额": r["建议金额"], "替代": r["替代决定"]} for r in rewards],
            })
        return rows

    @staticmethod
    def _pending_action(reward):
        if reward is None:
            return "无"
        return {
            REWARD_PENDING_REVIEW: "等待审核人批准",
            REWARD_PENDING_FINANCE: "超权限，等待财政会签",
            REWARD_PENDING_PAYMENT: "会签/审核完成，等待支付",
            REWARD_PAID: "已支付办结",
            REWARD_PENDING_CLAWBACK: "等待追缴",
            REWARD_CLAWED_BACK: "已追缴办结",
            REWARD_TERMINATED: "已终止",
            REWARD_SUPERSEDED: "已被追加决定更新",
            REWARD_UPHELD: "追加决定维持，无需新支付",
            REWARD_REJECTED: "已驳回",
        }.get(reward["状态"], reward["状态"])

    def case_worklog(self, case_id):
        """普通办案日志：只有别名、证据编号与流程节点，无法识别举报者。"""
        case = self._case(case_id)
        lines = [f"案件 {case_id}（{case['违法类别']}）办案日志："]
        for node in copy.deepcopy(case["时间线"]):
            lines.append(f"- {node.pop('时间')} {node.pop('事项')}"
                         + (f"（{node}）" if node else ""))
        for report_id in case["关联举报"]:
            report = self._report(report_id)
            for node in report["时间线"]:
                extras = {k: v for k, v in node.items() if k not in ("时间", "事项")}
                lines.append(f"- {node['时间']} {report['别名']}：{node['事项']}"
                             + (f"（{extras}）" if extras else ""))
        return "\n".join(lines)

    def external_materials(self, case_id):
        """对外材料：顺序代称 + 决定结果，不含别名编号、身份与线索细节。

        同一人的多条线索归并为一个代称，取其最高贡献顺序。
        """
        case = self._case(case_id)
        order_name = {CONTRIBUTION_FIRST: "首位线索提供者",
                      CONTRIBUTION_INDEPENDENT: "后续独立线索提供者",
                      CONTRIBUTION_DUPLICATE: "重复线索提供者",
                      CONTRIBUTION_NONE: "其他线索提供者"}
        role_rank = {CONTRIBUTION_FIRST: 0, CONTRIBUTION_INDEPENDENT: 1,
                     CONTRIBUTION_NONE: 2, CONTRIBUTION_DUPLICATE: 3}
        groups, order = {}, []
        for report_id in case["关联举报"]:
            report = self._report(report_id)
            key = report["举报人编号"]
            if key not in groups:
                groups[key] = {"report_ids": [], "recognition": None}
                order.append(key)
            groups[key]["report_ids"].append(report_id)
            rec = case["贡献认定"].get(report_id)
            current_best = groups[key]["recognition"]
            if rec and (current_best is None
                        or role_rank[rec["贡献顺序"]] < role_rank[current_best["贡献顺序"]]):
                groups[key]["recognition"] = rec

        items = []
        for key in order:
            group = groups[key]
            recognition = group["recognition"]
            label = order_name[recognition["贡献顺序"]] if recognition else "线索提供者"
            rewards = [r for r in self.rewards.values()
                       if r["举报编号"] in group["report_ids"] and r["案件编号"] == case_id
                       and r["状态"] not in (REWARD_SUPERSEDED, REWARD_UPHELD)]
            items.append({
                "代称": label,
                "奖励资格": any(r["状态"] not in (REWARD_TERMINATED, REWARD_REJECTED) for r in rewards),
                "奖励决定": [{"类型": r["决定类型"], "状态": r["状态"], "金额": r["建议金额"]}
                           for r in rewards],
            })
        return {
            "案件编号": case_id,
            "违法类别": case["违法类别"],
            "罚没结果": "无罚没款" if case["无罚没款"] else f"罚没 {case['罚没款']:,.2f} 元",
            "案件状态": case["状态"],
            "线索与奖励": items,
        }

    def case_brief(self, case_id):
        """承办人员视图：仅别名、证据编号、关联案件等办案所需信息。"""
        case = self._case(case_id)
        return {
            "案件编号": case_id,
            "违法类别": case["违法类别"],
            "状态": case["状态"],
            "承办人员": case["承办人员"],
            "关联举报": [
                {"举报编号": rid, "别名": self._report(rid)["别名"],
                 "证据材料": self._report(rid)["证据材料"],
                 "举报状态": self._report(rid)["状态"],
                 "贡献认定": case["贡献认定"].get(rid)}
                for rid in case["关联举报"]
            ],
        }

    # ---- 内部工具 ------------------------------------------------------

    def _report(self, report_id):
        report = self.reports.get(report_id)
        if report is None:
            raise DomainError(f"未知举报编号：{report_id}")
        return report

    def _case(self, case_id):
        case = self.cases.get(case_id)
        if case is None:
            raise DomainError(f"未知案件编号：{case_id}")
        return case

    def _paid_total(self, reward_id):
        reward = self.rewards.get(reward_id)
        if reward and reward.get("支付"):
            return reward["支付"]["支付金额"]
        return 0

    def _create_clawback(self, paid_reward, amount, at, reason):
        self._reward_seq += 1
        claw_id = f"JL{self._reward_seq:04d}"
        self.rewards[claw_id] = {
            "奖励编号": claw_id,
            "案件编号": paid_reward["案件编号"],
            "举报编号": paid_reward["举报编号"],
            "别名": paid_reward["别名"],
            "决定类型": "追缴奖励决定（撤回）",
            "替代决定": paid_reward["奖励编号"],
            "建议金额": amount,
            "计算明细": {"原因": reason},
            "精神奖励": [],
            "生成时间": at,
            "状态": REWARD_PENDING_CLAWBACK,
            "需财政会签": False,
            "流转记录": [{"时间": at, "事项": reason}],
        }
        return self.rewards[claw_id]
