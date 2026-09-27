"""验证基础服务、领域契约与转诊 HTTP 接口端到端行为。"""
import json, threading, unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from service import Handler, SERVICE_ID, health_payload, load_contract, repository
from domain import ReferralRepository
import service


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),Handler); cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start(); cls.base_url=f"http://127.0.0.1:{cls.server.server_port}"
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
    def read_json(self,path):
        with urlopen(f"{self.base_url}{path}",timeout=2) as response:
            self.assertEqual(response.status,200); self.assertEqual(response.headers.get_content_type(),"application/json"); return json.load(response)
    def test_health_identity(self): self.assertEqual(self.read_json("/health"),health_payload())
    def test_contract_identity_and_rules(self):
        contract=self.read_json("/contract"); self.assertEqual(contract,load_contract()); self.assertEqual(contract["service_id"],SERVICE_ID); self.assertGreaterEqual(len(contract["invariants"]),3)
    def test_unknown_route_is_hidden(self):
        with self.assertRaises(HTTPError) as error: urlopen(f"{self.base_url}/unknown",timeout=2)
        self.assertEqual(error.exception.code,404); error.exception.close()


class ReferralHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类使用干净仓储
        service.repository = ReferralRepository()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        service.repository = repository

    def call(self, method, path, token=None, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token: headers["X-Actor-Token"] = token
        req = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=2) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            payload = json.load(exc)
            return exc.code, payload

    def submit_case(self, idem="http-1", **overrides):
        payload = {"patient_name": "福鼎老人王", "summary": "胸痛伴出汗",
                   "expected_arrival": "2026-09-28T07:00:00+08:00",
                   "phone_severity": "普通", "idem_key": idem}
        payload.update(overrides)
        status, resp = self.call("POST", "/cases", "doc", payload)
        self.assertEqual(status, 201, resp)
        return resp["case_id"]

    def command(self, case_id, token, command):
        return self.call("POST", f"/cases/{case_id}/commands", token, command)

    def test_requires_token(self):
        status, resp = self.call("GET", "/cases")
        self.assertEqual(status, 401)

    def test_submit_requires_source_role(self):
        status, resp = self.call("POST", "/cases", "m1",
                                 {"patient_name": "x", "summary": "s",
                                  "expected_arrival": "t"})
        self.assertEqual(status, 403)

    def test_full_referral_loop_with_idempotency_and_visibility(self):
        case_id = self.submit_case()

        # 两名管家几乎同时接单：只有一个成功，另一个 409
        status1, r1 = self.command(case_id, "m1", {"type": "accept", "idem_key": "http-acc"})
        status2, r2 = self.command(case_id, "m2", {"type": "accept", "idem_key": "http-acc2"})
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 409)

        # 平台重推同一接单消息：不推进第二次，返回首次事件
        status, r3 = self.command(case_id, "m1", {"type": "accept", "idem_key": "http-acc"})
        self.assertEqual(status, 200)
        self.assertTrue(r3["idempotent_replay"])
        self.assertEqual(r3["event"]["event_id"], r1["event"]["event_id"])

        # 见面发现危重，改急诊并保留原计划
        status, r = self.command(case_id, "m1", {
            "type": "reroute_emergency", "idem_key": "http-em",
            "signals": ["血压70/40", "口唇紫绀"], "notes": "比电话严重"})
        self.assertEqual(status, 200, r)
        self.assertEqual(r["status"], "院内处理中")
        self.assertEqual(r["case"]["track"], "急诊")
        self.assertEqual(r["case"]["original_plan"]["planned_track"], "门诊检查")

        # 电话补录同一危重事实：幂等命中
        status, r = self.command(case_id, "m1", {
            "type": "reroute_emergency", "idem_key": "http-em",
            "signals": ["血压70/40"]})
        self.assertTrue(r["idempotent_replay"])

        # 检查 → 出院 → 下转 → 一周内回访 → 闭环
        status, r = self.command(case_id, "spec",
                                 {"type": "order_exam", "idem_key": "http-ex",
                                  "item": "肌钙蛋白"})
        self.assertEqual(status, 200, r)
        exam_id = r["case"]["exams"][-1]["exam_id"]
        self.assertEqual(self.command(case_id, "spec",
            {"type": "record_exam_result", "idem_key": "http-exr",
             "exam_id": exam_id, "result": "升高处置后回落"})[0], 200)
        self.assertEqual(self.command(case_id, "spec",
            {"type": "discharge_plan", "idem_key": "http-dp",
             "plan": "稳定期下转康复", "follow_up_required": True,
             "pending_items": ["一周复查心电图"]})[0], 200)
        self.assertEqual(self.command(case_id, "town",
            {"type": "accept_downstream", "idem_key": "http-da",
             "contact": "店下卫生院"})[0], 200)

        # 市医院（管家）此时能看到随访待完成
        status, mgr = self.call("GET", f"/cases/{case_id}", "m1")
        self.assertEqual(mgr["follow_up"]["status"], "待回访")

        # 回访完成、待办仍在 → 不能闭环
        self.assertEqual(self.command(case_id, "town",
            {"type": "complete_follow_up", "idem_key": "http-fu",
             "result": "复查完成，心电图正常"})[0], 200)
        status, resp = self.command(case_id, "m1",
                                    {"type": "close", "idem_key": "http-cl"})
        self.assertEqual(status, 422)

        # 负责人追溯：交接链、升级、急诊改道、回流结果齐备
        status, trace = self.call("GET", f"/cases/{case_id}/trace", "lead")
        self.assertEqual(status, 200)
        kinds = [h["kind"] for h in trace["handoffs"]]
        self.assertEqual(kinds, ["接单", "下转接收", "回访回流"])
        self.assertIsNotNone(trace["emergency_reroute"])
        self.assertEqual(trace["original_plan_preserved"]["planned_track"],
                         "门诊检查")
        self.assertEqual([i["description"] for i in trace["pending_items"]],
                         ["一周复查心电图"])

        # 基层勾销复查待办（重复勾销幂等/404）后闭环
        item_id = trace["pending_items"][0]["item_id"]
        self.assertEqual(self.command(case_id, "town",
            {"type": "resolve_pending_item", "idem_key": "http-ri",
             "item_id": item_id, "note": "心电图已复查"})[0], 200)
        status, resp = self.command(case_id, "m1",
                                    {"type": "close", "idem_key": "http-cl2"})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["status"], "已闭环")

    def test_trace_forbidden_for_outsider(self):
        case_id = self.submit_case(idem="http-2")
        status, _ = self.call("GET", f"/cases/{case_id}/trace", "doc")
        self.assertEqual(status, 403)

    def test_list_scoped_by_role(self):
        mine = self.submit_case(idem="http-3")
        # 别的基层医生提交的单子：doc 看不到（提交人不同）
        self.submit_case(idem="http-4")
        status, resp = self.call("GET", "/cases", "village")
        self.assertEqual(status, 200)
        self.assertEqual(resp["cases"], [])

        # 管家可见调度池全部
        status, resp = self.call("GET", "/cases", "m3")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(resp["cases"]), 2)
        for c in resp["cases"]:
            self.assertIn("events", c)  # 管家职责内可见完整记录

    def test_source_views_are_field_limited(self):
        case_id = self.submit_case(idem="http-5")
        status, view = self.call("GET", f"/cases/{case_id}", "doc")
        self.assertEqual(status, 200)
        self.assertNotIn("patient_contact", view)
        self.assertNotIn("events", view)
        self.assertIn("status", view)

    def test_village_and_clinic_and_screening_enter_same_flow(self):
        for token in ("village", "clinic", "screen"):
            payload = {"patient_name": "筛查对象", "summary": "血压偏高",
                       "expected_arrival": "2026-09-29T08:00:00+08:00",
                       "idem_key": f"src-{token}"}
            status, resp = self.call("POST", "/cases", token, payload)
            self.assertEqual(status, 201, resp)
            self.assertEqual(resp["status"], "待联系")

    def test_unreachable_escalation_visible_in_trace(self):
        case_id = self.submit_case(idem="http-6")
        self.command(case_id, "m2", {"type": "accept", "idem_key": "a6"})
        self.command(case_id, "m2", {"type": "mark_unreachable",
                                     "idem_key": "u1", "note": "电话无人接"})
        self.command(case_id, "m2", {"type": "mark_unreachable",
                                     "idem_key": "u2", "note": "仍失联"})
        status, trace = self.call("GET", f"/cases/{case_id}/trace", "lead")
        self.assertEqual(status, 200)
        self.assertEqual(len(trace["escalations"]), 2)
        self.assertTrue(all(e["kind"] == "失联" for e in trace["escalations"]))

    def test_invalid_json_and_unknown_command(self):
        case_id = self.submit_case(idem="http-7")
        data = b"{not json"
        req = Request(f"{self.base_url}/cases/{case_id}/commands", data=data,
                      headers={"X-Actor-Token": "m1",
                               "Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=2)
        self.assertEqual(ctx.exception.code, 400)
        status, resp = self.command(case_id, "m1",
                                    {"type": "nope", "idem_key": "x"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
