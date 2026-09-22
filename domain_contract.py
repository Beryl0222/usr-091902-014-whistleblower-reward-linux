"""领域契约：三人先后举报、无罚没款结案、跨生效日复议等争议场景。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain import (
    CONTRIBUTION_DUPLICATE,
    CONTRIBUTION_FIRST,
    CONTRIBUTION_INDEPENDENT,
    DomainError,
    RewardCenter,
    compute_reward,
    load_rules,
    rule_effective_on,
)
from service import Handler, health_payload

IDENTITY_MARKERS = ["艾实名", "110101199001011234", "步实名", "220102199202024567",
                    "常实名", "660106199503037890"]


def identity(name, idno):
    return {
        "姓名": name,
        "证件号码": idno,
        "联系方式": "13800000000",
        "银行账户": "6222000000000000",
        "住址": "某市某区某街",
        "工作单位": "某公司",
    }


def post(base_url, route, payload):
    request = Request(
        f"{base_url}{route}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


class ThreeReportersFlowTest(unittest.TestCase):
    """同一违法行为三人举报：先后顺序、重复举报、内部激励、会签、跨生效日复议。"""

    def setUp(self):
        self.center = RewardCenter()
        c = self.center
        # A 最早、内部举报；B 随后提供独立关键证据；C 就同一内容重复举报。
        a = c.receive_report(
            violation_key="VIOL-FOOD-01", violation_category="食品安全",
            received_at="2025-01-03", identity=identity("艾实名", "110101199001011234"),
            is_internal=True, content="某厂使用过期原料生产饼干", evidence_ids=["EV-A1"])
        b = c.receive_report(
            violation_key="VIOL-FOOD-01", violation_category="食品安全",
            received_at="2025-01-08", identity=identity("步实名", "220102199202024567"),
            content="独立掌握的仓储录像与批号记录", evidence_ids=["EV-B1"])
        dup = c.receive_report(
            violation_key="VIOL-FOOD-01", violation_category="食品安全",
            received_at="2025-01-09", identity=identity("常实名", "660106199503037890"),
            content="某厂使用过期原料生产饼干")
        self.a, self.b, self.dup = a["举报编号"], b["举报编号"], dup["举报编号"]
        self.alias_a = a["别名"]
        self.assertEqual(dup["状态"], "重复举报")
        self.assertEqual(dup["重复于"], self.a)  # 内容相同即识别为重复举报
        self.assertNotEqual(dup["别名"], self.alias_a)  # C 是第三人，获得独立别名

        c.open_case(case_id="AJ2025-01", violation_key="VIOL-FOOD-01",
                    violation_category="食品安全", opened_at="2025-01-10",
                    handlers=["张承办"])
        c.assess_contributions("AJ2025-01", {
            self.a: (CONTRIBUTION_FIRST, "一级"),
            self.b: (CONTRIBUTION_INDEPENDENT, "二级"),
            self.dup: (CONTRIBUTION_DUPLICATE, "三级"),
        }, actor="认定员甲", at="2025-03-01")
        self.closed = c.close_case(case_id="AJ2025-01", actor="张承办",
                                   closed_at="2025-03-10", penalty_confiscation=5_000_000)

    def test_rewards_are_system_generated_with_internal_uplift_and_ordering(self):
        rewards = {r["举报编号"]: r for r in self._reward_objects()}
        # A：500万×5%×一级1.0×首位1.0×内部上浮1.2 = 30万，超20万必须财政会签。
        self.assertEqual(rewards[self.a]["建议金额"], 300_000.0)
        self.assertTrue(rewards[self.a]["需财政会签"])
        self.assertEqual(rewards[self.a]["计算明细"]["适用版本"], "v2022")
        # B：500万×5%×二级0.7×独立关键0.6 = 10.5万，不触发会签。
        self.assertEqual(rewards[self.b]["建议金额"], 105_000.0)
        self.assertFalse(rewards[self.b]["需财政会签"])
        # C 为重复举报，不产生奖励决定。
        self.assertNotIn(self.dup, rewards)

    def test_handler_cannot_approve_own_case(self):
        reward_a = self.closed["奖励决定"][0]
        with self.assertRaisesRegex(DomainError, "不得既承办"):
            self.center.approve_reward(reward_a, "张承办", "2025-03-12")

    def test_approve_finance_countersign_and_pay_chain(self):
        c = self.center
        rid_a = next(r["奖励编号"] for r in self._reward_objects() if r["举报编号"] == self.a)
        rid_b = next(r["奖励编号"] for r in self._reward_objects() if r["举报编号"] == self.b)
        # A 超权限：审核后只能到财政会签，不能直接支付。
        self.assertEqual(c.approve_reward(rid_a, "李审核", "2025-03-12",
                                          spiritual=["通报表扬"])["待办"], "财政会签")
        with self.assertRaisesRegex(DomainError, "会签"):
            c.pay_reward(rid_a, "出纳王", "2025-03-13")
        with self.assertRaisesRegex(DomainError, "承办人不得承担财政会签"):
            c.countersign_reward(rid_a, "张承办", "2025-03-14")
        c.countersign_reward(rid_a, "财政赵", "2025-03-15")
        paid_a = c.pay_reward(rid_a, "出纳王", "2025-03-16")
        self.assertEqual(paid_a["实付金额"], 300_000.0)
        # B 不超权限：审核后直接支付；支付环节才发生身份揭示。
        self.assertEqual(len(c.vault.audit_trail), 1)
        c.approve_reward(rid_b, "李审核", "2025-03-12")
        c.pay_reward(rid_b, "出纳王", "2025-03-17")
        self.assertEqual(len(c.vault.audit_trail), 2)
        self.assertTrue(all(row["理由"] for row in c.vault.audit_trail))

    def test_cross_effective_date_review_appends_decisions_and_keeps_old(self):
        c = self.center
        rid_a = next(r["奖励编号"] for r in self._reward_objects() if r["举报编号"] == self.a)
        rid_b = next(r["奖励编号"] for r in self._reward_objects() if r["举报编号"] == self.b)
        c.approve_reward(rid_a, "李审核", "2025-03-12")
        c.countersign_reward(rid_a, "财政赵", "2025-03-15")
        c.pay_reward(rid_a, "出纳王", "2025-03-16")
        c.approve_reward(rid_b, "李审核", "2025-03-12")
        c.pay_reward(rid_b, "出纳王", "2025-03-17")

        # 2025-06-01 判决改变罚没结果，当日 v2025 已生效：追加决定调整差额。
        result = c.review_case(case_id="AJ2025-01", actor="法制员钱",
                               event_date="2025-06-01", reason="行政判决变更罚没数额",
                               penalty_confiscation=3_000_000)
        self.assertEqual(len(result["追加决定"]), 2)
        # 旧决定标记已调整但完整保留。
        self.assertEqual(c.rewards[rid_a]["状态"], "已调整")
        self.assertEqual(c.rewards[rid_a]["建议金额"], 300_000.0)
        self.assertEqual(c.rewards[rid_b]["状态"], "已调整")

        rows = {row["别名"]: row for row in c.reporter_explanations("AJ2025-01")}
        row_a = rows[self.alias_a]
        # A 新标：300万×6%×1.0×1.0×1.3 = 23.4万，差额 -6.6万，等待追缴。
        self.assertEqual(row_a["适用版本"], "v2025")
        self.assertEqual(row_a["建议金额"], 66_000.0)
        self.assertEqual(row_a["待办审批"], "等待追缴")
        self.assertEqual(row_a["累计已付"], 300_000.0)  # 追缴尚未执行
        trail_types = [(item["类型"], item["金额"], item["替代"]) for item in row_a["决定轨迹"]]
        self.assertIn(("初步物质奖励决定", 300_000.0, None), trail_types)
        self.assertTrue(any(item["类型"] == "追缴奖励决定（追加决定）" for item in row_a["决定轨迹"]))

        # 执行追缴后，逐人说明的实际支付净额随之更新。
        claw_a = next(item["决定"] for item in row_a["决定轨迹"]
                      if item["类型"] == "追缴奖励决定（追加决定）")
        c.clawback_reward(claw_a, "出纳王", "2025-06-10")
        row_a2 = next(row for row in c.reporter_explanations("AJ2025-01")
                      if row["别名"] == self.alias_a)
        self.assertEqual(row_a2["累计已付"], 234_000.0)

    def test_review_rejected_while_decisions_inflight(self):
        rid_a = self.closed["奖励决定"][0]
        self.center.approve_reward(rid_a, "李审核", "2025-03-12")  # A 停在财政会签
        with self.assertRaisesRegex(DomainError, "先办结"):
            self.center.review_case(case_id="AJ2025-01", actor="法制员钱",
                                    event_date="2025-06-01", reason="复议",
                                    penalty_confiscation=3_000_000)

    def test_two_first_contributions_rejected(self):
        with self.assertRaisesRegex(DomainError, "只能有一个"):
            self.center.assess_contributions("AJ2025-01", {
                self.a: (CONTRIBUTION_FIRST, "一级"),
                self.b: (CONTRIBUTION_FIRST, "一级"),
            }, actor="认定员乙", at="2025-03-02")

    def test_case_views_never_identify_reporters(self):
        c = self.center
        brief = json.dumps(c.case_brief("AJ2025-01"), ensure_ascii=False)
        worklog = c.case_worklog("AJ2025-01")
        external = json.dumps(c.external_materials("AJ2025-01"), ensure_ascii=False)
        explanations = json.dumps(c.reporter_explanations("AJ2025-01"), ensure_ascii=False)
        for view in (brief, worklog, external, explanations):
            for marker in IDENTITY_MARKERS:
                self.assertNotIn(marker, view)
        # 承办视图只有别名与证据编号；对外材料连“举报人A”字样都不出现。
        self.assertIn(self.alias_a, brief)
        self.assertIn("EV-A1", brief)
        self.assertNotIn("举报人A", external)
        self.assertNotIn("举报人B", external)
        self.assertIn("首位线索提供者", external)
        self.assertIn("重复线索提供者", external)
        # 对外材料不交代承办人姓名等办案信息。
        self.assertNotIn("张承办", external)

    def _reward_objects(self):
        return [r for r in self.center.rewards.values() if r["案件编号"] == "AJ2025-01"]


class NoPenaltyCaseTest(unittest.TestCase):
    """无罚没款结案：定额奖励；支付前撤回终止、支付后撤回追加追缴。"""

    def setUp(self):
        self.center = RewardCenter()
        c = self.center
        d = c.receive_report(
            violation_key="VIOL-PRICE-09", violation_category="价格违法",
            received_at="2025-02-01", identity=identity("丁实名", "330103198505051111"),
            is_internal=True, content="串通涨价的内部线索")
        e = c.receive_report(
            violation_key="VIOL-PRICE-09", violation_category="价格违法",
            received_at="2025-02-03", identity=identity("戊实名", "440104198606062222"),
            content="另一份独立票据线索")
        self.d, self.e = d["举报编号"], e["举报编号"]
        self.alias_d, self.alias_e = d["别名"], e["别名"]
        c.open_case(case_id="AJ2025-09", violation_key="VIOL-PRICE-09",
                    violation_category="价格违法", opened_at="2025-02-05",
                    handlers=["王承办"])
        c.assess_contributions("AJ2025-09", {
            self.d: (CONTRIBUTION_FIRST, "一级"),
            self.e: (CONTRIBUTION_INDEPENDENT, "二级"),
        }, actor="认定员丙", at="2025-03-01")
        # E 在结案前撤回，结案时不再生成奖励；案件无罚没款。
        c.withdraw_report(self.e, "接待员己", "2025-03-05")
        self.closed = c.close_case(case_id="AJ2025-09", actor="王承办",
                                   closed_at="2025-03-20", no_penalty=True)

    def test_no_penalty_uses_fixed_amount(self):
        # 价格违法 v2022 无罚没款定额 2000 × 一级1.0 × 首位1.0 × 内部1.2 = 2400。
        self.assertEqual(len(self.closed["奖励决定"]), 1)
        rid = self.closed["奖励决定"][0]
        reward = self.center.rewards[rid]
        self.assertEqual(reward["建议金额"], 2400.0)
        self.assertEqual(reward["计算明细"]["计算方式"], "无罚没款定额")

    def test_withdraw_before_and_after_payment(self):
        c = self.center
        rid = self.closed["奖励决定"][0]
        c.approve_reward(rid, "周审核", "2025-03-22", spiritual=["颁发荣誉证书"])
        c.pay_reward(rid, "出纳孙", "2025-03-25")
        # 支付后撤回：旧支付结论保留，新增待追缴决定。
        c.withdraw_report(self.d, "接待员己", "2025-04-02")
        rows = {row["别名"]: row for row in c.reporter_explanations("AJ2025-09")}
        self.assertEqual(c.rewards[rid]["状态"], "已支付")  # 旧结论不抹除
        row_d = rows[self.alias_d]
        self.assertEqual(row_d["奖励资格"], "不具备")
        self.assertEqual(row_d["资格说明"], "举报已撤回")
        self.assertEqual(row_d["待办审批"], "等待追缴")
        claw = next(item["决定"] for item in row_d["决定轨迹"]
                    if item["类型"] == "追缴奖励决定（撤回）")
        c.clawback_reward(claw, "出纳孙", "2025-04-10")
        row_d2 = next(row for row in c.reporter_explanations("AJ2025-09")
                      if row["别名"] == self.alias_d)
        self.assertEqual(row_d2["累计已付"], 0.0)
        # E 支付前撤回：无决定、无支付，逐人可说明。
        row_e = next(row for row in c.reporter_explanations("AJ2025-09")
                     if row["别名"] == self.alias_e)
        self.assertEqual(row_e["奖励资格"], "不具备")
        self.assertEqual(row_e["待办审批"], "无")
        self.assertIsNone(row_e["建议金额"])


class RewardFormulaTest(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.v2022 = rule_effective_on(self.rules, "2025-01-01")
        self.v2025 = rule_effective_on(self.rules, "2025-06-01")

    def test_cap_at_one_million(self):
        result = compute_reward(self.v2022, "食品安全", 100_000_000, "一级",
                                CONTRIBUTION_FIRST, True)
        self.assertEqual(result["建议金额"], 1_000_000.0)
        self.assertTrue(result["触发上限"])
        self.assertTrue(result["需财政会签"])

    def test_minimum_floor(self):
        result = compute_reward(self.v2022, "价格违法", 1_000, "一级",
                                CONTRIBUTION_FIRST, False)
        self.assertEqual(result["建议金额"], 500.0)  # 比例额30元，按最低奖励保底

    def test_no_penalty_duplicate_has_no_eligibility(self):
        self.assertIsNone(compute_reward(self.v2022, "产品质量", 0, "三级",
                                         CONTRIBUTION_DUPLICATE, False))

    def test_version_cutover_changes_coefficients(self):
        old = compute_reward(self.v2022, "食品安全", 0, "二级",
                             CONTRIBUTION_INDEPENDENT, False)
        new = compute_reward(self.v2025, "食品安全", 0, "二级",
                             CONTRIBUTION_INDEPENDENT, False)
        # 旧版 5000×0.7×0.6=2100；新版 6000×0.8×0.7=3360。
        self.assertEqual(old["建议金额"], 2100.0)
        self.assertEqual(new["建议金额"], 3360.0)

    def test_invalid_spiritual_honor_rejected(self):
        center = RewardCenter()
        center.receive_report(violation_key="V", violation_category="食品安全",
                              received_at="2025-01-01", identity=identity("测试甲", "111111")),
        rid_report = next(iter(center.reports))
        center.open_case(case_id="C1", violation_key="V", violation_category="食品安全",
                         opened_at="2025-01-02", handlers=["承办"])
        center.assess_contributions("C1", {rid_report: (CONTRIBUTION_FIRST, "一级")},
                                    actor="认定", at="2025-01-03")
        closed = center.close_case(case_id="C1", actor="承办", closed_at="2025-01-04",
                                   no_penalty=True)
        with self.assertRaisesRegex(DomainError, "精神奖励"):
            center.approve_reward(closed["奖励决定"][0], "审核", "2025-01-05",
                                  spiritual=["不存在的表彰"])

    def test_same_identity_reuses_stable_alias(self):
        center = RewardCenter()
        first = center.receive_report(
            violation_key="V1", violation_category="食品安全",
            received_at="2025-01-01", identity=identity("同实名", "990109199009099999"),
            content="第一次线索")
        second = center.receive_report(
            violation_key="V2", violation_category="食品安全",
            received_at="2025-01-02", identity=identity("同实名", "990109199009099999"),
            content="另一违法行为线索")
        self.assertEqual(second["别名"], first["别名"])
        self.assertNotEqual(second["举报编号"], first["举报编号"])

    def test_reveal_requires_reason(self):
        center = RewardCenter()
        _, alias, _ = center.vault.register(identity("测试乙", "222222"))
        with self.assertRaisesRegex(DomainError, "理由"):
            center.vault.reveal(alias, "某人", "")


class HttpEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        Handler.center = RewardCenter()  # 每个测试类使用独立内存数据
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_full_flow_over_http_without_identity_leak(self):
        status, report = post(self.base_url, "/reports", {
            "violation_key": "VIOL-WEB-1", "violation_category": "食品安全",
            "received_at": "2025-02-01",
            "identity": identity("网实名", "550105199007073333"),
            "is_internal": True, "content": "网络渠道线索"})
        self.assertEqual(status, 200)
        self.assertNotIn("identity", report)
        report_id = report["举报编号"]
        alias = report["别名"]

        status, case = post(self.base_url, "/cases", {
            "case_id": "AJ-WEB-1", "violation_key": "VIOL-WEB-1",
            "violation_category": "食品安全", "opened_at": "2025-02-02",
            "handlers": ["网承办"]})
        self.assertEqual(status, 200)
        self.assertEqual(case["关联举报"][0]["别名"], alias)

        status, _ = post(self.base_url, f"/reports/{report_id}/evidence",
                         {"evidence_id": "EV-W1", "summary": "照片", "at": "2025-02-03"})
        self.assertEqual(status, 200)
        status, _ = post(self.base_url, "/cases/AJ-WEB-1/recognize", {
            "assignments": {report_id: ["最先有效贡献", "一级"]},
            "actor": "认定员", "at": "2025-03-01"})
        self.assertEqual(status, 200)
        status, closed = post(self.base_url, "/cases/AJ-WEB-1/close", {
            "actor": "网承办", "closed_at": "2025-03-05",
            "penalty_confiscation": 5_000_000})
        self.assertEqual(status, 200)
        rid = closed["奖励决定"][0]

        # 职责分离通过 HTTP 同样强制。
        status, payload = post(self.base_url, f"/rewards/{rid}/approve",
                               {"approver": "网承办", "at": "2025-03-06"})
        self.assertEqual(status, 422)
        self.assertIn("不得既承办", payload["错误"])

        status, _ = post(self.base_url, f"/rewards/{rid}/approve",
                         {"approver": "网审核", "at": "2025-03-07"})
        self.assertEqual(status, 200)
        with urlopen(f"{self.base_url}/cases/AJ-WEB-1/explanations", timeout=3) as response:
            body = response.read().decode("utf-8")
        self.assertIn("等待财政会签", body)
        self.assertNotIn("550105199007073333", body)
        self.assertNotIn("网实名", body)

    def test_health_contract_remains_stable(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(json.load(response), health_payload())


if __name__ == "__main__":
    unittest.main()
