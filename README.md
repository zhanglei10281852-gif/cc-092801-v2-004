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

服务订单运营接口使用 `/api/compute` 前缀，婚庆套餐版本与订单冻结接口使用 `/api/ceremony` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 婚庆套餐版本与订单冻结

套餐固定包含场地布置、摄影、司仪、餐饮四类服务。顾问每次调整都会生成**不可变版本快照**（含变更原因、操作者、父版本与内容摘要），订单确认后会把所引用的版本与价格**冻结**到订单上，供应商事后改价或换人都不会改变已确认订单的承诺内容。

- `POST /api/ceremony/packages?actor=` 建立套餐
- `POST /api/ceremony/packages/{code}/versions?actor=` 基于当前（或指定）版本创建草稿快照，需给 `change_reason`
- `POST /api/ceremony/packages/{code}/versions/{n}/publish?actor=` 发布版本，旧的生效版本自动置为 `superseded`
- `GET /api/ceremony/packages/{code}/versions`、`.../versions/{n}` 查询版本与快照
- `GET /api/ceremony/packages/{code}/compare?from=1&to=2` 逐字段比较两个版本，返回差异明细与价格差异摘要
- `POST /api/ceremony/packages/{code}/versions/{n}/withdraw?actor=` 撤回**尚未被任何订单使用**的版本
- `GET /api/ceremony/packages/{code}/history` 查看套餐版本审计记录
- `POST /api/ceremony/orders?actor=` 建立草稿订单（可带 `expected_version`）
- `PUT /api/ceremony/orders/{id}/expected-version?actor=` 草稿订单改挂版本（确认后禁止）
- `POST /api/ceremony/orders/confirm?actor=` 确认订单并冻结当前生效版本；若携带的 `expected_version` 已过期（并发期间发布了新版本）返回 409 冲突并给出最新版本号
- `GET /api/ceremony/orders/{id}`、`/api/ceremony/orders/by-no/{no}` 查询订单，响应包含生效版本快照 `effective_version`、当前版本号与 `diff_summary` 差异摘要
- `GET /api/ceremony/orders/{id}/history` 查看订单审计记录

版本关系（父子链、状态）与审计事件全部落库，服务重启后仍可完整恢复。

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
app/ceremony/      婚庆套餐不可变版本、版本比较/撤回与订单版本冻结
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
