# 前端导航页 CRUD/链路实测审查（2026-05-03）

## 范围

- 前端路径：`D:\Projects\dingchang_frontend_full`
- 后端路径：`D:\Projects\dingchang\dingchang_backend`
- 访问方式：通过前端 Vite 代理 `http://127.0.0.1:5173/api` 调接口，避免绕过前端代理链路。
- 浏览器限制：Codex in-app browser backend 本轮不可用，因此用前端路由 GET + API 自动化替代真实点击。
- 测试日志：
  - `logs/nav-crud-results-2026-05-03.json`
  - `logs/nav-crud-user-rerun-after-fix-2026-05-03.json`
  - `logs/nav-crud-ai-bind-after-fix-2026-05-03.json`

## 覆盖结果

- 登录/用户：创建业务账号、按关键字查询、改密、登录、退出、删除，修复后 9/9 通过。
- 客户：列表、创建、下拉查询、更新、删除、删除后 include_deleted 回读通过。
- 渠道：列表、创建、下拉查询、更新、删除、删除后 include_deleted 回读通过。
- 订单：团队/业务员/客户/渠道下拉、草稿创建、提交、详情、保存、全部/未完成/已完成列表、total_only、完成状态流转通过。
- 财务：汇总、详情、单条导出、回款/返点标记、取消标记、退回未完成通过。
- 字段配置：配置列表、表单配置、同值更新、更新后回读通过。
- AI 助手：健康检查、会话列表、创建、历史、问答、删除通过；图片绑定接口补齐后通过。
- BOS：`/orders/bos-sts` 读取通过；真实 `/orders/bos-upload` 会写外部对象存储，本轮只做缺文件 422 安全校验，未上传真实文件。

## 关键耗时

- `GET /orders/bos-sts`：494.65 ms，最慢，但属于外部 STS/签名链路，可接受但应持续观察。
- `POST /orders/finalize`：169.63 ms。
- `GET /orders/teams`：165.47 ms。
- 用户创建/改密/登录：约 150 ms。
- `PUT /orders/{id}`：115.63 ms。
- `POST /orders/draft`：88.17 ms。

## 已修复问题

- 用户删除 500：登录过的账号即使退出，`user_session_new` 仍有外键引用，`DELETE /users/{id}` 删除 `user_new` 时触发 FK 异常。已在 `app/services/users_service.py` 修复：删除用户前清理该用户 session；若用户存在关联订单，则返回明确业务错误“用户存在关联订单，不能删除”，不再把数据库异常冒泡为 500。
- AI 图片绑定 404：前端 `bindOrderImagesForAi()` 调用 `/orders/{id}/images/bind`，后端缺接口。已在 `app/api/v1/orders.py` 增加接口，返回 `ok/order_id/bound_count/ocr_task_id/ocr_status`，并复用 slot、storage_key、OCR 任务校验逻辑。

## 权限审查备注

- `权限.txt` 中财务 BOS 仍写为 `/finance/bos-sts`、`/finance/bos-upload`、`/finance/finalize`，但当前前端与后端实际链路是 `/orders/bos-sts`、`/orders/bos-upload`、`/orders/finalize`，建议后续同步权限文档，避免运维和测试误判。
- 业务/财务/市场账号只允许 `team_name` 单团队；经理账号才允许 `team_names` 多团队。前端表单逻辑是合理的，自动化测试已按该规则补跑通过。

## 清理

- 本轮 API 创建的用户测试数据已通过接口删除。
- 本轮 AI 会话已通过接口删除。
- 本轮客户/渠道已先走 API 删除验证，再通过强前缀和 ID 校验做本地硬清理。
- 本轮订单无业务删除接口，已通过强前缀和 ID 校验清理订单、订单信息、订单事实投影测试数据。
