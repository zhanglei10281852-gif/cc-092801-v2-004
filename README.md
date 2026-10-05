# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，婚庆套餐版本与订单冻结接口使用 `/api/wedding` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/packages/      婚庆套餐不可变版本、发布/撤回与订单确认冻结
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 婚庆套餐版本与订单冻结

套餐包含场地布置（venue）、摄影（photography）、司仪（host）、餐饮（catering）四项承诺及附加项。

- 每次调整都会生成带 `snapshot_digest` 的**不可变快照**版本，记录变更原因（`change_reason`）与操作者（`created_by` / `published_by` / `withdrawn_by`）；草稿（draft）独立存放，发布（published）后才对外生效，已发布版本内容永不被改写。
- 订单创建时只登记期望版本（`expected_version_no`）；确认后把该版本快照与摘要复制进订单（`frozen_version_no`、`frozen_snapshot_json`、`frozen_digest`）。之后供应商改价或换人只产生新版本，**已确认订单仍按当时承诺执行**，草稿修改不会影响任何已下单内容。
- 基于旧版本确认时，若已有更新的已发布版本，接口返回 `409 conflict` 并附带 `expected_version_no` 与 `latest_published_version_no`，需显式改单（`/retarget`，留审计）后才能确认；也支持 `expected_row_version` 乐观锁，并发确认只有一方成功，另一方得到明确冲突。
- 管理员可比较任意两个版本（字段级变更与分项/总价差），并可撤回尚未使用的已发布版本；被待确认或已确认订单引用时撤回被拒绝（返回引用计数）。
- 查询订单会返回生效版本 `effective_version_no`、当前最新发布版本，以及与最新版本的差异摘要 `diff_to_current`；套餐时间线 `/timeline` 给出版本链与全部审计事件。所有版本关系与审计记录均保存在 SQLite，服务重启后完整恢复。

主要接口（前缀 `/api/wedding`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/packages` | 创建套餐，生成已发布 v1 不可变快照 |
| POST | `/packages/{code}/drafts` | 基于某版本创建草稿（变更原因、操作者） |
| POST | `/packages/{code}/publish` | 发布草稿为新版本 |
| POST | `/packages/{code}/versions/{n}/withdraw` | 撤回尚未被订单引用的版本 |
| POST | `/packages/{code}/versions/diff` | 比较两个版本并输出差异摘要 |
| GET | `/packages/{code}` / `/packages/{code}/timeline` | 版本列表、版本链与审计时间线 |
| POST | `/orders` | 按已发布版本创建待确认订单 |
| POST | `/orders/{id}/confirm` | 确认并冻结引用版本（旧版本/乐观锁冲突返回 409） |
| POST | `/orders/{id}/retarget` | 待确认订单显式改单到另一版本（留审计） |
| POST | `/orders/{id}/cancel` | 取消待确认订单 |
| GET | `/orders` / `/orders/{id}` | 查询订单、生效版本与差异摘要 |


## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
