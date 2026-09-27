# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8215`。身份使用 `X-Actor` 和 `X-Role`，角色为 `dispatcher`、`operator`、`field`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/assets`、`POST /api/facilities`：登记线路资产和医院等重要用户。
- `POST /api/outages`：创建或幂等接收同一事故。
- `POST /api/telemetry`：记录并隔离错误遥测。
- `POST /api/plans`、`/submit`、`/approve`、`/activate`：创建、提交、审批并启用安全恢复计划。
- `POST /api/plans/{id}/change`：在不修改已确认步骤的前提下创建新计划版本。
- `POST /api/field-reports`：合并现场离线报告，重复客户端编号不会重复写入；接地线未拆除时完成报告只留待处理（held）。
- `POST /api/plans/{id}/confirm`：调度员确认步骤，依赖未满足或资产接地闭锁中时拒绝。
- `POST /api/grounding`：登记接地线挂设台账（线路、接地线编号、挂设时刻、班组、现场单号），同一现场单号重复上传沿用第一次结果。
- `POST /api/grounding/{id}/remove`：由另一名值班员核对线号和现场照片后登记拆除，解除后受影响步骤的待处理报告重新生效。
- `GET /api/grounding`：接地线台账及按事故的闭锁线路、待办步骤和解除条件。
- `POST /api/status`：发布当前恢复状态；发布恢复完成前，事故相关接地线必须全部有拆除记录。
- `GET /api/plans/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成。
