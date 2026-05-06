# 业务线最高规格审查覆盖矩阵

审查日期：2026-04-28

## 覆盖原则

1. 每条业务线必须同时覆盖：前端入口、后端接口、角色边界、数据范围、性能风险、自检结果。
2. 不修改数据库字段，不修改前端布局样式。
3. 优化默认以“缩小查询范围、减少 ORM 关系加载、减少固定 SQL 成本、先 ACL 后详情加载”为优先。
4. 后台任务和系统基础设施虽然不是页面，也按业务线落库，因为它们会影响所有页面的响应速度。

## 业务线覆盖矩阵

| 业务线 | 前端入口 | 后端入口 | 审查文档 | 当前状态 |
| --- | --- | --- | --- | --- |
| 登录 / 会话 | `Login.vue` | `/auth/login`, `/auth/logout`, `app/api/deps.py` | `page-audit-login.md`, `page-audit-system-foundation.md` | 已审查并优化 |
| 用户管理 | `UserList.vue`, `CreateUser.vue` | `/users/*` | `page-audit-user-management.md` | 已审查并优化 |
| 订单列表 | `OrderListBase.vue`, `OrderList.vue`, `OrderFinished.vue`, `OrderUnfinished.vue` | `GET /orders` | `page-audit-order-list.md` | 已审查并优化 |
| 订单详情 | `OrderDetail.vue` | `GET /orders/{id}` | `page-audit-order-detail.md` | 已审查并优化 |
| 订单创建 / 编辑保存 | `OrderCreate.vue`, `OrderCreateForm.vue`, `OrderDetail.vue` | `POST /orders`, `POST /orders/draft`, `PUT /orders/{id}`, `POST /orders/finalize` | `page-audit-order-save.md` | 已审查并优化 |
| 订单导入 / OCR | `OrderImport.vue`, `VehicleCertTable.vue` | `/orders/finalize`, `/orders/ocr-tasks`, OCR worker | `page-audit-order-import-ocr.md`, `page-audit-background-jobs.md` | 已审查并优化 |
| 订单辅助接口 / 上传 | 订单筛选、创建、详情、AI 上传 | `/orders/customer-groups`, `/orders/channel-groups`, `/orders/teams`, `/orders/salespersons`, `/orders/bos-sts`, `/orders/bos-upload` | `page-audit-order-auxiliary.md` | 已审查并优化 |
| 财务 | `FinanceList.vue` | `/finance/*`, `/orders?is_finished=true` | `page-audit-finance-list.md` | 已审查并优化 |
| 客户 / 渠道 | `CustomerList.vue`, `ChannelList.vue`, 远程下拉 | `/customer-channel/*` | `page-audit-customer-channel.md` | 已审查并优化 |
| 字段配置 | 动态表单配置加载 | `/field-config/*` | `page-audit-field-config.md` | 已审查并优化 |
| 报价助手 | `AiAssistantWorkbench.vue` | `/ai-assistant/*` | `page-audit-ai-assistant.md` | 已审查并优化 |
| Dashboard / 路由权限 | `Dashboard.vue`, `router/index.js` | 前端路由守卫、菜单权限 | `page-audit-dashboard-routing.md` | 已审查 |
| 启动期 / 后台任务 | 应用 lifespan, OCR poller, order fact backfill | `app/main.py`, `ocr_worker.py` | `page-audit-background-jobs.md` | 已审查并优化 |
| 系统基础线 | 全站共享 | 配置、DB、鉴权依赖、存储、外部服务 | `page-audit-system-foundation.md` | 已审查并优化 |
| 本地前端性能实测 | 前端真实页面打开、同源接口请求 | `/api/*` 服务端计时 | `local-frontend-perf-2026-04-28.md` | 已实测并归档 |
| 前端卡顿优化 | 列表返回、远程下拉、字段配置、低频字典缓存 | 前端数据层、路由缓存、请求去重 | `frontend-optimization-2026-04-28.md` | 已优化并实测 |

## 当前路由覆盖结论

后端 `app/api/v1` 当前路由均已有对应审查文档覆盖；剩余前端 `NoPermission.vue` 属于静态权限提示页，`VehicleCertTable.vue` 属于订单导入页组件，已归入订单导入 / OCR 业务线。
