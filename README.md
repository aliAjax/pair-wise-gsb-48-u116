# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/netting_rules.py`：净额归集与批次状态规则（买正卖负、同证券相抵、公司行动调整后数量）。
- `src/netting_repository.py`：净额批次、组成单据和批次审计的SQLite存储。
- `src/netting_service.py`：批次建批、确认、交收、失败退回、冲正退回与废弃的用例编排。
- `src/netting_api.py`：净额批次HTTP路由。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `static/netting.html`：净额批次页面，按账户和交收日展示净收付、明细和状态。
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
- `GET /api/netting/batches`：净额批次列表，可带`account`、`settlement_day`、`state`和`limit`参数。
- `GET /api/netting/batches/{id}`：批次详情，含净额汇总与组成单据明细。
- `GET /api/netting/batches/{id}/audit`：批次审计时间线。
- `POST /api/netting/batches`：按`{"settlement_account":"...","currency":"CNY","settlement_day":2}`归集已复核单据建批。
- `POST /api/netting/batches/{id}/actions/{action}`：批次动作`confirm`/`settle`/`fail`/`reverse`/`discard`，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`、`/`和`/netting.html`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 净额批次

- 建单时记录`settlement_account`，批次按结算账户、币种和交收日归集`approved`状态的单据。
- 买入数量为正、卖出为负，同一证券先相抵；已应用公司行动的按调整后数量参与；现金按净额收付。
- 批次确认后组成单据被锁定，不能单独结清、失败或冲正；批次交收在净额级别校验券款足额后，组成单据一并转为`settled`。
- 交收失败（`fail`）或冲正（`reverse`）先把批次退回`returned`状态，组成单据保持归集，可调整后重新确认；`discard`废弃批次才解除归集。
- 批次与单据状态在同一事务内落库，服务重启后仍可按账户和交收日核对净收付、明细和状态。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
