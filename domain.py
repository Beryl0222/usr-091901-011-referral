"""县域双向转诊闭环的领域核心。

以追加式事件流承载一条转诊单的全部事实：基层上转、管家接人、见面再分级、
危重改走急诊（保留原计划）、检查/住院/出院方案、下转接收、一周内回访、
失联升级直至闭环。所有外部消息携带幂等键，重复提交只推进一次。
"""

import datetime as _dt
import itertools
import threading
import uuid
from collections import OrderedDict

# ---------- 领域常量 ----------

# 状态与领域契约 states 保持一致
S_PENDING_CONTACT = "待联系"      # 已上转，等待管家接单
S_PENDING_ARRIVAL = "待到院"      # 管家已接单，等待患者到院见面
S_IN_HOSPITAL = "院内处理中"      # 到院再分级后进入检查/住院处置
S_PENDING_DOWN = "待下转"         # 出院方案已出，等待基层接收
S_FOLLOW_UP = "随访中"            # 基层已接收，等待一周内回访
S_CLOSED = "已闭环"

# 院内处置通道：门诊检查 与 危重改道急诊；原计划始终保留
TRACK_PLANNED = "门诊检查"
TRACK_EMERGENCY = "急诊"

FOLLOW_UP_WINDOW_DAYS = 7
ESCALATION_LIMIT = 2

ROLE_REFERRING_DOCTOR = "基层医生"
ROLE_VILLAGE_DOCTOR = "村医"
ROLE_PRIVATE_CLINIC = "个体诊所"
ROLE_HEALTH_SCREENING = "体检高危人群"
ROLE_CASE_MANAGER = "转诊管家"
ROLE_SPECIALIST = "专科医生"
ROLE_RECEIVING_FACILITY = "接收卫生院"
ROLE_SUPERVISOR = "负责人"

# 可发起上转的基层来源
SOURCE_ROLES = (ROLE_REFERRING_DOCTOR, ROLE_VILLAGE_DOCTOR,
                ROLE_PRIVATE_CLINIC, ROLE_HEALTH_SCREENING)

# 命令 -> 允许执行的角色
COMMAND_ROLES = {
    "accept": {ROLE_CASE_MANAGER},
    "meet_and_retriage": {ROLE_CASE_MANAGER},
    "reroute_emergency": {ROLE_CASE_MANAGER, ROLE_SPECIALIST},
    "order_exam": {ROLE_CASE_MANAGER, ROLE_SPECIALIST},
    "record_exam_result": {ROLE_CASE_MANAGER, ROLE_SPECIALIST},
    "admit": {ROLE_CASE_MANAGER, ROLE_SPECIALIST},
    "discharge_plan": {ROLE_CASE_MANAGER, ROLE_SPECIALIST},
    "accept_downstream": {ROLE_RECEIVING_FACILITY},
    "complete_follow_up": {ROLE_RECEIVING_FACILITY, ROLE_CASE_MANAGER},
    "resolve_pending_item": {ROLE_CASE_MANAGER, ROLE_RECEIVING_FACILITY,
                             ROLE_SUPERVISOR},
    "close": {ROLE_CASE_MANAGER, ROLE_SUPERVISOR},
    "reassign": {ROLE_SUPERVISOR},
    "mark_unreachable": {ROLE_CASE_MANAGER},
    "escalate": {ROLE_CASE_MANAGER},
}

_seq = itertools.count(1)


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(value):
    return _dt.datetime.fromisoformat(value)


class DomainError(Exception):
    """拒绝原因以中文消息直接返回给调用方。"""

    def __init__(self, message, code=422):
        super().__init__(message)
        self.code = code


class ConcurrentAssignment(DomainError):
    """同一转诊单被两名管家同时接单时抛出。"""

    def __init__(self, manager_id):
        super().__init__(f"该转诊单已由管家 {manager_id} 接单", code=409)


# ---------- 聚合根 ----------

class ReferralCase:
    """一条转诊单：事件流 + 由事件折叠出的当前状态。"""

    def __init__(self, case_id=None):
        self.id = case_id or _new_id("CASE")
        self._events = []
        self._idempotency = {}   # idem_key -> 幂等命中的事件

    # ===== 事件回放 =====

    @classmethod
    def replay(cls, case_id, events):
        case = cls(case_id)
        for event in events:
            case._fold(event, persist=False)
        return case

    @property
    def events(self):
        return list(self._events)

    def _fold(self, event, persist):
        etype = event["type"]
        handler = getattr(self, f"_ev_{etype}", None)
        if handler:
            handler(event)
        if persist:
            self._events.append(event)
            self._idempotency[event["idem_key"]] = event

    def _ev_referral_submitted(self, e):
        self.status = S_PENDING_CONTACT
        self.patient_name = e["patient_name"]
        self.patient_contact = e.get("patient_contact")
        self.source_role = e["source_role"]
        self.source_org = e.get("source_org")
        self.submitter_id = e["actor_id"]
        self.summary = e["summary"]
        self.phone_severity = e.get("phone_severity", "")
        self.expected_arrival = e["expected_arrival"]
        self.original_plan = {
            "expected_arrival": e["expected_arrival"],
            "phone_severity": e.get("phone_severity", ""),
            "planned_track": TRACK_PLANNED,
        }
        self.manager_id = None
        self.track = None
        self.meet_assessment = None
        self.emergency_reroute = None
        self.exams = OrderedDict()
        self.admission = None
        self.discharge = None
        self.receiving_facility_id = None
        self.follow_up = None
        self.unreachable_count = 0
        self.last_contact_at = e["occurred_at"]
        self.escalations = []
        self.pending_items = OrderedDict()
        self.closed_reason = None

    def _ev_accepted(self, e):
        self.status = S_PENDING_ARRIVAL
        self.manager_id = e["manager_id"]

    def _ev_met_retriaged(self, e):
        self.status = S_IN_HOSPITAL
        self.track = e["track"]
        self.meet_assessment = {
            "severity": e["severity"], "notes": e["notes"],
            "actor_id": e["actor_id"], "occurred_at": e["occurred_at"],
        }
        self.last_contact_at = e["occurred_at"]

    def _ev_emergency_rerouted(self, e):
        self.status = S_IN_HOSPITAL
        self.track = TRACK_EMERGENCY
        self.emergency_reroute = {
            "signals": e["signals"], "notes": e["notes"],
            "actor_id": e["actor_id"], "occurred_at": e["occurred_at"],
        }
        self.last_contact_at = e["occurred_at"]

    def _ev_exam_ordered(self, e):
        self.exams[e["exam_id"]] = {
            "exam_id": e["exam_id"], "item": e["item"], "status": "待检查",
            "ordered_by": e["actor_id"], "ordered_at": e["occurred_at"],
        }
        self.pending_items[e["exam_id"]] = {
            "item_id": e["exam_id"], "description": f"检查：{e['item']}",
            "status": "待完成", "created_at": e["occurred_at"]}

    def _ev_exam_result_recorded(self, e):
        exam = self.exams[e["exam_id"]]
        exam["status"] = "已完成"
        exam["result"] = e["result"]
        exam["completed_at"] = e["occurred_at"]
        self.pending_items.pop(e["exam_id"], None)

    def _ev_admitted(self, e):
        self.admission = {
            "ward": e["ward"], "reason": e.get("reason", ""),
            "actor_id": e["actor_id"], "occurred_at": e["occurred_at"],
        }

    def _ev_discharge_planned(self, e):
        self.status = S_PENDING_DOWN
        self.discharge = {
            "plan": e["plan"], "follow_up_required": e["follow_up_required"],
            "actor_id": e["actor_id"], "occurred_at": e["occurred_at"],
        }
        for desc in e.get("pending_items", []):
            item_id = _new_id("ITEM")
            self.pending_items[item_id] = {
                "item_id": item_id, "description": desc,
                "status": "待完成", "created_at": e["occurred_at"]}

    def _ev_pending_item_resolved(self, e):
        self.pending_items.pop(e["item_id"], None)

    def _ev_downstream_accepted(self, e):
        self.status = S_FOLLOW_UP
        self.receiving_facility_id = e["facility_id"]
        self.downstream_accept = {
            "facility_id": e["facility_id"], "contact": e.get("contact", ""),
            "actor_id": e["actor_id"], "occurred_at": e["occurred_at"],
        }
        self.follow_up = {
            "required": self.discharge.get("follow_up_required", True) if self.discharge else True,
            "deadline": e["follow_up_deadline"],
            "status": "待回访",
        }
        self.last_contact_at = e["occurred_at"]

    def _ev_follow_up_completed(self, e):
        if self.follow_up is None:
            self.follow_up = {"required": True, "deadline": None, "status": "已完成"}
        self.follow_up["status"] = "已完成"
        self.follow_up["result"] = e["result"]
        self.follow_up["completed_at"] = e["occurred_at"]
        self.last_contact_at = e["occurred_at"]

    def _ev_unreachable_marked(self, e):
        self.unreachable_count += 1
        self.escalations.append({
            "level": self.unreachable_count, "kind": "失联",
            "note": e.get("note", ""), "actor_id": e["actor_id"],
            "occurred_at": e["occurred_at"], "resolved": False,
        })

    def _ev_escalated(self, e):
        self.escalations.append({
            "level": e["level"], "kind": e.get("kind", "业务升级"),
            "note": e.get("note", ""), "actor_id": e["actor_id"],
            "occurred_at": e["occurred_at"], "resolved": False,
        })

    def _ev_reassigned(self, e):
        self.manager_id = e["to_manager_id"]

    def _ev_closed(self, e):
        self.status = S_CLOSED
        self.closed_reason = e.get("reason", "转诊流程完成")
        self.pending_items.clear()
        for esc in self.escalations:
            esc["resolved"] = True

    # ===== 命令处理 =====

    def apply(self, command, actor, occurred_at=None):
        """执行一条命令。command 为 dict，须含 type 与 idem_key。

        同一 idem_key 重放时返回首次产生的事件，不再推进状态。
        """
        idem_key = command.get("idem_key")
        if not idem_key:
            raise DomainError("缺少幂等键 idem_key", code=400)
        hit = self._idempotency.get(idem_key)
        if hit is not None:
            return hit

        ctype = command.get("type")
        method = getattr(self, f"cmd_{ctype}", None)
        if method is None:
            raise DomainError(f"未知命令：{ctype}", code=400)
        allowed = COMMAND_ROLES.get(ctype, set())
        if actor["role"] not in allowed:
            raise DomainError(f"角色 {actor['role']} 无权执行 {ctype}", code=403)
        event = method(command, actor, occurred_at or now_iso())
        event.update({
            "event_id": _new_id("EVT"), "case_id": self.id,
            "actor_id": actor["id"], "actor_role": actor["role"],
            "occurred_at": occurred_at or now_iso(), "idem_key": idem_key,
        })
        self._fold(event, persist=True)
        return event

    def _event(self, etype, **payload):
        payload["type"] = etype
        return payload

    def _require_status(self, *statuses):
        if self.status not in statuses:
            raise DomainError(f"当前状态为{self.status}，不允许该操作")

    def cmd_accept(self, c, a, ts):
        if self.status != S_PENDING_CONTACT:
            if self.manager_id:
                raise ConcurrentAssignment(self.manager_id)
            raise DomainError(f"当前状态为{self.status}，不允许该操作")
        return self._event("accepted", manager_id=a["id"])

    def cmd_reassign(self, c, a, ts):
        if not self.manager_id:
            raise DomainError("尚未接单，无需改派")
        to = c.get("to_manager_id")
        if not to:
            raise DomainError("缺少改派目标 to_manager_id", code=400)
        return self._event("reassigned", to_manager_id=to)

    def cmd_meet_and_retriage(self, c, a, ts):
        self._require_status(S_PENDING_ARRIVAL)
        if a["id"] != self.manager_id:
            raise DomainError("仅当前负责管家可登记见面再分级", code=403)
        severity = c.get("severity")
        if not severity:
            raise DomainError("缺少见面后分级 severity", code=400)
        return self._event(
            "met_retriaged", severity=severity, notes=c.get("notes", ""),
            track=c.get("track", TRACK_PLANNED))

    def cmd_reroute_emergency(self, c, a, ts):
        # 见面发现危重信号：可在待到院直接改急诊，也可在院内处理中升级改道
        self._require_status(S_PENDING_ARRIVAL, S_IN_HOSPITAL)
        if not c.get("signals"):
            raise DomainError("必须记录危重信号 signals", code=400)
        return self._event(
            "emergency_rerouted", signals=list(c["signals"]),
            notes=c.get("notes", ""))

    def cmd_order_exam(self, c, a, ts):
        self._require_status(S_IN_HOSPITAL)
        if not c.get("item"):
            raise DomainError("缺少检查项目 item", code=400)
        return self._event("exam_ordered", exam_id=_new_id("EXAM"),
                           item=c["item"])

    def cmd_record_exam_result(self, c, a, ts):
        self._require_status(S_IN_HOSPITAL)
        exam = self.exams.get(c.get("exam_id"))
        if exam is None:
            raise DomainError("检查单不存在", code=404)
        if exam["status"] == "已完成":
            raise DomainError("检查结果已记录")
        return self._event("exam_result_recorded", exam_id=exam["exam_id"],
                           result=c.get("result", ""))

    def cmd_admit(self, c, a, ts):
        self._require_status(S_IN_HOSPITAL)
        if self.admission is not None:
            raise DomainError("已办理住院")
        if not c.get("ward"):
            raise DomainError("缺少住院病区 ward", code=400)
        return self._event("admitted", ward=c["ward"],
                           reason=c.get("reason", ""))

    def cmd_discharge_plan(self, c, a, ts):
        self._require_status(S_IN_HOSPITAL)
        pending = [oid for oid, ex in self.exams.items() if ex["status"] != "已完成"]
        if pending:
            raise DomainError("尚有检查未完成，不能出具出院方案")
        if not c.get("plan"):
            raise DomainError("缺少出院方案 plan", code=400)
        return self._event(
            "discharge_planned", plan=c["plan"],
            follow_up_required=bool(c.get("follow_up_required", True)),
            pending_items=list(c.get("pending_items", [])))

    def cmd_accept_downstream(self, c, a, ts):
        self._require_status(S_PENDING_DOWN)
        deadline = c.get("follow_up_deadline")
        if not deadline:
            # 默认接收后一周内完成回访
            base = parse_iso(ts)
            deadline = (base + _dt.timedelta(days=FOLLOW_UP_WINDOW_DAYS)).isoformat(timespec="seconds")
        return self._event(
            "downstream_accepted", facility_id=a["id"],
            contact=c.get("contact", ""), follow_up_deadline=deadline)

    def cmd_complete_follow_up(self, c, a, ts):
        self._require_status(S_FOLLOW_UP)
        if not self.follow_up or not self.follow_up.get("required", True):
            raise DomainError("该患者无需回访")
        if self.follow_up["status"] == "已完成":
            raise DomainError("回访已完成")
        within, reason = self._follow_up_within_window(ts)
        if not within and not c.get("late_ack"):
            raise DomainError(reason, code=409)
        return self._event("follow_up_completed", result=c.get("result", ""))

    def _follow_up_within_window(self, ts):
        deadline = self.follow_up and self.follow_up.get("deadline")
        if not deadline:
            return True, ""
        if parse_iso(ts) <= parse_iso(deadline):
            return True, ""
        return False, f"已超过一周回访期限（{deadline}），需负责人知情后补录（late_ack）"

    def cmd_mark_unreachable(self, c, a, ts):
        self._require_status(S_PENDING_ARRIVAL, S_IN_HOSPITAL,
                             S_PENDING_DOWN, S_FOLLOW_UP)
        return self._event("unreachable_marked", note=c.get("note", ""))

    def cmd_escalate(self, c, a, ts):
        self._require_status(S_PENDING_ARRIVAL, S_IN_HOSPITAL,
                             S_PENDING_DOWN, S_FOLLOW_UP)
        if self.unreachable_count >= ESCALATION_LIMIT:
            level = ESCALATION_LIMIT + 1
        else:
            level = self.unreachable_count + 1
        return self._event("escalated", level=level,
                           kind=c.get("kind", "业务升级"), note=c.get("note", ""))

    def cmd_resolve_pending_item(self, c, a, ts):
        """勾销出院方案带回的待办（如基层复查完成、药械到位）。"""
        self._require_status(S_PENDING_DOWN, S_FOLLOW_UP)
        item = self.pending_items.get(c.get("item_id"))
        if item is None:
            raise DomainError("待办不存在或已完成", code=404)
        return self._event("pending_item_resolved", item_id=item["item_id"],
                           note=c.get("note", ""))

    def cmd_close(self, c, a, ts):
        self._require_status(S_PENDING_DOWN, S_FOLLOW_UP, S_IN_HOSPITAL)
        if self.status == S_FOLLOW_UP and self.follow_up and \
                self.follow_up.get("required", True) and \
                self.follow_up.get("status") != "已完成":
            raise DomainError("一周内回访未完成，不能闭环；如确认无需回访请先记录结果")
        open_items = [item["description"] for item in self.pending_items.values()]
        if open_items:
            raise DomainError("仍有未完成事项：" + "；".join(open_items))
        return self._event("closed", reason=c.get("reason", "转诊流程完成"))

    # ===== 对外视图 =====

    def snapshot(self):
        """完整视图，供负责人与存储使用。"""
        return {
            "case_id": self.id,
            "status": self.status,
            "patient_name": self.patient_name,
            "patient_contact": self.patient_contact,
            "source_role": self.source_role,
            "source_org": self.source_org,
            "submitter_id": self.submitter_id,
            "summary": self.summary,
            "phone_severity": self.phone_severity,
            "expected_arrival": self.expected_arrival,
            "original_plan": self.original_plan,
            "manager_id": self.manager_id,
            "track": self.track,
            "meet_assessment": self.meet_assessment,
            "emergency_reroute": self.emergency_reroute,
            "exams": list(self.exams.values()),
            "admission": self.admission,
            "discharge": self.discharge,
            "receiving_facility_id": self.receiving_facility_id,
            "follow_up": self.follow_up,
            "unreachable_count": self.unreachable_count,
            "escalations": list(self.escalations),
            "pending_items": list(self.pending_items.values()),
            "closed_reason": self.closed_reason,
            "events": self.events,
        }

    def visible_fields(self, role):
        """按角色返回职责所需字段；越权字段不出现而非置空。"""
        full = self.snapshot()
        keep = ["case_id", "status"]
        if role in SOURCE_ROLES:
            keep += ["patient_name", "source_org", "summary",
                     "expected_arrival", "track", "manager_id",
                     "receiving_facility_id", "follow_up", "closed_reason"]
        elif role == ROLE_CASE_MANAGER:
            keep = list(full.keys())
        elif role == ROLE_SPECIALIST:
            keep += ["patient_name", "summary", "phone_severity",
                     "original_plan", "track", "meet_assessment",
                     "emergency_reroute", "exams", "admission",
                     "discharge", "pending_items", "manager_id"]
        elif role == ROLE_RECEIVING_FACILITY:
            keep += ["patient_name", "patient_contact", "summary",
                     "discharge", "follow_up", "pending_items",
                     "escalations"]
        elif role == ROLE_SUPERVISOR:
            keep = list(full.keys())
        else:
            keep = ["case_id", "status"]
        return {k: full[k] for k in keep if k in full}

    def trace(self):
        """负责人追溯视图：每次交接、未完成事项、失联升级、回流结果。"""
        handoffs = []
        for e in self._events:
            if e["type"] == "accepted":
                handoffs.append({"at": e["occurred_at"], "from": "调度池",
                                 "to": e["manager_id"], "kind": "接单"})
            elif e["type"] == "reassigned":
                handoffs.append({"at": e["occurred_at"],
                                 "from": e["actor_id"],
                                 "to": e["to_manager_id"], "kind": "改派"})
            elif e["type"] == "downstream_accepted":
                handoffs.append({"at": e["occurred_at"],
                                 "from": self.manager_id or "市医院",
                                 "to": e["facility_id"], "kind": "下转接收"})
            elif e["type"] == "follow_up_completed":
                handoffs.append({"at": e["occurred_at"],
                                 "from": e["actor_id"],
                                 "to": self.manager_id or "会诊转诊中心",
                                 "kind": "回访回流"})
        return {
            "case_id": self.id,
            "patient_name": self.patient_name,
            "status": self.status,
            "handoffs": handoffs,
            "pending_items": list(self.pending_items.values()),
            "escalations": list(self.escalations),
            "emergency_reroute": self.emergency_reroute,
            "original_plan_preserved": self.original_plan,
            "follow_up": self.follow_up,
            "closed_reason": self.closed_reason,
            "event_count": len(self._events),
        }


# ---------- 仓储（内存实现，接口可替换为持久化实现） ----------

class ReferralRepository:
    def __init__(self):
        self._cases = {}
        self._lock = threading.RLock()

    def submit(self, payload, actor, occurred_at=None):
        for field in ("patient_name", "summary", "expected_arrival"):
            if not payload.get(field):
                raise DomainError(f"缺少必填字段：{field}", code=400)
        with self._lock:
            case = ReferralCase()
            ts = occurred_at or now_iso()
            event = {
                "type": "referral_submitted",
                "event_id": _new_id("EVT"), "case_id": case.id,
                "actor_id": actor["id"], "actor_role": actor["role"],
                "occurred_at": ts,
                "idem_key": payload.get("idem_key") or _new_id("IDEM"),
                "patient_name": payload["patient_name"],
                "patient_contact": payload.get("patient_contact"),
                "source_role": actor["role"],
                "source_org": payload.get("source_org"),
                "summary": payload["summary"],
                "phone_severity": payload.get("phone_severity", ""),
                "expected_arrival": payload["expected_arrival"],
            }
            case._fold(event, persist=True)
            self._cases[case.id] = case
            return case

    def get(self, case_id):
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise DomainError("转诊单不存在", code=404)
            return case

    def issue(self, case_id, command, actor, occurred_at=None):
        """在聚合级锁内执行命令，保证两名管家不会同时接单。"""
        with self._lock:
            case = self.get(case_id)
            idem_key = command.get("idem_key")
            if not idem_key:
                raise DomainError("缺少幂等键 idem_key", code=400)
            # 先查幂等命中：重复平台消息/电话补录直接返回首次事件
            hit = case._idempotency.get(idem_key)
            if hit is not None:
                return case, hit, True
            if command.get("type") == "accept" and case.status == S_PENDING_CONTACT:
                event = case.apply(command, actor, occurred_at)
                return case, event, False
            event = case.apply(command, actor, occurred_at)
            return case, event, False

    def list_cases(self):
        with self._lock:
            return list(self._cases.values())
