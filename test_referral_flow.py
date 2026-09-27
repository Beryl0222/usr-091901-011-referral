"""端到端验证转诊闭环：状态推进、消息幂等、接单唯一、角色可见性与审计。"""

import json
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from referral_domain import CST, DomainError, ReferralCenter
from service import Handler

TODAY = datetime.now(CST).date().isoformat()


def call(base, method, path, actor=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if actor:
        headers["X-Actor-Id"] = actor
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        try:
            body = json.load(exc)
        except Exception:
            body = {}
        return exc.code, body


class ReferralFlowTest(unittest.TestCase):
    """通过 HTTP 接口走完上转、陪诊、下转与回访闭环。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.reset_center()
        self.seed()

    def call(self, method, path, actor=None, payload=None):
        return call(self.base, method, path, actor, payload)

    def ok(self, result, status=200):
        code, body = result
        self.assertEqual(code, status, body)
        return body

    def seed(self):
        self.ok(self.call("POST", "/actors", None,
                          {"id": "boss", "name": "陈主任", "role": "负责人"}), 201)
        for actor_id, name, role, org in [
            ("d1", "乡村医生老吴", "基层医生", "磻溪镇湖林村卫生室"),
            ("d2", "个体诊所林医生", "基层医生", "山前街道个体诊所"),
            ("exam1", "体检中心小周", "基层医生", "市体检中心"),
            ("s1", "王管家", "转诊管家", None),
            ("s2", "李管家", "转诊管家", None),
            ("s3", "赵管家", "转诊管家", None),
            ("s4", "高峰支援小郑", "转诊管家", None),
            ("s9", "未排班管家", "转诊管家", None),
            ("sp1", "心内科陈医生", "专科医生", None),
            ("hc1", "店下卫生院接收员", "接收卫生院", "店下中心卫生院"),
            ("hc2", "点头卫生院接收员", "接收卫生院", "点头镇卫生院"),
            ("p1", "周阿婆本人", "患者", None),
        ]:
            self.ok(self.call("POST", "/actors", "boss",
                              {"id": actor_id, "name": name, "role": role, "org": org}), 201)
        self.ok(self.call("POST", "/roster", "boss", {"date": TODAY, "entries": [
            {"steward": "s1", "shift": "专职"}, {"steward": "s2", "shift": "专职"},
            {"steward": "s3", "shift": "专职"}, {"steward": "s4", "shift": "高峰支援"}]}))

    def submit(self, doctor="d1", message_id="m-sub-1", **overrides):
        payload = {
            "message_id": message_id,
            "patient": {"name": "周阿婆", "phone": "13800000000", "age": 72,
                        "gender": "女", "address": "福鼎市磻溪镇湖林村",
                        "actor_id": "p1"},
            "condition_summary": "反复胸闷气促一周，加重半天，偏远乡镇天不亮出发",
            "expected_arrival": "2026-09-27T10:30:00+08:00",
            "source_type": "村医",
            "initial_triage": "急",
            "target_department": "心内科",
        }
        payload.update(overrides)
        return self.call("POST", "/referrals", doctor, payload)

    def drive_to_arrival(self, rid, steward="s1", prefix="m"):
        self.ok(self.call("POST", f"/referrals/{rid}/claim", steward,
                          {"message_id": f"{prefix}-claim-{rid}"}))
        self.ok(self.call("POST", f"/referrals/{rid}/contact", steward,
                          {"message_id": f"{prefix}-contact-{rid}",
                           "meeting_point": "市医院门诊大厅"}))
        return self.ok(self.call("POST", f"/referrals/{rid}/arrival", steward,
                                 {"message_id": f"{prefix}-arrival-{rid}",
                                  "triage_level": "重"}))

    def milestones_to_stable(self, rid, prefix="m"):
        self.ok(self.call("POST", f"/referrals/{rid}/milestones", "sp1",
                          {"message_id": f"{prefix}-exam-{rid}", "name": "检查"}))
        self.ok(self.call("POST", f"/referrals/{rid}/milestones", "sp1",
                          {"message_id": f"{prefix}-admit-{rid}", "name": "住院"}))
        return self.ok(self.call("POST", f"/referrals/{rid}/milestones", "sp1", {
            "message_id": f"{prefix}-plan-{rid}", "name": "出院方案",
            "discharge_plan": {"summary": "病情稳定，转回卫生院继续康复",
                               "target_center": "店下中心卫生院",
                               "follow_up_items": ["每日监测血压", "一周内复查心电图"],
                               "reexam_required": True}}))

    def drive_to_stable(self, rid, steward="s1", prefix="m"):
        self.drive_to_arrival(rid, steward, prefix)
        return self.milestones_to_stable(rid, prefix)

    # -- 完整闭环 --------------------------------------------------------------

    def test_full_closed_loop(self):
        rid = self.ok(self.submit(), 201)["id"]
        self.assertEqual(
            self.ok(self.call("POST", f"/referrals/{rid}/claim", "s1",
                              {"message_id": "m-claim"}))["steward_id"], "s1")
        self.assertEqual(
            self.ok(self.call("POST", f"/referrals/{rid}/contact", "s1",
                              {"message_id": "m-contact",
                               "meeting_point": "市医院门诊大厅"}))["status"], "待到院")
        body = self.ok(self.call("POST", f"/referrals/{rid}/arrival", "s1",
                                 {"message_id": "m-arrival", "triage_level": "重"}))
        self.assertEqual(body["status"], "院内处理中")
        self.assertEqual(body["arrival_triage"]["level"], "重")
        self.assertEqual(self.milestones_to_stable(rid)["status"], "待下转")
        self.assertEqual(
            self.ok(self.call("POST", f"/referrals/{rid}/accept", "hc1",
                              {"message_id": "m-accept"}))["status"], "随访中")
        body = self.ok(self.call("POST", f"/referrals/{rid}/follow-ups", "hc1",
                                 {"message_id": "m-fu", "reexam_done": True,
                                  "completed": True, "note": "复查已完成，恢复良好"}))
        self.assertEqual(body["status"], "已闭环")
        self.assertTrue(body["return_result"]["reexam_done"])
        # 基层医生能看到自己转诊患者的回流结果
        view = self.ok(self.call("GET", f"/referrals/{rid}", "d1"))
        self.assertEqual(view["return_result"]["outcome"], "已回流基层随访")
        # 负责人从患者记录看到每次交接、待办与回流结果
        timeline = self.ok(self.call("GET", f"/referrals/{rid}/timeline", "boss"))
        kinds = [event["kind"] for event in timeline["events"]]
        self.assertEqual(kinds, ["转诊申请已提交", "转诊管家接单", "已联系患者并确认接人",
                                 "到院再分级", "院内节点:检查", "院内节点:住院",
                                 "出院方案已定，进入待下转", "卫生院确认接收",
                                 "回访记录", "闭环"])
        handovers = [e["handover"] for e in timeline["events"] if e["handover"]]
        self.assertGreaterEqual(len(handovers), 4)
        self.assertTrue(all(item["done"] for item in timeline["open_items"]))
        self.assertEqual(timeline["return_result"]["outcome"], "已回流基层随访")

    # -- 幂等 ------------------------------------------------------------------

    def test_duplicate_messages_advance_only_once(self):
        first = self.ok(self.submit(), 201)
        replay = self.ok(self.submit(), 201)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(
            len(self.ok(self.call("GET", "/referrals", "boss"))["referrals"]), 1)
        rid = first["id"]
        # 消息编号不能挪作其他操作
        self.ok(self.call("POST", f"/referrals/{rid}/claim", "s1",
                          {"message_id": "m-sub-1"}), 409)
        self.drive_to_arrival(rid)
        # 到院消息重放不重复推进
        again = self.ok(self.call("POST", f"/referrals/{rid}/arrival", "s1",
                                  {"message_id": f"m-arrival-{rid}",
                                   "triage_level": "重"}))
        self.assertTrue(again["duplicate"])
        # 电话补录只入账一次
        note = {"message_id": "m-note-1", "channel": "电话",
                "note": "家属电话确认已服降压药"}
        self.ok(self.call("POST", f"/referrals/{rid}/notes", "s1", note))
        self.assertTrue(
            self.ok(self.call("POST", f"/referrals/{rid}/notes", "s1", note))["duplicate"])
        # 回访完成消息重放只闭环一次
        self.milestones_to_stable(rid)
        self.ok(self.call("POST", f"/referrals/{rid}/accept", "hc1",
                          {"message_id": "m-accept"}))
        follow_up = {"message_id": "m-fu", "reexam_done": True, "completed": True}
        self.ok(self.call("POST", f"/referrals/{rid}/follow-ups", "hc1", follow_up))
        self.assertTrue(self.ok(
            self.call("POST", f"/referrals/{rid}/follow-ups", "hc1", follow_up))["duplicate"])
        timeline = self.ok(self.call("GET", f"/referrals/{rid}/timeline", "boss"))
        kinds = [event["kind"] for event in timeline["events"]]
        self.assertEqual(kinds.count("到院再分级"), 1)
        self.assertEqual(kinds.count("补录"), 1)
        self.assertEqual(kinds.count("回访记录"), 1)
        view = self.ok(self.call("GET", f"/referrals/{rid}", "hc1"))
        self.assertEqual(len(view["follow_ups"]), 1)

    # -- 接单 ------------------------------------------------------------------

    def test_claim_requires_roster_and_is_unique(self):
        rid1 = self.ok(self.submit(message_id="m-a"), 201)["id"]
        rid2 = self.ok(self.submit(doctor="d2", message_id="m-b",
                                   source_type="个体诊所"), 201)["id"]
        rid3 = self.ok(self.submit(doctor="exam1", message_id="m-c",
                                   source_type="体检机构", initial_triage="高危"), 201)["id"]
        # 未排班管家不能接单
        self.ok(self.call("POST", f"/referrals/{rid1}/claim", "s9",
                          {"message_id": "m-c9"}), 403)
        # 高峰支援在排班内可接单
        self.ok(self.call("POST", f"/referrals/{rid3}/claim", "s4",
                          {"message_id": "m-c4"}))
        # 同一单只能一名管家承接
        self.ok(self.call("POST", f"/referrals/{rid1}/claim", "s1",
                          {"message_id": "m-c1"}))
        conflict = self.ok(self.call("POST", f"/referrals/{rid1}/claim", "s2",
                                     {"message_id": "m-c2"}), 409)
        self.assertIn("已被认领", conflict["error"])
        # 非管家角色不能接单
        self.ok(self.call("POST", f"/referrals/{rid2}/claim", "sp1",
                          {"message_id": "m-c5"}), 403)

    # -- 急诊改道 --------------------------------------------------------------

    def test_emergency_diverts_but_keeps_original_plan(self):
        rid = self.ok(self.submit(), 201)["id"]
        self.ok(self.call("POST", f"/referrals/{rid}/claim", "s1",
                          {"message_id": "m-claim"}))
        self.ok(self.call("POST", f"/referrals/{rid}/contact", "s1",
                          {"message_id": "m-contact", "meeting_point": "急诊门口"}))
        body = self.ok(self.call("POST", f"/referrals/{rid}/emergency", "s1",
                                 {"message_id": "m-emg",
                                  "signal": "见面时意识模糊、出冷汗"}))
        self.assertEqual(body["status"], "院内处理中")
        self.assertEqual(body["emergency"]["signal"], "见面时意识模糊、出冷汗")
        self.assertEqual(body["original_plan"]["expected_arrival"],
                         "2026-09-27T10:30:00+08:00")
        self.assertEqual(body["original_plan"]["target_department"], "心内科")
        # 急诊处置后原计划流程继续
        self.assertEqual(self.milestones_to_stable(rid)["status"], "待下转")
        timeline = self.ok(self.call("GET", f"/referrals/{rid}/timeline", "boss"))
        self.assertIn("危重改走急诊（原计划保留）",
                      [event["kind"] for event in timeline["events"]])

    # -- 角色可见性 --------------------------------------------------------------

    def test_role_scoped_visibility(self):
        rid = self.ok(self.submit(), 201)["id"]
        # 其他基层医生、未登记身份一律看不到
        self.ok(self.call("GET", f"/referrals/{rid}", "d2"), 404)
        self.ok(self.call("GET", f"/referrals/{rid}"), 401)
        self.ok(self.call("GET", f"/referrals/{rid}", "ghost"), 401)
        # 卫生院在下转前看不到；值班管家能看到待联系池；专科医生此时看不到
        self.ok(self.call("GET", f"/referrals/{rid}", "hc1"), 404)
        self.ok(self.call("GET", f"/referrals/{rid}", "s2"))
        self.ok(self.call("GET", f"/referrals/{rid}", "sp1"), 404)
        self.drive_to_stable(rid)
        # 已被 s1 承接，s2 不再可见；专科医生可见但不见联系方式与内部轨迹
        self.ok(self.call("GET", f"/referrals/{rid}", "s2"), 404)
        medical = self.ok(self.call("GET", f"/referrals/{rid}", "sp1"))
        self.assertNotIn("phone", medical["patient"])
        self.assertNotIn("events", medical)
        # 目标卫生院可见出院方案与患者联系方式，非目标卫生院不可见
        center_view = self.ok(self.call("GET", f"/referrals/{rid}", "hc1"))
        self.assertEqual(center_view["discharge_plan"]["target_center"], "店下中心卫生院")
        self.assertIn("phone", center_view["patient"])
        self.assertNotIn("events", center_view)
        self.ok(self.call("GET", f"/referrals/{rid}", "hc2"), 404)
        # 患者本人只见最小视图
        mine = self.ok(self.call("GET", f"/referrals/{rid}", "p1"))
        self.assertEqual(mine["status"], "待下转")
        self.assertNotIn("events", mine)
        # 列表按角色过滤
        self.assertEqual(
            self.ok(self.call("GET", "/referrals", "d2"))["referrals"], [])
        self.assertEqual(
            len(self.ok(self.call("GET", "/referrals", "boss"))["referrals"]), 1)

    # -- 非法流转 ----------------------------------------------------------------

    def test_invalid_transitions_are_rejected(self):
        rid = self.ok(self.submit(), 201)["id"]
        # 未接单不能确认接人/到院
        self.ok(self.call("POST", f"/referrals/{rid}/contact", "s1",
                          {"message_id": "m-x1", "meeting_point": "门口"}), 403)
        self.ok(self.call("POST", f"/referrals/{rid}/arrival", "s1",
                          {"message_id": "m-x2", "triage_level": "重"}), 403)
        self.ok(self.call("POST", f"/referrals/{rid}/claim", "s1",
                          {"message_id": "m-claim"}))
        # 未到院不能记院内节点；未下转不能接收/回访
        self.ok(self.call("POST", f"/referrals/{rid}/milestones", "sp1",
                          {"message_id": "m-x3", "name": "检查"}), 409)
        self.ok(self.call("POST", f"/referrals/{rid}/accept", "hc1",
                          {"message_id": "m-x4"}), 409)
        self.ok(self.call("POST", f"/referrals/{rid}/follow-ups", "hc1",
                          {"message_id": "m-x5"}), 409)
        self.ok(self.call("POST", f"/referrals/{rid}/contact", "s1",
                          {"message_id": "m-x6", "meeting_point": "门口"}))
        self.ok(self.call("POST", f"/referrals/{rid}/arrival", "s1",
                          {"message_id": "m-x7", "triage_level": "重"}))
        # 出院方案缺要素、未知节点
        self.ok(self.call("POST", f"/referrals/{rid}/milestones", "sp1",
                          {"message_id": "m-x8", "name": "出院方案"}), 400)
        self.ok(self.call("POST", f"/referrals/{rid}/milestones", "sp1",
                          {"message_id": "m-x9", "name": "换药"}), 400)
        # 非目标卫生院不能接收
        self.milestones_to_stable(rid)
        self.ok(self.call("POST", f"/referrals/{rid}/accept", "hc2",
                          {"message_id": "m-x10"}), 403)

    # -- 失联升级与待办 ----------------------------------------------------------

    def test_escalation_and_open_items_visible_to_director(self):
        rid = self.ok(self.submit(), 201)["id"]
        self.ok(self.call("POST", f"/referrals/{rid}/claim", "s1",
                          {"message_id": "m-claim"}))
        # 非承接管家不能升级
        self.ok(self.call("POST", f"/referrals/{rid}/escalations", "s2",
                          {"message_id": "m-e0", "reason": "失联"}), 403)
        body = self.ok(self.call("POST", f"/referrals/{rid}/escalations", "s1",
                                 {"message_id": "m-e1", "reason": "患者电话失联",
                                  "detail": "连续三次未接通"}))
        esc_id = body["escalations"][0]["id"]
        items = self.ok(self.call("GET", "/open-items", "boss"))["open_items"]
        self.assertTrue(any(i["referral_id"] == rid and "失联" in i["title"]
                            for i in items))
        # 非负责人看不到待办总览
        self.ok(self.call("GET", "/open-items", "s1"), 403)
        self.ok(self.call("POST", f"/referrals/{rid}/escalations/{esc_id}/resolve",
                          "boss", {"message_id": "m-e2",
                                   "note": "已联系村干部找到患者"}))
        items = self.ok(self.call("GET", "/open-items", "boss"))["open_items"]
        self.assertFalse(any(i["referral_id"] == rid and "失联" in i["title"]
                             for i in items))
        timeline = self.ok(self.call("GET", f"/referrals/{rid}/timeline", "boss"))
        self.assertTrue(timeline["escalations"][0]["resolved"])
        # 时间线对范围外人员隐藏
        self.ok(self.call("GET", f"/referrals/{rid}/timeline", "d1"), 404)


class ClaimRaceTest(unittest.TestCase):
    """并发接单只有一人成功，避免重复接单。"""

    def test_concurrent_claim_has_single_winner(self):
        center = ReferralCenter()
        center.register_actor(None, "boss", "陈主任", "负责人")
        center.register_actor("boss", "d1", "老吴", "基层医生", "湖林村卫生室")
        center.register_actor("boss", "s1", "王管家", "转诊管家")
        center.register_actor("boss", "s2", "李管家", "转诊管家")
        center.set_roster("boss", TODAY, [{"steward": "s1"}, {"steward": "s2"}])
        referral = center.submit_referral("d1", {
            "patient": {"name": "周阿婆", "phone": "13800000000"},
            "condition_summary": "胸闷气促",
            "expected_arrival": "2026-09-27T10:30:00+08:00",
            "source_type": "村医"}, "m-sub")
        outcomes = []

        def claim(steward, message_id):
            try:
                center.claim(steward, referral["id"], message_id)
                outcomes.append("ok")
            except DomainError as exc:
                outcomes.append(exc.status)

        threads = [threading.Thread(target=claim, args=("s1", "m-c1")),
                   threading.Thread(target=claim, args=("s2", "m-c2"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count(409), 1)
        self.assertIn(center.referrals[referral["id"]]["steward_id"], ("s1", "s2"))


class FollowUpWindowTest(unittest.TestCase):
    """下转接收后生成一周回访期限，逾期对负责人可见，回访完成后闭环。"""

    def test_follow_up_deadline_and_overdue(self):
        clock = [datetime(2026, 9, 27, 8, 0, tzinfo=CST)]
        center = ReferralCenter(now=lambda: clock[0])
        center.register_actor(None, "boss", "陈主任", "负责人")
        center.register_actor("boss", "d1", "老吴", "基层医生", "湖林村卫生室")
        center.register_actor("boss", "s1", "王管家", "转诊管家")
        center.register_actor("boss", "sp1", "陈医生", "专科医生")
        center.register_actor("boss", "hc1", "接收员", "接收卫生院", "店下中心卫生院")
        center.set_roster("boss", "2026-09-27", [{"steward": "s1"}])
        rid = center.submit_referral("d1", {
            "patient": {"name": "周阿婆", "phone": "13800000000"},
            "condition_summary": "胸闷气促",
            "expected_arrival": "2026-09-27T10:30:00+08:00",
            "source_type": "村医"}, "m1")["id"]
        center.claim("s1", rid, "m2")
        center.confirm_contact("s1", rid, {"meeting_point": "门诊大厅"}, "m3")
        center.register_arrival("s1", rid, {"triage_level": "重"}, "m4")
        center.record_milestone("sp1", rid, {"name": "出院方案", "discharge_plan": {
            "summary": "病情稳定", "target_center": "店下中心卫生院"}}, "m5")
        center.accept_downward("hc1", rid, {}, "m6")
        items = center.open_items("boss")
        follow = next(i for i in items if i["title"] == "一周内回访")
        self.assertEqual(follow["deadline"],
                         (clock[0] + timedelta(days=7)).isoformat())
        self.assertFalse(follow["overdue"])
        clock[0] += timedelta(days=8)
        follow = next(i for i in center.open_items("boss") if i["title"] == "一周内回访")
        self.assertTrue(follow["overdue"])
        # 电话补录回访完成后闭环，待办清零
        center.record_follow_up("hc1", rid, {
            "channel": "电话", "reexam_done": True, "completed": True}, "m7")
        self.assertEqual(center.referrals[rid]["status"], "已闭环")
        self.assertEqual(center.open_items("boss"), [])


if __name__ == "__main__":
    unittest.main()
