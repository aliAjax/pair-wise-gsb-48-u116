# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、交收完整性和公司行动调整和冲突检查。
- `src/netting.py`：净额批次归集与计算规则（纯计算，不依赖存储）。
- `src/repository.py`：结算指令 SQLite 建表、事务和查询。
- `src/netting_repository.py`：净额批次、组成明细和批次事件的 SQLite 存储。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/netting_service.py`：净额批次用例编排与单据退回钩子。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `static/netting.html`：净额批次核对页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data`需包含`settlement_account`结算账户。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/net-batches`：净额批次列表，可带`account`、`settlement_day`、`state`和`limit`参数。
- `POST /api/net-batches`：按账户、币种和交收日归集净额批次，请求体为`{"account":"...","currency":"CNY","settlement_day":2}`。
- `GET /api/net-batches/{id}`：批次详情，含净收付、净券和组成明细。
- `GET /api/net-batches/{id}/audit`：批次事件时间线。
- `POST /api/net-batches/{id}/actions/{action}`：批次动作（`confirm`/`refresh`/`settle`/`fail`/`cancel`），请求体为`{"expected_version":1,"data":{...}}`。

除`/health`、`/`和`/netting.html`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 净额批次

- 建单时记录`settlement_account`，批次按结算账户、币种和交收日归集`captured`/`adjusted`/`approved`状态的指令。
- 买入数量与金额为正、卖出为负，同一证券先相抵得到净券；已应用公司行动的指令按调整后数量参与。
- 批次确认（`confirm`）后组成单据不能另行结清或调整公司行动，只能整批交收（`settle`，需提交与净额一致的`positions`和足够的`cash_paid`）。
- 单据冲正或交收失败先退回批次：批次自动重算净额并回到归集中状态，单据再按自身流程处理；批次交收失败（`fail`）则释放全部组成单据。
- 批次、明细与事件均持久化于SQLite，服务重启后可通过接口或`/netting.html`核对。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
