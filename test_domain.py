"""领域核心测试：状态机、幂等、抢单冲突、急诊改道保留原计划、
下转回访、失联升级、闭环约束与角色最小可见。"""

import unittest

from domain import (
    ReferralCase, ReferralRepository, DomainError, ConcurrentAssignment,
    S_PENDING_CONTACT, S_PENDING_ARRIVAL, S_IN_HOSPITAL,
    S_PENDING_DOWN, S_FOLLOW_UP, S_CLOSED,
    ROLE_CASE_MANAGER, ROLE_SPECIALIST, ROLE_RECEIVING_FACILITY,
    ROLE_SUPERVISOR, ROLE_PRIVATE_CLINIC, ROLE_HEALTH_SCREENING,
)


def actor(actor_id, role):
    return {"id": actor_id, "role": role}


M1 = actor("M-01", ROLE_CASE_MANAGER)
M2 = actor("M-02", ROLE_CASE_MANAGER)
SPEC = actor("D-SPEC", ROLE_SPECIALIST)
TOWN = actor("FW-TM", ROLE_RECEIVING_FACILITY)
LEAD = actor("U-LEAD", ROLE_SUPERVISOR)


def submit(repo=None, submitter=None, **overrides):
    repo = repo or ReferralRepository()
    submitter = submitter or actor("D-TOWN", "基层医生")
    payload = {
        "patient_name": "张三",
        "summary": "胸痛2小时",
        "expected_arrival": "2026-09-27T07:30:00+08:00",
        "phone_severity": "普通",
        "idem_key": "msg-0001",
    }
    payload.update(overrides)
    return repo, repo.submit(payload, submitter)


def cmd(case, ctype, who, **fields):
    fields.setdefault("idem_key", f"k-{ctype}-{len(case.events)}")
    fields["type"] = ctype
    return case.apply(fields, who)


class SubmitTest(unittest.TestCase):
    def test_submit_creates_pending_contact(self):
        _, case = submit()
        self.assertEqual(case.status, S_PENDING_CONTACT)
        self.assertIsNone(case.manager_id)
        self.assertEqual(case.original_plan["planned_track"], "门诊检查")

    def test_submit_required_fields(self):
        repo = ReferralRepository()
        with self.assertRaises(DomainError):
            repo.submit({"summary": "x"}, actor("x", "村医"))

    def test_village_clinic_screening_sources_accepted(self):
        for role in ("村医", ROLE_PRIVATE_CLINIC, ROLE_HEALTH_SCREENING):
            _, case = submit(submitter=actor("src", role))
            self.assertEqual(case.source_role, role)
            self.assertEqual(case.status, S_PENDING_CONTACT)


class AcceptanceTest(unittest.TestCase):
    def test_first_manager_wins(self):
        _, case = submit()
        cmd(case, "accept", M1)
        self.assertEqual(case.status, S_PENDING_ARRIVAL)
        self.assertEqual(case.manager_id, "M-01")
        with self.assertRaises(ConcurrentAssignment) as ctx:
            cmd(case, "accept", M2)
        self.assertEqual(ctx.exception.code, 409)
        # 状态与负责人不变
        self.assertEqual(case.manager_id, "M-01")

    def test_reassign_keeps_history(self):
        _, case = submit()
        cmd(case, "accept", M1)
        case.apply({"type": "reassign", "idem_key": "r1",
                    "to_manager_id": "M-02"}, LEAD)
        self.assertEqual(case.manager_id, "M-02")
        trace = case.trace()
        kinds = [h["kind"] for h in trace["handoffs"]]
        self.assertIn("接单", kinds)
        self.assertIn("改派", kinds)

    def test_role_cannot_accept(self):
        _, case = submit()
        with self.assertRaises(DomainError) as ctx:
            case.apply({"type": "accept", "idem_key": "x"}, SPEC)
        self.assertEqual(ctx.exception.code, 403)


class RetriageAndEmergencyTest(unittest.TestCase):
    def test_meet_retriage_sets_planned_track(self):
        _, case = submit()
        cmd(case, "accept", M1)
        cmd(case, "meet_and_retriage", M1, severity="稳定",
            notes="症状缓解", track="门诊检查")
        self.assertEqual(case.status, S_IN_HOSPITAL)
        self.assertEqual(case.track, "门诊检查")
        # 电话描述的原计划保留
        self.assertEqual(case.original_plan["phone_severity"], "普通")
        self.assertEqual(case.original_plan["expected_arrival"],
                         "2026-09-27T07:30:00+08:00")

    def test_emergency_reroute_preserves_original_plan(self):
        _, case = submit(phone_severity="普通")
        cmd(case, "accept", M1)
        # 见面发现实际病情危重：直接改急诊，未走 meet_retriage
        cmd(case, "reroute_emergency", M1,
            signals=["口唇紫绀", "血压70/40"], notes="比电话描述严重")
        self.assertEqual(case.track, "急诊")
        self.assertEqual(case.status, S_IN_HOSPITAL)
        self.assertIsNotNone(case.emergency_reroute)
        # 原计划完整保留，可查可溯
        self.assertEqual(case.original_plan["phone_severity"], "普通")
        self.assertEqual(case.original_plan["planned_track"], "门诊检查")
        self.assertEqual(case.original_plan["expected_arrival"],
                         "2026-09-27T07:30:00+08:00")
        trace = case.trace()
        self.assertIsNotNone(trace["emergency_reroute"])
        self.assertEqual(trace["original_plan_preserved"]["planned_track"],
                         "门诊检查")

    def test_emergency_requires_signals(self):
        _, case = submit()
        cmd(case, "accept", M1)
        with self.assertRaises(DomainError):
            cmd(case, "reroute_emergency", M1, signals=[])

    def test_only_assigned_manager_retriages(self):
        _, case = submit()
        cmd(case, "accept", M1)
        with self.assertRaises(DomainError) as ctx:
            case.apply({"type": "meet_and_retriage", "idem_key": "x",
                        "severity": "稳定"}, M2)
        self.assertEqual(ctx.exception.code, 403)


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_message_advances_once(self):
        _, case = submit()
        command = {"type": "accept", "idem_key": "platform-msg-77"}
        first = case.apply(command, M1)
        second = case.apply(dict(command), M1)
        self.assertIs(first, second)
        self.assertEqual(len(case.events), 2)  # 提交 + 接单，无重复事件
        self.assertEqual(case.manager_id, "M-01")

    def test_phone_backfill_same_key_no_double_progress(self):
        _, case = submit()
        cmd(case, "accept", M1)
        command = {"type": "reroute_emergency", "idem_key": "call-9",
                   "signals": ["意识模糊"]}
        first = case.apply(command, M1)
        # 平台重推 + 电话补录同一事实
        replay = case.apply({"type": "reroute_emergency",
                             "idem_key": "call-9", "signals": ["意识模糊"]}, M1)
        self.assertIs(first, replay)
        self.assertEqual(len(case.events), 3)
        self.assertEqual(case.unreachable_count, 0)

    def test_same_key_different_case_is_independent(self):
        repo = ReferralRepository()
        p1 = {"patient_name": "甲", "summary": "s", "expected_arrival": "t",
              "idem_key": "shared"}
        p2 = {"patient_name": "乙", "summary": "s", "expected_arrival": "t",
              "idem_key": "shared"}
        c1 = repo.submit(p1, actor("d1", "基层医生"))
        c2 = repo.submit(p2, actor("d2", "基层医生"))
        self.assertNotEqual(c1.id, c2.id)
        e1 = c1.apply({"type": "accept", "idem_key": "shared"}, M1)
        e2 = c2.apply({"type": "accept", "idem_key": "shared"}, M2)
        self.assertIsNot(e1, e2)


class HospitalFlowTest(unittest.TestCase):
    def _in_hospital(self, track="门诊检查"):
        _, case = submit()
        cmd(case, "accept", M1)
        if track == "急诊":
            cmd(case, "reroute_emergency", M1, signals=["休克"])
        else:
            cmd(case, "meet_and_retriage", M1, severity="稳定", track=track)
        return case

    def test_exam_lifecycle_and_blocks_discharge(self):
        case = self._in_hospital()
        cmd(case, "order_exam", SPEC, item="心电图")
        with self.assertRaises(DomainError):
            cmd(case, "discharge_plan", SPEC, plan="带药回家")
        exam_id = list(case.exams.values())[0]["exam_id"]
        cmd(case, "record_exam_result", SPEC, exam_id=exam_id, result="正常")
        self.assertEqual(list(case.exams.values())[0]["status"], "已完成")
        self.assertNotIn(exam_id, case.pending_items)

    def test_duplicate_exam_result_rejected(self):
        case = self._in_hospital()
        cmd(case, "order_exam", M1, item="CT")
        exam_id = list(case.exams.values())[0]["exam_id"]
        cmd(case, "record_exam_result", M1, exam_id=exam_id, result="阴性")
        with self.assertRaises(DomainError):
            cmd(case, "record_exam_result", M1, exam_id=exam_id, result="阴性")

    def test_admit_then_discharge(self):
        case = self._in_hospital(track="急诊")
        cmd(case, "admit", SPEC, ward="心内科", reason="心梗可能")
        self.assertEqual(case.admission["ward"], "心内科")
        cmd(case, "discharge_plan", SPEC, plan="稳定期下转",
            follow_up_required=True, pending_items=["一周后复查肝功能"])
        self.assertEqual(case.status, S_PENDING_DOWN)
        self.assertIn("一周后复查肝功能",
                      [i["description"] for i in case.pending_items.values()])

    def test_double_admit_rejected(self):
        case = self._in_hospital()
        cmd(case, "admit", M1, ward="观察区")
        with self.assertRaises(DomainError):
            cmd(case, "admit", M1, ward="观察区")


class DownstreamFollowUpTest(unittest.TestCase):
    def _ready_for_downstream(self):
        _, case = submit()
        cmd(case, "accept", M1)
        cmd(case, "meet_and_retriage", M1, severity="稳定")
        cmd(case, "discharge_plan", M1, plan="下转康复",
            follow_up_required=True)
        return case

    def test_downstream_accept_starts_follow_up_clock(self):
        case = self._ready_for_downstream()
        cmd(case, "accept_downstream", TOWN,
            **{"contact": "王院长",
               "follow_up_deadline": "2026-10-04T12:00:00+08:00"})
        self.assertEqual(case.status, S_FOLLOW_UP)
        self.assertEqual(case.receiving_facility_id, "FW-TM")
        self.assertEqual(case.follow_up["status"], "待回访")
        self.assertEqual(case.follow_up["deadline"],
                         "2026-10-04T12:00:00+08:00")

    def test_follow_up_within_window_closes(self):
        case = self._ready_for_downstream()
        cmd(case, "accept_downstream", TOWN,
            **{"follow_up_deadline": "2026-10-04T12:00:00+08:00"})
        cmd(case, "complete_follow_up", TOWN, result="复查正常，已停药")
        self.assertEqual(case.follow_up["status"], "已完成")
        cmd(case, "close", M1)
        self.assertEqual(case.status, S_CLOSED)

    def test_late_follow_up_needs_ack(self):
        case = self._ready_for_downstream()
        cmd(case, "accept_downstream", TOWN,
            **{"follow_up_deadline": "2026-10-01T08:00:00+08:00"})
        late = "2026-10-08T09:00:00+08:00"
        with self.assertRaises(DomainError) as ctx:
            case.apply({"type": "complete_follow_up", "idem_key": "late1",
                        "result": "已回访"}, TOWN, occurred_at=late)
        self.assertEqual(ctx.exception.code, 409)
        # 负责人知情后补录可推进
        case.apply({"type": "complete_follow_up", "idem_key": "late2",
                    "result": "已回访", "late_ack": True}, TOWN,
                   occurred_at=late)
        self.assertEqual(case.follow_up["status"], "已完成")

    def test_cannot_close_without_follow_up(self):
        case = self._ready_for_downstream()
        cmd(case, "accept_downstream", TOWN)
        with self.assertRaises(DomainError):
            cmd(case, "close", M1)

    def test_cannot_close_with_pending_items(self):
        _, case = submit()
        cmd(case, "accept", M1)
        cmd(case, "meet_and_retriage", M1, severity="稳定")
        cmd(case, "discharge_plan", M1, plan="下转康复",
            follow_up_required=False, pending_items=["复查肝功能"])
        cmd(case, "accept_downstream", TOWN)
        with self.assertRaises(DomainError):
            cmd(case, "close", M1)

    def test_default_deadline_is_seven_days(self):
        case = self._ready_for_downstream()
        case.apply({"type": "accept_downstream", "idem_key": "acc",
                    "contact": ""}, TOWN,
                   occurred_at="2026-09-27T10:00:00+08:00")
        self.assertEqual(case.follow_up["deadline"],
                         "2026-10-04T10:00:00+08:00")


class UnreachableEscalationTest(unittest.TestCase):
    def test_two_unreachable_then_supervisor_level(self):
        _, case = submit()
        cmd(case, "accept", M1)
        cmd(case, "mark_unreachable", M1, note="电话无人接")
        cmd(case, "mark_unreachable", M1, note="再次失联")
        self.assertEqual(case.unreachable_count, 2)
        cmd(case, "escalate", M1, note="两次失联，升级负责人")
        self.assertEqual(case.escalations[-1]["level"], 3)
        trace = case.trace()
        self.assertEqual(len(trace["escalations"]), 3)
        # 闭环后升级全部标记解决
        cmd(case, "meet_and_retriage", M1, severity="稳定")
        cmd(case, "discharge_plan", M1, plan="无需下转",
            follow_up_required=False)
        # 待下转状态、无待办：失联患者找回后可直接闭环
        cmd(case, "close", M1, reason="失联患者找回，流程结束")
        self.assertTrue(all(e["resolved"] for e in case.escalations))

    def test_escalation_recorded_in_trace(self):
        _, case = submit()
        cmd(case, "accept", M1)
        cmd(case, "mark_unreachable", M1, note="村医协助找人")
        trace = case.trace()
        self.assertEqual(trace["escalations"][0]["kind"], "失联")
        self.assertFalse(trace["escalations"][0]["resolved"])


class VisibilityTest(unittest.TestCase):
    def _case_with_full_flow(self):
        repo = ReferralRepository()
        payload = {"patient_name": "李四", "patient_contact": "13800000000",
                   "summary": "脑梗康复", "expected_arrival": "t",
                   "phone_severity": "普通", "idem_key": "s1",
                   "source_org": "嵛山卫生院"}
        case = repo.submit(payload, actor("D-TOWN", "基层医生"))
        case.apply({"type": "accept", "idem_key": "a"}, M1)
        case.apply({"type": "meet_and_retriage", "idem_key": "m",
                    "severity": "稳定"}, M1)
        case.apply({"type": "discharge_plan", "idem_key": "d",
                    "plan": "康复下转", "follow_up_required": True}, M1)
        case.apply({"type": "accept_downstream", "idem_key": "da"}, TOWN)
        return case

    def test_submitter_sees_limited_fields(self):
        case = self._case_with_full_flow()
        view = case.visible_fields("基层医生")
        self.assertIn("status", view)
        self.assertIn("follow_up", view)
        # 病情联系方式等内部信息不对基层来源展开
        self.assertNotIn("patient_contact", view)
        self.assertNotIn("events", view)
        self.assertNotIn("escalations", view)

    def test_village_doctor_boundary(self):
        case = self._case_with_full_flow()
        # 村医不是本单提交人时看不到任何字段（由列表层过滤）；
        # visible_fields 对其他基层角色同样最小裁剪
        other_village = case.visible_fields("村医")
        self.assertNotIn("patient_contact", other_village)
        self.assertNotIn("events", other_village)

    def test_specialist_sees_clinical_not_events(self):
        case = self._case_with_full_flow()
        view = case.visible_fields(ROLE_SPECIALIST)
        self.assertIn("exams", view)
        self.assertIn("discharge", view)
        self.assertNotIn("events", view)
        self.assertNotIn("follow_up", view)

    def test_receiving_facility_sees_contact_and_followup(self):
        case = self._case_with_full_flow()
        view = case.visible_fields(ROLE_RECEIVING_FACILITY)
        self.assertIn("patient_contact", view)
        self.assertIn("follow_up", view)
        self.assertNotIn("events", view)
        self.assertNotIn("original_plan", view)

    def test_supervisor_sees_everything_including_events(self):
        case = self._case_with_full_flow()
        view = case.visible_fields(ROLE_SUPERVISOR)
        self.assertIn("events", view)
        self.assertIn("escalations", view)
        self.assertIn("patient_contact", view)


class TraceTest(unittest.TestCase):
    def test_full_trace_handoffs_and_outcome(self):
        _, case = submit()
        cmd(case, "accept", M1)
        cmd(case, "meet_and_retriage", M1, severity="稳定")
        cmd(case, "discharge_plan", M1, plan="下转")
        cmd(case, "accept_downstream", TOWN)
        cmd(case, "complete_follow_up", TOWN, result="康复良好")
        cmd(case, "close", M1)
        trace = case.trace()
        kinds = [h["kind"] for h in trace["handoffs"]]
        self.assertEqual(kinds, ["接单", "下转接收", "回访回流"])
        self.assertEqual(trace["status"], S_CLOSED)
        self.assertEqual(trace["closed_reason"], "转诊流程完成")
        self.assertEqual(trace["event_count"], 7)
        self.assertEqual(trace["pending_items"], [])


class ConcurrencyTest(unittest.TestCase):
    def test_repository_serializes_competing_accepts(self):
        repo, case = submit()
        _, ev1, replay1 = repo.issue(case.id, {"type": "accept",
                                               "idem_key": "t1"}, M1)
        self.assertEqual(ev1["manager_id"], "M-01")
        self.assertFalse(replay1)
        # 第二个人的接单请求被拒绝，负责人始终是 M-01
        with self.assertRaises(ConcurrentAssignment):
            repo.issue(case.id, {"type": "accept", "idem_key": "t2"}, M2)
        self.assertEqual(repo.get(case.id).manager_id, "M-01")

    def test_repository_idempotent_replay_flag(self):
        repo, case = submit()
        _, ev1, first = repo.issue(case.id, {"type": "accept",
                                             "idem_key": "same"}, M1)
        _, ev2, replay = repo.issue(case.id, {"type": "accept",
                                              "idem_key": "same"}, M1)
        self.assertFalse(first)
        self.assertTrue(replay)
        self.assertIs(ev1, ev2)


if __name__ == "__main__":
    unittest.main()
