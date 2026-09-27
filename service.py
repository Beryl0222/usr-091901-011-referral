"""县域双向转诊闭环的基础服务入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from referral_domain import MILESTONES, STATES, DomainError, ReferralCenter

SERVICE_ID = "bidirectional-referral"
SERVICE_NAME = "县域双向转诊闭环"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

CENTER = ReferralCenter()


def reset_center():
    """清空内存中的业务状态（测试与演示用）。"""
    global CENTER
    CENTER = ReferralCenter()
    return CENTER


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查、领域契约与转诊闭环业务接口。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            status, payload = self._route(method)
        except DomainError as exc:
            status, payload = exc.status, {"error": str(exc)}
        except (KeyError, TypeError, ValueError):
            status, payload = 400, {"error": "请求参数不完整或不合法"}
        except Exception as exc:  # 兜底，避免连接被直接挂断
            status, payload = 500, {"error": f"服务内部错误:{exc}"}
        self._send_json(payload, status)

    def _route(self, method):
        parsed = urlparse(self.path)
        seg = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        if method == "GET" and seg == ["health"]:
            return 200, health_payload()
        if method == "GET" and seg == ["contract"]:
            return 200, load_contract()
        if seg == ["actors"] and method == "POST":
            payload = self._read_json()
            actor = CENTER.register_actor(self._actor_id(), payload["id"], payload["name"],
                                          payload["role"], payload.get("org"))
            return 201, actor
        if seg == ["actors"] and method == "GET":
            return 200, {"actors": CENTER.list_actors(self._actor_id())}
        if seg == ["roster"] and method == "POST":
            payload = self._read_json()
            return 200, CENTER.set_roster(self._actor_id(), payload.get("date"),
                                          payload.get("entries") or [])
        if seg == ["roster"] and method == "GET":
            return 200, CENTER.get_roster(self._actor_id(), query.get("date", [None])[0])
        if seg == ["referrals"] and method == "POST":
            payload = self._read_json()
            return 201, CENTER.submit_referral(self._actor_id(), payload,
                                               payload.get("message_id"))
        if seg == ["referrals"] and method == "GET":
            return 200, {"referrals": CENTER.list_for(self._actor_id(),
                                                      query.get("status", [None])[0])}
        if seg == ["open-items"] and method == "GET":
            return 200, {"open_items": CENTER.open_items(self._actor_id())}
        if len(seg) >= 2 and seg[0] == "referrals":
            referral_id = seg[1]
            if len(seg) == 2 and method == "GET":
                return 200, CENTER.view_for(self._actor_id(), referral_id)
            if len(seg) == 3 and seg[2] == "timeline" and method == "GET":
                return 200, CENTER.timeline_for(self._actor_id(), referral_id)
            if len(seg) == 3 and method == "POST":
                payload = self._read_json()
                message_id = payload.get("message_id")
                action = seg[2]
                if action == "claim":
                    return 200, CENTER.claim(self._actor_id(), referral_id, message_id)
                if action == "contact":
                    return 200, CENTER.confirm_contact(self._actor_id(), referral_id,
                                                       payload, message_id)
                if action == "arrival":
                    return 200, CENTER.register_arrival(self._actor_id(), referral_id,
                                                        payload, message_id)
                if action == "emergency":
                    return 200, CENTER.divert_emergency(self._actor_id(), referral_id,
                                                        payload, message_id)
                if action == "milestones":
                    return 200, CENTER.record_milestone(self._actor_id(), referral_id,
                                                        payload, message_id)
                if action == "accept":
                    return 200, CENTER.accept_downward(self._actor_id(), referral_id,
                                                       payload, message_id)
                if action == "follow-ups":
                    return 200, CENTER.record_follow_up(self._actor_id(), referral_id,
                                                        payload, message_id)
                if action == "escalations":
                    return 200, CENTER.escalate(self._actor_id(), referral_id,
                                                payload, message_id)
                if action == "notes":
                    return 200, CENTER.record_note(self._actor_id(), referral_id,
                                                   payload, message_id)
            if (len(seg) == 5 and seg[2] == "escalations" and seg[4] == "resolve"
                    and method == "POST"):
                payload = self._read_json()
                return 200, CENTER.resolve_escalation(self._actor_id(), referral_id,
                                                      seg[3], payload,
                                                      payload.get("message_id"))
        raise DomainError("接口不存在", 404)

    def _actor_id(self):
        return self.headers.get("X-Actor-Id")

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        assert tuple(contract["states"]) == STATES, "契约状态与领域实现不一致"
        assert tuple(contract.get("milestones", ())) == MILESTONES, "契约院内节点与领域实现不一致"
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
