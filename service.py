"""市场举报奖励的运行入口。

除健康检查外，提供举报登记、证据补充、案件关联、贡献认定、结案建议、
审核/财政会签/支付、撤回与复议追加决定、逐人说明及脱敏材料等接口。
所有业务记录由领域模块（domain.py）产生，接口层不做金额计算。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import domain
from domain import DomainError, RewardCenter

SERVICE_ID = "whistleblower-reward"
SERVICE_NAME = "市场举报奖励"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def check_configuration():
    """启动前自检：身份与规则配置必须可用。"""
    rules = domain.load_rules()
    # 每个生效版本都应覆盖三级物质奖励、精神奖励与一百万元上限。
    for version in rules["规则版本"]:
        assert version["最高限额"] == 1000000, "最高限额应为一百万元"
        assert 0 < version["财政会签阈值"] <= version["最高限额"]
        assert set(version["等级系数"]) == {"一级", "二级", "三级"}
        assert version["精神奖励"], "精神奖励不得为空"
    return rules


class Handler(BaseHTTPRequestHandler):
    """JSON 接口；真实身份仅在登记时进入身份库，响应一律使用别名。"""

    center = RewardCenter()

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/health":
            self._write_json(200, health_payload())
            return
        case_id = self._match(route, "/cases/", "/explanations")
        if case_id:
            self._call(lambda: self.center.reporter_explanations(case_id))
            return
        case_id = self._match(route, "/cases/", "/worklog")
        if case_id:
            self._call(lambda: self.center.case_worklog(case_id), raw_text=True)
            return
        case_id = self._match(route, "/cases/", "/external")
        if case_id:
            self._call(lambda: self.center.external_materials(case_id))
            return
        case_id = self._match(route, "/cases/", "/brief")
        if case_id:
            self._call(lambda: self.center.case_brief(case_id))
            return
        self.send_error(404)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/reports":
            self._call(lambda: self.center.receive_report(**self._body()))
            return
        report_id, action = self._match2(route, "/reports/")
        if report_id and action:
            body = self._body()
            if action == "evidence":
                self._call(lambda: self.center.add_evidence(report_id, **body))
                return
            if action == "link":
                self._call(lambda: self.center.link_case(report_id, **body))
                return
            if action == "withdraw":
                self._call(lambda: self.center.withdraw_report(report_id, **body))
                return
        if route == "/cases":
            self._call(lambda: self.center.open_case(**self._body()))
            return
        case_id, action = self._match2(route, "/cases/")
        if case_id and action == "recognize":
            body = self._body()
            assignments = {rid: tuple(value) for rid, value in body["assignments"].items()}
            self._call(lambda: self.center.assess_contributions(
                case_id, assignments, body["actor"], body["at"]))
            return
        if case_id and action == "close":
            body = self._body()
            self._call(lambda: self.center.close_case(case_id=case_id, **body))
            return
        if case_id and action == "review":
            body = self._body()
            self._call(lambda: self.center.review_case(case_id=case_id, **body))
            return
        reward_id, action = self._match2(route, "/rewards/")
        if reward_id and action == "approve":
            self._call(lambda: self.center.approve_reward(reward_id, **self._body()))
            return
        if reward_id and action == "countersign":
            self._call(lambda: self.center.countersign_reward(reward_id, **self._body()))
            return
        if reward_id and action == "pay":
            self._call(lambda: self.center.pay_reward(reward_id, **self._body()))
            return
        if reward_id and action == "clawback":
            self._call(lambda: self.center.clawback_reward(reward_id, **self._body()))
            return
        self.send_error(404)

    # -- 工具 -----------------------------------------------------------

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            self._write_json(400, {"错误": f"请求体不是合法 JSON：{error}"})
            raise _Handled()

    def _call(self, func, raw_text=False):
        try:
            result = func()
        except DomainError as error:
            self._write_json(422, {"错误": str(error)})
        except (_Handled,):
            pass
        except TypeError as error:
            self._write_json(400, {"错误": f"参数不匹配：{error}"})
        else:
            if raw_text:
                payload = result.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self._write_json(200, result)

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _match(route, prefix, suffix):
        if route.startswith(prefix) and route.endswith(suffix):
            middle = route[len(prefix):-len(suffix)]
            if middle and "/" not in middle:
                return middle
        return None

    @staticmethod
    def _match2(route, prefix):
        if not route.startswith(prefix):
            return None, None
        parts = route[len(prefix):].split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
        return (parts[0], None) if len(parts) == 1 and parts[0] else (None, None)

    def log_message(self, *_args):
        return


class _Handled(Exception):
    """错误响应已写出，用于中断处理链。"""


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        check_configuration()
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
