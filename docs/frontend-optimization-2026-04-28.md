# 前端卡顿优化记录

优化时间：2026-04-28  
范围：`D:\Projects\dingchang_frontend_full`  
约束：不修改前端样式布局，不修改数据库字段

## 问题判断

本轮前端优化前，本地真实访问已经证明列表 SQL 本体不是主要瓶颈，`GET /api/orders` 隔离 P95 约 32ms。继续排查后发现，前端卡顿主要来自以下逻辑问题：

1. 列表页进入详情后返回，会重新触发列表、total、团队、业务员等请求。
2. `Dashboard.vue` 使用裸 `router-view`，列表页离开到详情时没有稳定保留状态。
3. `OrderListBase.vue` 的页面上下文 key 依赖全局 `route.path`，列表组件即使被缓存，进入详情时也会被后台响应式触发，误判为上下文变化。
4. 字段配置、团队、业务员、客户/渠道下拉等低频变化数据缺少统一短缓存和并发去重。
5. 远程下拉搜索每次输入都可能立即请求，快速输入时会产生无效请求。

## 已实施优化

| 优化点 | 说明 |
| --- | --- |
| 请求短缓存与并发去重 | 新增 `src/utils/requestCache.js`，对低频 GET 请求做 TTL 缓存，同一 key 的并发请求合并为一个 Promise |
| 数据版本号 | 新增 `src/utils/dataVersion.js`，写操作成功后 bump 对应业务版本，避免缓存导致脏数据 |
| 客户/渠道远程下拉缓存 | `customerChannel.js` 对分页下拉接口做 60s 缓存；客户/渠道增删改后主动失效 |
| 团队/业务员缓存 | `orders.js` 对 `getTeams`、`listSalespersons` 做 60s 缓存；用户增删改后失效 |
| 字段配置共享缓存 | `useOrderFieldConfig.js` 对 `/field-config/form-config` 做 5 分钟共享缓存，多个页面共用同一次结果 |
| 远程下拉防抖 | `RemotePagedSelect.vue` 搜索输入增加 220ms 防抖，并用序号防止旧请求覆盖新状态 |
| 列表 KeepAlive | `Dashboard.vue` 对订单列表、已完成、未完成、财务列表启用 KeepAlive，不改变布局 |
| 列表状态快照 | `OrderListBase.vue` 离开列表时保存筛选、分页、数据、total、summary 和版本号；返回时版本未变且 30s 内直接恢复 |
| 稳定页面上下文 key | `OrderListBase.vue` 的 key 改为 `pageMode + mode`，不再依赖全局 `route.path`，避免进入详情时后台误刷新列表 |

## 正确性保护

1. 订单创建、草稿、保存、完成状态、图片绑定等写操作成功后 bump `orders` / `finance` 版本。
2. 财务回款、返点、退回未完成成功后 bump `finance` / `orders` 版本。
3. 用户增删改后失效团队/业务员缓存，并 bump `users` / `orders` 版本。
4. 客户/渠道增删改后失效远程下拉缓存，并 bump `customer-channel` 版本。
5. 列表状态快照恢复前会校验版本号和 TTL；版本变化或超过新鲜窗口都会重新请求后端。

## 本地实测结果

测试方式：打开本地前端 `http://127.0.0.1:5173`，使用本地测试 session，真实点击订单列表首行“详情”，进入详情后用浏览器返回列表，并清空后端性能日志观察返回过程新增请求数。

| 场景 | 返回列表后的后端新增请求 |
| --- | ---: |
| 优化前 / 直接销毁重建 | 约 2-3 个请求，包含 `GET /api/orders`、`GET /api/orders total_only`、团队/业务员 |
| 修正 KeepAlive 但上下文 key 仍依赖 `route.path` | 仍有 2 个订单列表请求 |
| 修正稳定 key + 状态快照后 | 0 个请求 |

最终复测结果：从 `/orders/all` 点击“详情”进入订单详情，再返回 `/orders/all`，`logs/frontend_perf.jsonl` 新增请求数为 `0`。

## 自检

1. 前端生产构建通过：`npm run build`。
2. 浏览器真实链路复测通过：列表 -> 详情 -> 返回列表，后端新增请求为 `0`。
3. 临时本地测试入口已删除，测试 session 已在测试后置为失效。

## 后续可继续优化

1. 对财务导出做异步任务或流式导出，解决当前唯一秒级接口。
2. 对用户、客户、渠道管理页加入同类的“查询参数 key + 短状态快照”，减少管理页来回切换重查。
3. 对大表格后续可评估虚拟滚动，但这会触及表格交互细节，需单独审查后再做。

