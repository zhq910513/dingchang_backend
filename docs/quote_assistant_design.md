# 报价助手落库与流程设计

## 边界

- 本线路允许新增报价助手专用表。
- 已有业务表不做结构修改。
- 已拍版订单、财务、账号、OCR接口不改变表字段和页面布局。
- 平台登录、填报、报价适配器未接入前，先使用本地假流程跑通：资料审查 -> 短信验证码 -> 假报价返回。

## 新增表

- `quote_case_new`：报价会话的业务主档。可关联已有订单，也可作为新订单草稿独立存在。
- `quote_case_image_new`：报价图片候选池。记录用户上传槽位、系统识别槽位、最终确认槽位和 active/replaced 状态。单图槽只保留一个 active，旧图不删除。
- `quote_task_new`：一次平台报价任务。保存登录状态、短信状态、提交快照、平台请求/响应/标准化结果。
- `quote_case_event_new`：报价助手业务记忆与审计事件。保存聊天输入、图片归位、任务状态变化等。
- `quote_assistant_session_new`：报价助手聊天会话主表。保存归属用户、标题、软删除状态、消息数和最后消息摘要。
- `quote_assistant_message_new`：报价助手聊天消息表。保存用户/助手消息、展示 metadata、图片撤回状态和分页游标。

## 图片归位策略

- 上传时不纠正、不提示用户。
- 入报价草稿时执行识别并写入 `provided_slot_key`、`predicted_slot_key`、`confirmed_slot_key`。
- 高置信 OCR/文件名/路径规则自动归位。
- 低置信时保留用户上传槽位，若无法判断则放入 `related`。
- 单图槽如已有 active 图片，新图片成为 active，旧图片改为 `replaced`，不物理删除。

## 报价流程

1. 用户在聊天框输入平台报价指令，或补充车主/车辆/图片信息。
2. 后端按 session/order 找到或创建 `quote_case_new`。
3. 若关联已有订单，读取订单已有字段和图片作为草稿基础。
4. 若无订单，则创建 `new_order_draft` 草稿，后续可扩展“一键转订单”。
5. 汇总聊天字段、订单字段、上传图片并写入报价草稿。
6. 校验必填项：车主姓名、手机号、身份证号、车牌、VIN、发动机号、车型、合格证、身份证正面、行驶证主页。
7. 未满足时返回真实缺失原因，不触发平台登录。
8. 满足时创建 `quote_task_new`，进入 `waiting_sms`，提示业务员输入短信验证码。
9. 当前版本输入 4-8 位验证码后走本地假报价返回，后续替换真实平台适配器。

## 上线前操作

生产环境如果 `AUTO_CREATE_TABLES=0`，需要先执行：

```bash
cd /data/backend/dingchang_backend
python scripts/create_quote_assistant_tables.py
```

脚本只创建报价助手七张新表，不会修改已有表结构。

## 本轮补充

### 会话历史分页

- 接口 `/ai-assistant/sessions/{session_id}/history` 默认只返回最新 3 条消息。
- 向上滚动时，前端继续按 `cursor + limit=5` 拉取更早历史。
- 后端返回 `next_cursor` 和 `has_more`，最早一页不会重复回卷。
- 生产接口使用 `quote_assistant_session_new` / `quote_assistant_message_new` 落库，不再依赖本地 JSON 文件保存聊天记忆。

### 平台登录资料留存

- 新增 `quote_platform_account_new`，按 `owner_user_id + platform_code` 维度存一份平台登录资料。
- 明文保留：登录手机号、账号名、最后使用时间、最后登录状态。
- 密文保留：密码和未来平台 token/cookie 载荷，避免每次重复询问。
- 业务员只发 `13800138000` 这种纯手机号时，在已有报价上下文里也能作为登录手机号补录。

### 订单查询扩展

- 仍然先走角色/团队权限控制，不越权读取订单。
- 支持按订单号、车牌、手机号、VIN、发动机号、身份证号定位订单。
- 可按问题中的字段展示保费、财务、图片、OCR、备注、状态等内容。

### 人性化提示约定

- 已记住的平台资料会优先复用，不反复追问。
- 如果只差登录手机号，会直接提示“请发登录手机号”，并允许后续一句话补齐账号和密码。
- 若资料齐了但还没有手机号，会先保存账号/密码，再继续提示补手机号。

### 本轮稳定性加固

- 平台别名检测已收紧：`PA`、`TP` 这类短英文缩写只允许作为独立缩写命中，避免从密码或账号值里误判平台。
- 凭证类消息在识别平台前会先遮盖登录手机号、账号、密码的值，只保留标签和真正写在外面的平台名。
- 会话 JSON 存储增加跨进程锁，并在每次读写前重新加载文件，避免线上多 worker 下历史消息互相覆盖或删除后仍被旧进程读到。
- 已补回归测试：凭证平台误判、显式平台凭证保存、会话历史分页、删除后不可见、双 Store 实例一致性。

## 2026-05-05 深度审查补充

### 图片拖入报错根因与修复

- 根因：前端拖入图片后，上传响应里可能带临时 BOS 签名 URL，包含 `authorization` 和 `x-bce-security-token`，长度可能超过 `quote_case_image_new.image_url varchar(512)`，写库时报 `Data too long for column image_url`。
- 修复：报价助手入库时只保存基于 `storage_key` 生成的稳定短 URL，不再持久化签名 query；返回给前端的 `attached_images/images_by_slot` 同步使用短 URL。
- 兼容：不修改已有字段，不删除图片；单图槽仍只保留一个 active，旧图转 `replaced`。

### 会话历史与隐私沉淀

- 用户消息上下文入历史前会过滤 `current_user_id`、`role_name`、`team_names`、`session_id` 等内部上下文。
- 助手 metadata 入历史前递归清理签名 URL、STS token、授权 query、平台密码和密文字段。
- 图片历史只保留 `storage_key`、槽位、置信度、稳定展示 URL、撤回状态等必要信息，避免 JSON 历史文件持续膨胀。

### 前端失败体验

- 图片流程拆分为“上传阶段”和“后台识别归位阶段”，上传成功但助手处理失败时提示“图片处理失败”，不再误导为“图片上传失败”。
- 前端会把后端 `result_status=failed` 识别为业务失败；聊天气泡保留后端中文真实原因。
- 后端规则异常会将常见错误转换为中文可读原因，例如数据库字段过长、唯一冲突、MySQL 连接失败、锁等待超时、外部服务超时。

### 本轮真实自测

- `python -m compileall -q app tests`：通过。
- `python -m unittest discover`：22 个测试通过。
- `npm run build`：前端生产构建通过。
- 本地真实接口：`/api/health`、`/api/auth/login`、`/api/ai-assistant/chat`、`/api/ai-assistant/sessions/{session_id}/history` 通过。
- 长签名图片 URL 实测：聊天返回 `quote_image_collect`，历史接口和 `storage/quote_assistant_sessions.json` 均不含 `authorization` / `x-bce-security-token`，数据库 `image_url` 长度为 82。
- 平台账号引导实测：未绑定平台账号时返回“绑定账号”动作；绑定账号后可进入短信等待；输入验证码后假报价流程返回成功。

## 2026-05-17 链路打磨补充

### 图片上下文识别

- 用户在输入框补一句“这是身份证正面 / 行驶证副页 / 合格证”等说明后再拖图，后端会把这类上下文作为材料类型强提示参与归位。
- 弱上下文（例如“张三资料”）不会阻断真实 OCR；后端仍会继续尝试 OCR 分类，避免因为用户随手备注导致图片长期落到相关图片。
- 文件名或路径出现 `id-front`、`drive-main`、`vehicle-cert` 等明确材料类型时，会高置信直接归位，避免不必要的 OCR 慢调用。
- OCR 分类增加单次调用和单图总耗时预算，外部 OCR 慢或不可达时不会长期卡住聊天响应。

### 前端与提示

- 报价助手回复进一步精简，草稿号、任务号、trace 等内部信息只保留在结构化 metadata，不主动展示在聊天正文。
- 前端开发代理支持 `VITE_API_PROXY_TARGET`，本地 8000/8011 切换时无需改代码。
- 代理无法连接后端时，前端会提示“本地开发代理无法连接后端服务”，不再只显示笼统的“服务器内部错误”。
- 前端增加低调的流程提示、空会话快捷指令、历史“查看更早消息”、报价命令格式提示、短信验证码提示和上传前文件校验。
- 图片预览链接失效时显示“无法预览”，不再让默认加载失败文案干扰聊天正文。

### 会话记忆落库

- `/ai-assistant/sessions`、`/sessions/{session_id}/history`、`/chat`、`/images/recall` 已切换为 DB-backed 会话存储。
- 历史消息继续保持前端原响应结构：`id/role/content/created_at/metadata`，图片 metadata 会继续清理签名 URL、token、密码和内部权限上下文。
- 原 `storage/quote_assistant_sessions.json` 仅保留为单元测试和临时兜底实现，不作为生产聊天记忆来源。
- 图片撤回会同时更新聊天消息 metadata 和报价图片候选池，保证用户从会话里撤回后不会继续参与报价材料审查。
