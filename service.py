"""县域双向转诊闭环的基础服务入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
SERVICE_ID = "bidirectional-referral"
SERVICE_NAME = "县域双向转诊闭环"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID: raise ValueError("领域契约与服务身份不一致")
    return contract

def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}

class Handler(BaseHTTPRequestHandler):
    """提供健康检查和领域契约读取接口。"""
    def do_GET(self):
        if self.path == "/health": self._send_json(health_payload()); return
        if self.path == "/contract": self._send_json(load_contract()); return
        self.send_error(404)
    def _send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_args): return

def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000); parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract(); assert contract["states"] and contract["invariants"]; print("基础检查通过"); return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()

if __name__ == "__main__": main()

