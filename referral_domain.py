"""县域双向转诊闭环的领域核心。

基层上转、到院再分级、院内陪诊、稳定期下转与一周内回访共用同一状态机；
平台消息与电话补录一律按外部消息编号幂等推进，患者信息按角色最小可见。
"""

import copy
import threading
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

ROLES = ("基层医生", "转诊管家", "专科医生", "患者", "接收卫生院", "负责人")
SOURCE_TYPES = ("村医", "个体诊所", "体检机构", "卫生院")
STATES = ("待联系", "待到院", "院内处理中", "待下转", "随访中", "已闭环")
MILESTONES = ("检查", "住院", "出院方案")
SHIFTS = ("专职", "高峰支援")
CHANNELS = ("平台", "电话")
FOLLOW_UP_WINDOW = timedelta(days=7)


class DomainError(Exception):
    """业务规则冲突，status 对应 HTTP 语义。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _need(payload, *fields):
    missing = [field for field in fields if not payload.get(field)]
    if missing:
        raise DomainError("缺少字段:" + ",".join(missing))


class ReferralCenter:
    """会诊转诊中心的内存领域服务，写操作串行化以保证接单唯一与消息幂等。"""

    def __init__(self, now=None):
        self._now = now or (lambda: datetime.now(CST))
        self._lock = threading.RLock()
        self.actors = {}
        self.referrals = {}
        self.roster = {}
        self.messages = {}

    # -- 基础 ----------------------------------------------------------------

    def _ts(self):
        return self._now().isoformat()

    def _actor(self, actor_id):
        actor = self.actors.get(actor_id or "")
        if actor is None:
            raise DomainError("未登记的参与者", 401)
        return actor

    def _require(self, actor_id, *roles):
        actor = self._actor(actor_id)
        if actor["role"] not in roles:
            raise DomainError("该角色无权执行此操作", 403)
        return actor

    def _get(self, referral_id):
        referral = self.referrals.get(referral_id or "")
        if referral is None:
            raise DomainError("未找到该转诊记录", 404)
        return referral

    def _once(self, message_id, actor_id, action, do):
        """同一外部消息只推进一次：重放时返回首次受理结果并标记 duplicate。"""
        if not message_id:
            raise DomainError("缺少外部消息编号")
        with self._lock:
            receipt = self.messages.get(message_id)
            if receipt is not None:
                if receipt["actor_id"] != actor_id or receipt["action"] != action:
                    raise DomainError("消息编号与原始请求不一致", 409)
                result = copy.deepcopy(receipt["result"])
                result["duplicate"] = True
                return result
            result = do()
            self.messages[message_id] = {
                "actor_id": actor_id, "action": action,
                "result": copy.deepcopy(result), "at": self._ts(),
            }
            return result

    def _event(self, referral, kind, actor_id, detail=None, handover=None):
        referral["events"].append({
            "seq": len(referral["events"]) + 1, "at": self._ts(), "kind": kind,
            "actor_id": actor_id, "detail": detail or {}, "handover": handover,
        })
        referral["updated_at"] = referral["events"][-1]["at"]

    def _add_item(self, referral, title, deadline=None):
        referral["open_items"].append({
            "id": f"item-{len(referral['open_items']) + 1}", "title": title,
            "created_at": self._ts(), "deadline": deadline, "done": False, "done_at": None,
        })

    def _close_item(self, referral, title):
        for item in referral["open_items"]:
            if item["title"] == title and not item["done"]:
                item["done"] = True
                item["done_at"] = self._ts()

    # -- 参与者与排班 ----------------------------------------------------------

    def register_actor(self, by, actor_id, name, role, org=None):
        """登记参与者；系统为空时首个登记者必须是负责人，之后仅负责人可登记。"""
        if role not in ROLES:
            raise DomainError("未知角色")
        if not actor_id or not name:
            raise DomainError("缺少参与者编号或姓名")
        with self._lock:
            if self.actors:
                registrar = self.actors.get(by or "")
                if registrar is None:
                    raise DomainError("未登记的参与者", 401)
                if registrar["role"] != "负责人":
                    raise DomainError("仅负责人可登记参与者", 403)
            elif role != "负责人":
                raise DomainError("首位参与者必须是负责人")
            if actor_id in self.actors:
                raise DomainError("参与者已存在", 409)
            actor = {"id": actor_id, "name": name, "role": role, "org": org}
            self.actors[actor_id] = actor
            return dict(actor)

    def list_actors(self, by):
        self._require(by, "负责人")
        return [dict(actor) for actor in self.actors.values()]

    def set_roster(self, by, date, entries):
        """为某日排班（专职或高峰支援），只有排班内的管家才能接单。"""
        self._require(by, "负责人")
        if not date:
            raise DomainError("缺少字段:date")
        with self._lock:
            day = self.roster.setdefault(date, {})
            for entry in entries or []:
                steward = self.actors.get(entry.get("steward") or "")
                if steward is None or steward["role"] != "转诊管家":
                    raise DomainError(f"排班对象不是转诊管家:{entry.get('steward')}")
                shift = entry.get("shift", "专职")
                if shift not in SHIFTS:
                    raise DomainError("未知班次类型")
                day[steward["id"]] = shift
        return self.get_roster(by, date)

    def get_roster(self, by, date=None):
        self._require(by, "负责人", "转诊管家")
        date = date or self._now().date().isoformat()
        return {"date": date, "entries": [
            {"steward": sid, "name": self.actors[sid]["name"], "shift": shift}
            for sid, shift in self.roster.get(date, {}).items() if sid in self.actors]}

    # -- 上转与院内陪诊 --------------------------------------------------------

    def submit_referral(self, by, payload, message_id):
        """基层医生（村医/个体诊所/体检机构/卫生院）提交上转申请。"""
        actor = self._require(by, "基层医生")
        _need(payload, "condition_summary", "expected_arrival", "source_type")
        if payload["source_type"] not in SOURCE_TYPES:
            raise DomainError("未知转诊来源")
        patient = payload.get("patient") or {}
        if not patient.get("name") or not patient.get("phone"):
            raise DomainError("缺少患者姓名或联系方式")

        def do():
            ts = self._ts()
            referral_id = f"R{self._now():%Y%m%d}-{len(self.referrals) + 1:04d}"
            referral = {
                "id": referral_id, "status": "待联系",
                "patient": {
                    "name": patient["name"], "phone": patient["phone"],
                    "age": patient.get("age"), "gender": patient.get("gender"),
                    "address": patient.get("address"), "actor_id": patient.get("actor_id"),
                },
                "source": {"type": payload["source_type"], "doctor_id": actor["id"],
                           "doctor_name": actor["name"], "org": actor.get("org")},
                "initial_triage": payload.get("initial_triage"),
                "arrival_triage": None,
                "original_plan": {
                    "condition_summary": payload["condition_summary"],
                    "expected_arrival": payload["expected_arrival"],
                    "target_department": payload.get("target_department"),
                    "source_type": payload["source_type"],
                    "submitted_by": actor["id"], "created_at": ts,
                },
                "emergency": None, "steward_id": None, "pickup_plan": None,
                "milestones": [], "discharge_plan": None, "downward_acceptance": None,
                "follow_ups": [], "open_items": [], "escalations": [], "events": [],
                "return_result": None, "created_at": ts, "updated_at": ts,
            }
            self.referrals[referral_id] = referral
            self._add_item(referral, "联系患者确认接人")
            self._event(referral, "转诊申请已提交", by,
                        {"source_type": payload["source_type"],
                         "expected_arrival": payload["expected_arrival"]},
                        handover={"from": payload["source_type"], "to": "转诊中心"})
            return self._view(actor, referral)

        return self._once(message_id, by, "submit", do)

    def claim(self, by, referral_id, message_id):
        """当日排班内的转诊管家接单；同一转诊单只能由一名管家承接。"""
        actor = self._require(by, "转诊管家")
        today = self._now().date().isoformat()
        shift = self.roster.get(today, {}).get(by)
        if shift is None:
            raise DomainError("不在今日排班内，不能接单", 403)

        def do():
            referral = self._get(referral_id)
            if referral["status"] != "待联系":
                raise DomainError("该转诊单当前不可接单", 409)
            if referral["steward_id"] is not None:
                raise DomainError("该转诊单已被认领", 409)
            referral["steward_id"] = by
            self._event(referral, "转诊管家接单", by, {"shift": shift},
                        handover={"from": "转诊中心池", "to": f"转诊管家:{actor['name']}"})
            return self._view(actor, referral)

        return self._once(message_id, by, f"claim:{referral_id}", do)

    def confirm_contact(self, by, referral_id, payload, message_id):
        """承接管家确认联系患者并安排接人：待联系 → 待到院。"""
        actor = self._require(by, "转诊管家")
        _need(payload, "meeting_point")

        def do():
            referral = self._get(referral_id)
            if referral["steward_id"] != by:
                raise DomainError("仅承接该单的管家可确认接人", 403)
            if referral["status"] != "待联系":
                raise DomainError("当前状态不可确认接人", 409)
            referral["pickup_plan"] = {
                "meeting_point": payload["meeting_point"],
                "planned_arrival": payload.get("planned_arrival")
                or referral["original_plan"]["expected_arrival"],
                "note": payload.get("note", ""),
            }
            self._close_item(referral, "联系患者确认接人")
            self._add_item(referral, "陪同到院再分级")
            referral["status"] = "待到院"
            self._event(referral, "已联系患者并确认接人", by, dict(referral["pickup_plan"]))
            return self._view(actor, referral)

        return self._once(message_id, by, f"contact:{referral_id}", do)

    def register_arrival(self, by, referral_id, payload, message_id):
        """患者到院，见面后重新分级：待到院 → 院内处理中。原始转诊计划保留。"""
        actor = self._require(by, "转诊管家", "专科医生")
        _need(payload, "triage_level")

        def do():
            referral = self._get(referral_id)
            if actor["role"] == "转诊管家" and referral["steward_id"] != by:
                raise DomainError("仅承接该单的管家可登记到院", 403)
            if referral["status"] != "待到院":
                raise DomainError("当前状态不可登记到院", 409)
            referral["arrival_triage"] = {
                "level": payload["triage_level"], "note": payload.get("note", ""),
                "at": self._ts(), "by": by,
            }
            self._close_item(referral, "陪同到院再分级")
            self._add_item(referral, "完成院内检查与收治")
            referral["status"] = "院内处理中"
            self._event(referral, "到院再分级", by,
                        {"arrival_triage": dict(referral["arrival_triage"]),
                         "original_plan_kept": True},
                        handover={"from": "转诊管家", "to": "专科医生"})
            return self._view(actor, referral)

        return self._once(message_id, by, f"arrival:{referral_id}", do)

    def divert_emergency(self, by, referral_id, payload, message_id):
        """发现危重信号立即改走急诊；原始转诊计划完整保留，处置后流程继续。"""
        actor = self._require(by, "转诊管家", "专科医生")
        _need(payload, "signal")

        def do():
            referral = self._get(referral_id)
            if actor["role"] == "转诊管家" and referral["steward_id"] != by:
                raise DomainError("仅承接该单的管家可改走急诊", 403)
            if referral["status"] not in ("待到院", "院内处理中"):
                raise DomainError("当前状态不可改走急诊", 409)
            referral["emergency"] = {
                "signal": payload["signal"], "note": payload.get("note", ""),
                "at": self._ts(), "by": by,
            }
            if referral["status"] == "待到院":
                self._close_item(referral, "陪同到院再分级")
                self._add_item(referral, "完成院内检查与收治")
                referral["status"] = "院内处理中"
            self._event(referral, "危重改走急诊（原计划保留）", by,
                        {"emergency": dict(referral["emergency"]),
                         "original_plan_kept": True})
            return self._view(actor, referral)

        return self._once(message_id, by, f"emergency:{referral_id}", do)

    def record_milestone(self, by, referral_id, payload, message_id):
        """记录院内节点（检查/住院/出院方案）；出院方案确定后进入待下转。"""
        actor = self._require(by, "专科医生", "转诊管家")
        _need(payload, "name")
        name = payload["name"]
        if name not in MILESTONES:
            raise DomainError("未知院内节点")

        def do():
            referral = self._get(referral_id)
            if actor["role"] == "转诊管家" and referral["steward_id"] != by:
                raise DomainError("仅承接该单的管家可补录院内节点", 403)
            if referral["status"] != "院内处理中":
                raise DomainError("当前状态不可记录院内节点", 409)
            entry = {"name": name, "at": self._ts(), "by": by,
                     "note": payload.get("note", "")}
            referral["milestones"].append(entry)
            if name == "出院方案":
                plan = payload.get("discharge_plan") or {}
                _need(plan, "summary", "target_center")
                referral["discharge_plan"] = {
                    "summary": plan["summary"], "target_center": plan["target_center"],
                    "follow_up_items": plan.get("follow_up_items", []),
                    "reexam_required": bool(plan.get("reexam_required", True)),
                    "decided_by": by, "decided_at": self._ts(),
                }
                self._close_item(referral, "完成院内检查与收治")
                self._add_item(referral, "基层卫生院确认接收")
                referral["status"] = "待下转"
                self._event(referral, "出院方案已定，进入待下转", by,
                            {"discharge_plan": dict(referral["discharge_plan"])},
                            handover={"from": "专科医生", "to": "接收卫生院"})
            else:
                self._event(referral, f"院内节点:{name}", by, dict(entry))
            return self._view(actor, referral)

        return self._once(message_id, by, f"milestone:{referral_id}:{name}", do)

    # -- 下转与回访 ------------------------------------------------------------

    def accept_downward(self, by, referral_id, payload, message_id):
        """接收卫生院确认接收：待下转 → 随访中，并生成一周回访期限。"""
        actor = self._require(by, "接收卫生院")

        def do():
            referral = self._get(referral_id)
            if referral["status"] != "待下转":
                raise DomainError("当前状态不可确认接收", 409)
            plan = referral["discharge_plan"] or {}
            if plan.get("target_center") != actor.get("org"):
                raise DomainError("该转诊单未下转至本机构", 403)
            referral["downward_acceptance"] = {
                "at": self._ts(), "by": by, "note": payload.get("note", ""),
            }
            self._close_item(referral, "基层卫生院确认接收")
            deadline = (self._now() + FOLLOW_UP_WINDOW).isoformat()
            self._add_item(referral, "一周内回访", deadline=deadline)
            referral["status"] = "随访中"
            self._event(referral, "卫生院确认接收", by,
                        dict(referral["downward_acceptance"]),
                        handover={"from": "市医院", "to": actor.get("org") or "接收卫生院"})
            return self._view(actor, referral)

        return self._once(message_id, by, f"accept:{referral_id}", do)

    def record_follow_up(self, by, referral_id, payload, message_id):
        """记录回访（含复查是否完成）；completed 时闭环并写入回流结果。"""
        actor = self._require(by, "接收卫生院", "转诊管家")
        channel = payload.get("channel", "平台")
        if channel not in CHANNELS:
            raise DomainError("未知回访渠道")

        def do():
            referral = self._get(referral_id)
            if referral["status"] != "随访中":
                raise DomainError("当前状态不可记录回访", 409)
            plan = referral["discharge_plan"] or {}
            if actor["role"] == "接收卫生院" and actor.get("org") != plan.get("target_center"):
                raise DomainError("非本机构接收的转诊单", 403)
            if actor["role"] == "转诊管家" and referral["steward_id"] != by:
                raise DomainError("仅承接该单的管家可记录回访", 403)
            record = {"at": self._ts(), "by": by, "channel": channel,
                      "reexam_done": bool(payload.get("reexam_done")),
                      "note": payload.get("note", "")}
            referral["follow_ups"].append(record)
            self._event(referral, "回访记录", by, dict(record))
            if payload.get("completed"):
                self._close_item(referral, "一周内回访")
                referral["status"] = "已闭环"
                referral["return_result"] = {
                    "outcome": payload.get("outcome") or "已回流基层随访",
                    "reexam_done": record["reexam_done"],
                    "at": self._ts(), "recorded_by": by,
                }
                self._event(referral, "闭环", by,
                            {"return_result": dict(referral["return_result"])},
                            handover={"from": "市医院", "to": referral["source"]["type"]})
            return self._view(actor, referral)

        return self._once(message_id, by, f"follow-up:{referral_id}", do)

    # -- 失联升级与补录 --------------------------------------------------------

    def escalate(self, by, referral_id, payload, message_id):
        """承接管家或负责人发起失联升级，生成负责人待办。"""
        actor = self._require(by, "转诊管家", "负责人")
        _need(payload, "reason")

        def do():
            referral = self._get(referral_id)
            if actor["role"] == "转诊管家" and referral["steward_id"] != by:
                raise DomainError("仅承接该单的管家可发起升级", 403)
            escalation = {
                "id": f"esc-{len(referral['escalations']) + 1}", "at": self._ts(),
                "by": by, "reason": payload["reason"],
                "detail": payload.get("detail", ""),
                "resolved": False, "resolved_at": None, "resolution": None,
            }
            referral["escalations"].append(escalation)
            self._add_item(referral, f"跟进失联升级:{escalation['id']}")
            self._event(referral, "失联升级", by, {"escalation": dict(escalation)})
            return self._view(actor, referral)

        return self._once(message_id, by, f"escalate:{referral_id}", do)

    def resolve_escalation(self, by, referral_id, escalation_id, payload, message_id):
        """负责人处理失联升级并关闭对应待办。"""
        actor = self._require(by, "负责人")

        def do():
            referral = self._get(referral_id)
            escalation = next((e for e in referral["escalations"]
                               if e["id"] == escalation_id), None)
            if escalation is None:
                raise DomainError("未找到该升级记录", 404)
            if escalation["resolved"]:
                raise DomainError("该升级已处理", 409)
            escalation["resolved"] = True
            escalation["resolved_at"] = self._ts()
            escalation["resolution"] = payload.get("note", "")
            self._close_item(referral, f"跟进失联升级:{escalation_id}")
            self._event(referral, "失联升级已处理", by, {"escalation": dict(escalation)})
            return self._view(actor, referral)

        return self._once(message_id, by, f"resolve:{referral_id}:{escalation_id}", do)

    def record_note(self, by, referral_id, payload, message_id):
        """平台或电话补录：只记录不推进状态，同一补录编号只入账一次。"""
        actor = self._actor(by)
        _need(payload, "note")
        channel = payload.get("channel", "平台")
        if channel not in CHANNELS:
            raise DomainError("未知补录渠道")

        def do():
            referral = self._get(referral_id)
            if not self._involved(actor, referral):
                raise DomainError("未参与该患者照护，不能补录", 403)
            self._event(referral, "补录", by,
                        {"channel": channel, "note": payload["note"]})
            return {"referral_id": referral["id"], "status": referral["status"],
                    "recorded": True}

        return self._once(message_id, by, f"note:{referral_id}", do)

    # -- 查询与角色视图 --------------------------------------------------------

    def view_for(self, by, referral_id):
        return self._view(self._actor(by), self._get(referral_id))

    def list_for(self, by, status=None):
        actor = self._actor(by)
        views = []
        for referral in self.referrals.values():
            view = self._view(actor, referral, raise_missing=False)
            if view is not None and (status is None or referral["status"] == status):
                views.append(view)
        views.sort(key=lambda view: (view["created_at"], view["id"]))
        return views

    def timeline_for(self, by, referral_id):
        """负责人（及承接管家）从患者记录看到每次交接、待办、升级与回流结果。"""
        actor = self._actor(by)
        referral = self._get(referral_id)
        if actor["role"] == "负责人" or (
                actor["role"] == "转诊管家" and referral["steward_id"] == by):
            return {
                "referral_id": referral["id"], "status": referral["status"],
                "events": copy.deepcopy(referral["events"]),
                "open_items": copy.deepcopy(referral["open_items"]),
                "escalations": copy.deepcopy(referral["escalations"]),
                "return_result": copy.deepcopy(referral["return_result"]),
            }
        raise DomainError("未找到该转诊记录", 404)

    def open_items(self, by):
        """负责人视角的全部未完成事项，带逾期标记。"""
        self._require(by, "负责人")
        now = self._ts()
        items = []
        for referral in self.referrals.values():
            for item in referral["open_items"]:
                if item["done"]:
                    continue
                items.append({**item, "referral_id": referral["id"],
                              "patient_name": referral["patient"]["name"],
                              "overdue": bool(item["deadline"] and item["deadline"] < now)})
        return items

    def _involved(self, actor, referral):
        role = actor["role"]
        if role == "负责人":
            return True
        if role == "转诊管家":
            return referral["steward_id"] == actor["id"]
        if role == "基层医生":
            return referral["source"]["doctor_id"] == actor["id"]
        if role == "专科医生":
            return referral["status"] in ("院内处理中", "待下转")
        if role == "接收卫生院":
            plan = referral["discharge_plan"] or {}
            return (plan.get("target_center") == actor.get("org")
                    and referral["status"] in ("待下转", "随访中", "已闭环"))
        return False

    def _view(self, actor, referral, raise_missing=True):
        """按角色给出职责所需的最小视图；范围外的记录一律当作不存在。"""
        role = actor["role"]
        if role == "负责人":
            return self._full_view(referral)
        if role == "转诊管家":
            if referral["steward_id"] == actor["id"] or (
                    referral["steward_id"] is None and referral["status"] == "待联系"):
                return self._full_view(referral)
        elif role == "基层医生":
            if referral["source"]["doctor_id"] == actor["id"]:
                return self._grassroots_view(referral)
        elif role == "专科医生":
            if referral["status"] in ("院内处理中", "待下转"):
                return self._medical_view(referral)
        elif role == "接收卫生院":
            plan = referral["discharge_plan"] or {}
            if (plan.get("target_center") == actor.get("org")
                    and referral["status"] in ("待下转", "随访中", "已闭环")):
                return self._center_view(referral)
        elif role == "患者":
            if referral["patient"].get("actor_id") == actor["id"]:
                return self._patient_view(referral)
        if raise_missing:
            raise DomainError("未找到该转诊记录", 404)
        return None

    def _steward_name(self, referral):
        steward = self.actors.get(referral["steward_id"] or "")
        return steward["name"] if steward else None

    def _full_view(self, referral):
        view = copy.deepcopy(referral)
        view["steward_name"] = self._steward_name(referral)
        return view

    def _grassroots_view(self, referral):
        plan = referral["discharge_plan"] or {}
        return {
            "id": referral["id"], "status": referral["status"],
            "patient_name": referral["patient"]["name"],
            "source_type": referral["source"]["type"],
            "expected_arrival": referral["original_plan"]["expected_arrival"],
            "steward_name": self._steward_name(referral),
            "initial_triage": referral["initial_triage"],
            "arrival_triage": copy.deepcopy(referral["arrival_triage"]),
            "discharge_summary": plan.get("summary"),
            "follow_ups": [{"at": f["at"], "reexam_done": f["reexam_done"],
                            "note": f["note"]} for f in referral["follow_ups"]],
            "return_result": copy.deepcopy(referral["return_result"]),
            "created_at": referral["created_at"],
        }

    def _medical_view(self, referral):
        patient = referral["patient"]
        return {
            "id": referral["id"], "status": referral["status"],
            "patient": {"name": patient["name"], "age": patient["age"],
                        "gender": patient["gender"]},
            "condition_summary": referral["original_plan"]["condition_summary"],
            "target_department": referral["original_plan"]["target_department"],
            "initial_triage": referral["initial_triage"],
            "arrival_triage": copy.deepcopy(referral["arrival_triage"]),
            "emergency": copy.deepcopy(referral["emergency"]),
            "milestones": copy.deepcopy(referral["milestones"]),
            "discharge_plan": copy.deepcopy(referral["discharge_plan"]),
            "created_at": referral["created_at"],
        }

    def _center_view(self, referral):
        patient = referral["patient"]
        return {
            "id": referral["id"], "status": referral["status"],
            "patient": {"name": patient["name"], "age": patient["age"],
                        "gender": patient["gender"], "phone": patient["phone"],
                        "address": patient["address"]},
            "steward_name": self._steward_name(referral),
            "discharge_plan": copy.deepcopy(referral["discharge_plan"]),
            "downward_acceptance": copy.deepcopy(referral["downward_acceptance"]),
            "follow_ups": copy.deepcopy(referral["follow_ups"]),
            "return_result": copy.deepcopy(referral["return_result"]),
            "created_at": referral["created_at"],
        }

    def _patient_view(self, referral):
        plan = referral["discharge_plan"] or {}
        return {
            "id": referral["id"], "status": referral["status"],
            "expected_arrival": referral["original_plan"]["expected_arrival"],
            "steward_name": self._steward_name(referral),
            "discharge_summary": plan.get("summary"),
            "return_result": copy.deepcopy(referral["return_result"]),
            "created_at": referral["created_at"],
        }
