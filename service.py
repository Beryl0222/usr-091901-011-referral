"""县域双向转诊闭环的服务入口。

在原有健康检查与领域契约接口之外，提供：
  POST /cases                     基层提交上转
  POST /cases/{id}/commands       管家/专科/卫生院推进流程（幂等）
  GET  /cases                     按角色过滤的转诊单列表
  GET  /cases/{id}                按角色裁剪后的转诊单视图
  GET  /cases/{id}/trace          负责人追溯：交接、未决事项、失联升级、回流结果

调用方以 X-Actor-Token 表明身份，字段级最小可见在领域层完成。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from domain import (
    ROLE_CASE_MANAGER, ROLE_SUPERVISOR, ROLE_SPECIALIST,
    ROLE_RECEIVING_FACILITY, SOURCE_ROLES,
    S_PENDING_DOWN, S_IN_HOSPITAL,
    DomainError, ReferralRepository,
)

SERVICE_ID = "bidirectional-referral"
SERVICE_NAME = "县域双向转诊闭环"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

# 演示用令牌目录；生产环境应替换为鉴权服务签发的令牌
TOKENS = {
    "lead":    {"id": "U-LEAD", "role": ROLE_SUPERVISOR},
    "m1":      {"id": "M-01",   "role": ROLE_CASE_MANAGER},
    "m2":      {"id": "M-02",   "role": ROLE_CASE_MANAGER},
    "m3":      {"id": "M-03",   "role": ROLE_CASE_MANAGER},
    "spec":    {"id": "D-SPEC", "role": ROLE_SPECIALIST},
    "town":    {"id": "FW-TM",  "role": ROLE_RECEIVING_FACILITY},
    "doc":     {"id": "D-TOWN", "role": "基层医生"},
    "village": {"id": "V-01",   "role": "村医"},
    "clinic":  {"id": "C-01",   "role": "个体诊所"},
    "screen":  {"id": "S-01",   "role": "体检高危人群"},
}

repository = ReferralRepository()


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID: raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _can_list(actor, case):
    role = actor["role"]
    if role in (ROLE_CASE_MANAGER, ROLE_SUPERVISOR):
        return True
    if role in SOURCE_ROLES:
        return case.submitter_id == actor["id"]
    if role == ROLE_SPECIALIST:
        return case.status in (S_IN_HOSPITAL, S_PENDING_DOWN)
    if role == ROLE_RECEIVING_FACILITY:
        return (case.status == S_PENDING_DOWN
                or case.receiving_facility_id == actor["id"])
    return False


def _can_view(actor, case):
    if _can_list(actor, case):
        return True
    # 下转完成后，原接收卫生院仍需查看其在管/已闭环患者
    return (actor["role"] == ROLE_RECEIVING_FACILITY
            and case.receiving_facility_id == actor["id"])


class Handler(BaseHTTPRequestHandler):
    """转诊流程 HTTP 接口。"""

    def do_GET(self):
        if self.path == "/health": self._send_json(health_payload()); return
        if self.path == "/contract": self._send_json(load_contract()); return
        case_id, sub = self._case_path()
        if self.path != "/cases" and case_id is None:
            self.send_error(404); return
        actor = self._actor_or_error()
        if actor is None: return
        if self.path == "/cases":
            cases = [c.visible_fields(actor["role"])
                     for c in repository.list_cases() if _can_list(actor, c)]
            self._send_json({"cases": cases}); return
        if case_id:
            try:
                case = repository.get(case_id)
            except DomainError as exc:
                self._send_error(exc); return
            if sub is None:
                if not _can_view(actor, case):
                    self._send_json({"error": "无权查看该转诊单"}, code=403); return
                self._send_json(case.visible_fields(actor["role"])); return
            if sub == "trace":
                if actor["role"] != ROLE_SUPERVISOR and not (
                        actor["role"] == ROLE_CASE_MANAGER
                        and case.manager_id == actor["id"]):
                    self._send_json({"error": "仅负责人可追溯全部记录"}, code=403); return
                self._send_json(case.trace()); return
        self.send_error(404)

    def do_POST(self):
        case_id, sub = self._case_path()
        if self.path != "/cases" and (case_id is None or sub != "commands"):
            self.send_error(404); return
        actor = self._actor_or_error()
        if actor is None: return
        payload = self._read_body()
        if payload is None: return
        try:
            if self.path == "/cases":
                if actor["role"] not in SOURCE_ROLES:
                    self._send_json({"error": "仅基层来源可提交上转"}, code=403); return
                case = repository.submit(payload, actor)
                self._send_json({"case_id": case.id, "status": case.status,
                                 "case": case.visible_fields(actor["role"])}, code=201); return
            case, event, replayed = repository.issue(case_id, payload, actor)
            self._send_json({
                "event": event, "status": case.status,
                "idempotent_replay": replayed,
                "case": case.visible_fields(actor["role"]),
            }, code=200); return
        except DomainError as exc:
            self._send_error(exc); return

    # ----- 辅助 -----

    def _case_path(self):
        parts = [p for p in self.path.strip("/").split("/") if p]
        if len(parts) >= 2 and parts[0] == "cases":
            return parts[1], parts[2] if len(parts) > 2 else None
        return None, None

    def _actor_or_error(self):
        token = self.headers.get("X-Actor-Token")
        actor = TOKENS.get(token)
        if actor is None:
            self._send_json({"error": "未提供有效身份令牌"}, code=401)
            return None
        return actor

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "请求体不是合法 JSON"}, code=400)
            return None

    def _send_error(self, exc):
        self._send_json({"error": str(exc)}, code=getattr(exc, "code", 422))

    def _send_json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args): return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000); parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract(); assert contract["states"] and contract["invariants"]; print("基础检查通过"); return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()

if __name__ == "__main__": main()
