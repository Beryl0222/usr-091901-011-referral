# 县域双向转诊闭环

承接基层上转、到院再分级、院内陪诊、稳定期下转和一周内回访。项目以领域契约约定参与者、状态和不可破坏的业务原则，领域层（`domain.py`）以追加事件流承载转诊单全部事实，服务层（`service.py`）提供 HTTP 接口。

## 运行

- `python3 service.py --check`：核对服务配置与契约
- `python3 service.py --port 8000`：启动服务
- `python3 -m unittest -v`：运行全部测试（领域规则 + HTTP 端到端）

## 状态流

待联系 → 待到院 → 院内处理中 → 待下转 → 随访中 → 已闭环。见面发现危重信号可随时改走**急诊**，但电话分级、预计到达时间与原门诊计划在 `original_plan` 中只读保留。

## 接口（`X-Actor-Token` 鉴权）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 基层医生/村医/个体诊所/体检高危人群提交病情摘要、电话分级与期望到达时间 |
| POST | `/cases/{id}/commands` | 推进流程：`accept`、`meet_and_retriage`、`reroute_emergency`、`order_exam`、`record_exam_result`、`admit`、`discharge_plan`、`accept_downstream`、`complete_follow_up`、`resolve_pending_item`、`mark_unreachable`、`escalate`、`reassign`、`close` |
| GET | `/cases` | 按角色过滤的转诊单列表 |
| GET | `/cases/{id}` | 按角色裁剪后的详情（越权字段不返回） |
| GET | `/cases/{id}/trace` | 负责人追溯：每次交接、未完成事项、失联升级、急诊改道与最终回流结果 |
| GET | `/health`、`/contract` | 运行状态与领域契约 |

## 关键规则

- **幂等**：每条外部消息携带 `idem_key`，平台重推或电话补录同一事实只推进一次，响应中 `idempotent_replay: true` 表示命中去重。
- **抢单**：三名专职管家与高峰支援共用调度池，同一单先抢先得，第二个接单者得到 409；负责人可 `reassign` 改派，交接链完整保留。
- **回访**：基层接收即起算一周期限，逾期补录需 `late_ack`；回访未完成或有待办未勾销时不能闭环。
- **最小可见**：基层来源只见状态与随访结论，专科医生见临床信息不见事件流，接收卫生院见联系方式与出院方案，负责人可见全部事件与追溯视图。
