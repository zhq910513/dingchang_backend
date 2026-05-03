# 前端导航 CRUD/链路第二轮审查（2026-05-03）

## 范围与约束

- 后端路径：`D:\Projects\dingchang\dingchang_backend`
- 前端路径：`D:\Projects\dingchang_frontend_full`
- 访问方式：通过前端 Vite 代理 `http://127.0.0.1:5173/api` 调用接口，尽量贴近前端真实访问链路。
- 明确约束：未修改数据库字段，未修改前端样式和布局。
- 浏览器限制：Codex in-app browser 后端本轮不可用，已尝试连接 `iab` 后端但发现无可用 browser-use backend。因此本轮采用“前端路由 GET + 前端代理 API 自动化”替代真实点击。

## 日志与样本

- 主测试日志：`logs/nav-crud-second-round-nondeletion-2026-05-03.json`
- 匿名鉴权复测：`logs/nav-crud-second-round-auth-rerun-2026-05-03.json`
- 删除验证与清理：`logs/nav-crud-second-round-cleanup-2026-05-03.json`
- 清理后自检：`logs/nav-crud-second-round-selfcheck-2026-05-03.json`
- 本轮运行 ID：`20260503125606_104f14`
- 本轮测试前缀：`codex_round2_20260503125606104f14`
- 本轮创建样本：用户 `34/35/36/37`、客户 `440`、渠道 `239`、订单 `3221`、AI 会话 `ab76ce82275b462ea29e2c0a30e20052`。

## 覆盖结果

- 前端导航路由：`/login`、`/users`、`/ai-assistant`、`/channels`、`/customers`、`/orders/import`、`/orders/all`、`/orders/finished`、`/orders/unfinished`、`/orders/create`、`/finance`、`/orders/3221` 均能通过前端服务返回页面入口。
- 登录/会话：错误密码 401；测试角色创建后均可登录和退出；真实匿名访问 `/users`、`/orders`、`/customer-channel/customers` 均返回 401。
- 账号管理：`sales/finance/market` 访问 `/users` 返回 403；`manager` 可访问；业务/财务/市场账号若错误携带 `team_names` 会返回 400，符合单团队规则。
- 客户/渠道：创建、重复编码校验、下拉搜索、更新均通过；`finance` 创建客户/渠道返回 403。
- 字段配置：列表、表单配置读取通过；`sales` 管理字段配置返回 403；普通表单配置读取可用。
- 订单链路：缺少必填客户/渠道返回 400；业务创建草稿、提交、详情、更新、按 owner/vin/date 过滤、`total_only`、完成状态流转均通过。
- 财务链路：未完成订单详情拒绝财务访问；完成后财务详情、汇总、导出、回款/返点状态更新、退回未完成均通过。
- 角色边界：`market/finance` 创建订单返回 403；`sales` 访问财务汇总返回 403；`market` 可看财务汇总但导出和状态写入返回 403。
- AI 助手：会话创建、历史、列表、问答、空消息校验、销售账号会话隔离均通过；订单图片绑定空绑定 200，非法 slot 400。
- BOS：`GET /orders/bos-sts` 通过；`POST /orders/bos-upload` 缺文件校验返回 422。本轮未执行真实文件上传，因为该动作会写入外部对象存储且当前未发现配套删除接口。

## 结果判定

- 主测试 88 步，87 步通过，1 步失败。
- 失败项为测试脚本问题：标记“anonymous /users should reject”的请求误带管理员 token，接口返回 200。随后已单独用无 token 请求复测，`/users`、`/orders`、`/customer-channel/customers` 全部 401，因此不是业务缺陷。
- 当前第二轮非删除链路判定为通过。

## 慢接口观察

- `GET /orders/bos-sts`：约 435 ms，属于外部 STS/签名链路，当前可接受但建议后续继续观察。
- `POST /auth/login`：约 246-356 ms，测试账号登录最慢；当前使用密码校验和会话落库，属于可接受区间。
- `POST /users`：约 208-295 ms，主要是账号、角色和权限校验写入链路。
- `POST /orders/finalize`：约 268 ms，提交订单包含订单主体、订单信息、投影/事实数据同步，仍在可接受范围。
- 常规列表/筛选：大部分在 10-50 ms，第二轮未复现前端列表查询卡顿。

## 权限与文档差异

- `权限.txt` 仍包含旧路径 `/finance/bos-sts`、`/finance/bos-upload`、`/finance/finalize`；真实前后端链路为 `/orders/bos-sts`、`/orders/bos-upload`、`/orders/finalize`。
- 本轮继续按当前后端真实 ACL 和既有审查文档执行，没有为了性能扩大任何角色的数据范围。
- `market` 可查看财务汇总但禁止导出/状态写入，这与当前后端实现一致；若利润/应收应付属于敏感字段，建议后续由业务侧确认市场只读范围是否继续保留。

## 删除验证与清理

- 已按确认执行“清理、不上传 BOS”。
- 删除保护验证通过：订单 `3221` 存在时，删除销售用户 `34` 返回 400，避免数据库外键异常冒泡成 500。
- API 删除通过：AI 会话删除 200；客户 `440`、渠道 `239` 软删除 200；无订单关联的用户 `35/36/37` 删除 200。
- 订单 `3221` 无业务删除接口，已在强前缀、固定 ID、车主名、客户/渠道编码全部匹配后，安全清理订单、订单信息、订单事实投影、客户、渠道测试残留。
- 订单清理后再次通过 API 删除销售用户 `34`，返回 200。
- 残留核验：用户、用户角色、用户会话、订单、订单信息、订单事实、订单图片、OCR 任务、客户、渠道的本轮测试残留计数均为 0。
- 真实 BOS 上传未执行：该动作会写入外部对象存储，且当前未发现配套删除接口，本轮按确认跳过。

## 清理后自检

- 后端关键文件编译：`python -B -m py_compile app\api\v1\orders.py app\services\users_service.py` 通过。
- 前端生产构建：`npm run build` 通过。
- 后端健康检查：`/api/health` 返回 `ok=true`，数据库和 Redis 均为 true。
- 匿名鉴权：无 token 请求 `/users` 返回 401。
- 删除后读回：`/orders/3221` 返回 404。
- API 残留：用户前缀、订单前缀、客户编码 `R2CU5606104F14`、渠道编码 `R2CH5606104F14` 均查询为 0。
- 数据库残留：本轮用户、角色、会话、订单、订单信息、订单事实、图片、OCR、客户、渠道均为 0。
