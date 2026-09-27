# 县域双向转诊闭环

承接基层上转、到院再分级、院内陪诊、稳定期下转和回访。项目以领域契约约定参与者、状态和不可破坏的业务原则：`referral_domain.py` 是状态机、消息幂等、排班接单与角色视图的领域核心，`service.py` 对外提供 HTTP 接口。

## 运行

- `python3 service.py --check` 核对服务配置与契约一致性
- `python3 service.py --port 8000` 启动服务
- `python3 -m unittest -v` 运行全部测试

## 业务闭环

`待联系 → 待到院 → 院内处理中（检查/住院/出院方案）→ 待下转 → 随访中 → 已闭环`

- 村医、个体诊所、体检机构（高危人群）与卫生院以基层医生身份提交病情摘要和期望到达时间。
- 当日排班（专职 + 高峰支援）的转诊管家接单，同一单只能一人承接；确认接人后进入待到院。
- 见面到院重新分级；出现危重信号可立即改走急诊，原始转诊计划完整保留，处置后流程继续。
- 出院方案确定后进入待下转，接收卫生院确认后一周内回访，记录复查是否完成与回流结果后闭环。
- 联系不上患者可失联升级给负责人；每次交接、未完成事项、升级与回流结果都能从患者记录的时间线查到。

## 接口约定

- 除 `/health`、`/contract` 与首个负责人登记外，所有请求需带 `X-Actor-Id` 头。
- 所有写操作必须携带 `message_id`（平台消息编号或电话补录单号）：同一编号只推进一次，重放返回首次受理结果并带 `duplicate: true`。
- 患者信息按角色最小可见：基层医生只见本人提交单据的进展，专科医生不见联系方式，卫生院只见下转至本机构的患者，范围外的记录一律返回 404，负责人可见全部。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | /health | - | 运行状态 |
| GET | /contract | - | 领域契约 |
| POST | /actors | 负责人（首个自举登记） | 登记参与者 |
| POST/GET | /roster | 负责人排班，管家可查 | 每日专职与高峰支援 |
| POST | /referrals | 基层医生 | 提交上转申请 |
| GET | /referrals | 各角色 | 按职责过滤的列表，可 `?status=` |
| GET | /referrals/{id} | 参与照护者 | 角色视图 |
| POST | /referrals/{id}/claim | 转诊管家 | 接单（限当日排班，唯一） |
| POST | /referrals/{id}/contact | 承接管家 | 确认接人 → 待到院 |
| POST | /referrals/{id}/arrival | 承接管家/专科医生 | 到院再分级 → 院内处理中 |
| POST | /referrals/{id}/emergency | 承接管家/专科医生 | 危重改走急诊（保留原计划） |
| POST | /referrals/{id}/milestones | 专科医生/承接管家 | 检查/住院/出院方案 |
| POST | /referrals/{id}/accept | 接收卫生院 | 确认接收 → 随访中 |
| POST | /referrals/{id}/follow-ups | 接收卫生院/承接管家 | 回访记录，completed 后闭环 |
| POST | /referrals/{id}/escalations | 承接管家/负责人 | 失联升级 |
| POST | /referrals/{id}/escalations/{eid}/resolve | 负责人 | 处理升级 |
| POST | /referrals/{id}/notes | 参与照护者 | 平台/电话补录（不推进状态） |
| GET | /referrals/{id}/timeline | 负责人/承接管家 | 交接、待办、升级与回流结果 |
| GET | /open-items | 负责人 | 全部未完成事项（含逾期标记） |
