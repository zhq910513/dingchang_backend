# 页面审查：订单列表 / 财务列表
更新时间：2026-04-26

本页审查范围：`/orders/all`、`/orders/finished`、`/orders/unfinished`、`/finance`，以及 `GET /orders`、`GET /orders/teams`、`GET /orders/salespersons`、`GET /finance/orders/summary`、`GET /finance/orders/export`。

## 权限边界

| 角色 | 订单列表 | 财务列表 |
| --- | --- | --- |
| `super_admin` | 可看全量，可按团队/业务员筛选 | 可看/汇总/导出全量 |
| `manager` | 只看辖区团队业务员订单 | 只看/汇总/导出辖区团队订单 |
| `sales` | 只看自己的订单 | 禁止访问 |
| `finance` | 仅允许按单团队读取已完成订单；前端订单模块禁入；财务页复用 `/orders?is_finished=true` | 只看/汇总/导出单团队已完成订单，可改回款/返点/退回 |
| `market` | 只看单团队订单，不可写 | 可看/汇总单团队财务数据，不可导出/写入 |

本轮没有扩大角色数据范围。财务域权限继续按 `权限.txt`：`sales` 禁止财务；`market` 只读；`finance/manager/super_admin` 可导出和写财务状态。

## 本地数据抽样

| 项目 | 数量 |
| --- | ---: |
| 订单总数 | 3177 |
| 已完成订单 | 2859 |
| 未完成订单 | 318 |
| `order_info_new` | 3177 |
| `order_fact_new` | 3177 |
| 缺失 `order_fact_new` | 0 |
| 空业务员订单 | 0 |

角色样本：`finance=4`、`manager=10`、`market=2`、`sales=7`、`super_admin=1`。

## 已发现问题与处理状态

| 编号 | 问题 | 风险 | 处理状态 |
| --- | --- | --- | --- |
| ORDER-LIST-001 | 初次进入列表时，`onMounted` 与 `onActivated` 在 KeepAlive 场景可能连续触发两轮列表请求 | 首屏重复请求，造成用户感知卡顿和后端无效压力 | 已处理：首轮 `onActivated` 跳过，不改布局/样式 |
| ORDER-LIST-002 | 团队/财务精确 total 需要 `is_finished + salesperson_id` 组合过滤，旧计划容易先扫大量已完成订单 | 数据增大后 total 请求拖慢分页 | 已处理：新增复合索引 `ix_order_list_finished_salesperson_id`，并仅在 scoped total 上使用 MySQL index hint |
| ORDER-LIST-003 | 车主/备注/市场等模糊搜索仍是 `%keyword%` | 大数据量下无法完全利用 BTree 索引 | 保留当前业务体验；后续如数据继续放大，应引入只读搜索投影或全文索引方案 |
| ORDER-LIST-004 | 财务页首屏会先取列表，再异步取精确 total 和 summary | 单页实际有 2-3 个接口请求 | 当前已做取消与异步拆分；继续保留，避免首屏被汇总阻塞 |
| ORDER-LIST-005 | 财务角色可直接调用普通订单列表/详情看到未完成订单 | 前端菜单禁入无法作为安全边界 | 已处理：财务角色在 `/orders` 列表/详情仅可读已完成订单，未完成列表 0 SQL 拒绝，未完成详情 1 SQL 拒绝 |

## 已实施优化

1. `app/models/order.py` 新增 `ix_order_list_finished_salesperson_id(is_finished, salesperson_id, id)`，不修改任何数据库字段。
2. `app/api/v1/orders.py` 对团队/业务员范围内的精确 total，在 MySQL 下强制使用该复合索引；列表页取 ID 仍保留优化器原计划，避免为第一页引入 filesort。
3. `src/views/orders/OrderListBase.vue` 增加首轮 activated 跳过保护，避免初次进入页面重复拉取列表。

## 自检结果

1. 编译检查通过：`python -m py_compile app/models/order.py app/api/v1/orders.py app/api/v1/finance.py app/services/order_read_model.py app/services/order_fact_service.py`。
2. 前端构建通过：`npm run build`。
3. 启动期 schema 工具实际新增索引：`ix_order_list_finished_salesperson_id`。
4. 默认列表第一页复测：`super_admin` 约 13.25ms、`manager` 约 13.30ms、`sales` 约 9.22ms、`finance` 约 11.26ms、`market` 约 7.20ms。
5. 精确 total 复测：`finance?is_finished=true` 约 3.86ms，`manager?is_finished=true` 约 2.33ms，`sales?is_finished=true` 约 1.14ms，且 scoped total SQL 确认包含 `FORCE INDEX`。
6. 财务 summary 复测：`super_admin` 约 12.09ms、`manager` 约 7.28ms、`finance` 约 12.43ms、`market` 约 3.95ms。
7. 财务普通订单读权限复测：未传 `is_finished` 时后端强制只返回已完成订单；`is_finished=false` 0 SQL 返回 403；未完成详情 1 SQL 返回 403。

## 残余风险

当前数据量只有 3177 单，模糊搜索和导出全量仍可接受；如果后续订单量达到十万级，`%keyword%`、HTML Excel 一次性拼接、summary 实时聚合会成为下一轮瓶颈。该阶段应优先做只读搜索投影/异步导出任务，而不是继续堆叠前端 loading。
