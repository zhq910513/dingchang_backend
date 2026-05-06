# 角色权限矩阵与审查基线

更新时间：2026-04-26

本文档是逐页接口/业务逻辑优化前的权限基线。后续任何页面优化都必须先对照本文档，不能因为性能优化扩大角色权限范围。前端菜单仅作为体验层提示，后端鉴权才是最终约束。

权限来源：项目根目录 `权限.txt` 是当前财务域权限的主要业务依据；本文档同时对照当前前端菜单、后端路由和服务层实现。若 `权限.txt`、前端、后端存在冲突，先记录为审查项，不直接扩大或收缩权限。

## 角色定义

| 角色 | 业务定位 | 团队范围 |
| --- | --- | --- |
| `super_admin` | 超级账号 | 可访问全部系统团队；查询不传团队时代表全量 |
| `manager` | 经理账号 | 可访问自己 `team_name/team_names` 覆盖的多个团队 |
| `sales` | 业务账号 | 必须且只能绑定一个团队；订单范围收敛到自己作为业务员的订单 |
| `finance` | 财务账号 | 必须且只能绑定一个团队；财务查询和写入锁定该团队 |
| `market` | 市场账号 | 必须且只能绑定一个团队；主要是市场/渠道/客户维护和只读查看 |

主角色优先级：`super_admin > manager > finance > market > sales`。登录响应当前只返回主角色 `role_name`，未返回全量角色列表。

## 页面级入口

| 页面 | 前端路由 | 主要接口 | 当前前端入口规则 |
| --- | --- | --- | --- |
| 登录 | `/login` | `POST /auth/login` | 公共页面 |
| 账号管理 | `/users` | `/users` | `super_admin`、`manager` 可进入 |
| 渠道管理 | `/channels` | `/customer-channel/channel-groups` | 菜单允许进入，写权限靠页面按钮和后端 |
| 客户管理 | `/customers` | `/customer-channel/customer-groups` | 菜单允许进入，写权限靠页面按钮和后端 |
| 订单列表 | `/orders/all`、`/orders/finished`、`/orders/unfinished` | `/orders` | `finance` 前端禁入 |
| 订单导入/创建 | `/orders/import`、`/orders/create` | `/orders/draft`、`/orders/finalize`、`/orders` | `finance`、`market` 前端禁写 |
| 订单详情 | `/orders/:id` | `/orders/{id}`、`PUT /orders/{id}`、`PATCH /orders/{id}/status` | `finance` 前端禁入订单模块 |
| 财务列表 | `/finance` | `/orders`、`/finance/orders/summary`、`/finance/orders/export` | `sales` 前端禁入 |
| 财务详情 | `/finance/orders/:id` | `/finance/orders/{id}`、财务状态/退回接口 | `sales` 前端禁入 |
| 报价助手 | `/ai-assistant` | `/ai-assistant/*` | 前端当前允许所有已登录角色 |

## 后端权限基线

## 基于 `权限.txt` 的合理性评估

| 结论 | 项目 | 判断 |
| --- | --- | --- |
| 合理 | `super_admin` 财务全量、可跨团队 | 符合系统兜底管理角色定位，但导出/写入应保留操作审计 |
| 合理 | `manager` 财务权限受团队集合限制 | 符合团队负责人看数和处理本团队单据的场景 |
| 需确认 | `manager` 允许财务写入、导出 | `权限.txt` 明确允许；从风控角度这比“只看本团队”更高风险，后续不能无审计扩大 |
| 合理 | `finance` 必须单团队，所有财务读写锁定该团队 | 清晰、低歧义，后端应持续强制单团队 |
| 需确认 | `market` 可看财务汇总/详情但只读 | `权限.txt` 明确允许；若利润/应收应付属于敏感财务数据，需要业务确认市场只读可见范围 |
| 合理 | `sales` 禁止访问 `/finance/*` | 职责隔离明确，当前前后端方向一致 |
| 冲突/迁移 | `权限.txt` 提到 `/finance/orders` 列表 | 当前后端没有该列表接口，前端财务列表复用 `/orders?is_finished=true`，财务汇总/导出/详情仍在 `/finance/*` |
| 冲突/迁移 | `权限.txt` 提到 `/finance/bos-sts`、`/finance/bos-upload`、`/finance/finalize` | 当前前端和后端实际复用 `/orders/bos-upload`、`/orders/finalize`，并在订单域里对 `finance` 做 related 图片限制 |
| 不完整 | 客户/渠道域没有完整写权限说明 | 当前代码仅禁止 `finance` 新增/删除，允许 `market` 编辑；`sales` 新增/删除是否合理需在客户/渠道页面审查 |
| 已处理 | 报价助手权限 | 统一为所有系统已知角色可进入；会话按用户隔离，订单/OCR 数据继续按订单域 ACL 收口 |

专业判断：`权限.txt` 对财务域的核心分层是合理的，但它不是完整系统权限矩阵。它缺少订单域、客户/渠道域、账号域、AI 助手域的完整规则，也包含已经迁移的接口路径。因此后续优化应采用“`权限.txt` 财务规则优先 + 当前代码真实行为对照 + 页面专项确认”的方式推进。

### 登录与会话

| 接口 | 权限 |
| --- | --- |
| `POST /auth/login` | 公共；仅允许 `status=1` 且至少配置一个角色的用户登录；禁用/无角色返回 403 |
| `POST /auth/logout` | 需要有效 `X-Session-Token` |
| 其他接口 | 需要有效 `X-Session-Token`，并在依赖中校验用户启用状态 |

### 账号管理

| 角色 | 当前权限 |
| --- | --- |
| `super_admin` | 可管理所有非超管账号，不可管理自己；可创建/编辑 `manager/sales/finance/market` 的团队归属 |
| `manager` | 可管理自己创建的 `sales/finance/market` 账号，不可管理自己；只能给子账号分配自己团队集合内的团队 |
| `sales`、`finance`、`market` | 不可管理账号 |

账号团队规则：

| 目标角色 | 团队规则 |
| --- | --- |
| `manager` | 必须配置至少一个 `team_names`，`team_name` 必须落在 `team_names` 内；仅 `super_admin` 可创建/维护经理团队 |
| `sales`、`finance`、`market` | 必须配置且只能配置一个 `team_name`；`manager` 只能分配自己 `team_name/team_names` 覆盖的团队，`super_admin` 可分配任一系统团队 |

### 订单域

| 角色 | 读 | 写/创建/编辑 | 关键范围 |
| --- | --- | --- | --- |
| `super_admin` | 可读全部 | 可写 | 全量 |
| `manager` | 可读所辖团队 | 可写所辖团队订单 | `team_name/team_names` 交集 |
| `sales` | 可读自己的订单 | 可写自己的未完成订单；不能退回已完成订单 | `salesperson_id == 当前用户` |
| `finance` | 仅可读取本团队已完成订单；前端禁入普通订单模块，财务页复用已完成列表 | 订单域普通写入禁止；仅财务相关图片走受限路径 | 单团队 |
| `market` | 可读本团队订单 | 订单域写入禁止 | 单团队 |

### 财务域

| 角色 | 查看/汇总/详情 | 导出 | 回款/返点/退回 |
| --- | --- | --- | --- |
| `super_admin` | 允许 | 允许 | 允许 |
| `manager` | 允许，限所辖团队 | 允许，限所辖团队 | 允许，限所辖团队 |
| `finance` | 允许，限单团队 | 允许，限单团队 | 允许，限单团队 |
| `market` | 允许，限单团队 | 禁止 | 禁止 |
| `sales` | 禁止 | 禁止 | 禁止 |

### 图片与 BOS 链路

| 角色 | 获取 STS / 代理上传 | 订单图片绑定 | 图片读取 |
| --- | --- | --- | --- |
| `super_admin` | 允许，可上传所有合法 slot | 允许，所有订单 | 全量订单 ACL 内读取，返回短期签名 URL |
| `manager` | 允许，可上传所有合法 slot | 允许，所辖团队订单 | 所辖团队订单 ACL 内读取，返回短期签名 URL |
| `sales` | 允许，可上传所有合法 slot | 允许，本人订单 | 本人订单 ACL 内读取，返回短期签名 URL |
| `finance` | 仅允许 `related` | 仅已完成订单的 `related` | 仅本团队已完成订单 ACL 内读取，返回短期签名 URL |
| `market` | 禁止 | 禁止 | 本团队订单只读 ACL 内读取，返回短期签名 URL |

图片链路安全基线：

1. `storage_key` 必须符合 B1 规则：`prefix/md5[0:2]/md5[2:4]/md5.ext`，且 prefix 必须匹配 slot。
2. 前端不得把展示 URL 作为图片真源落库；后端只信任 `storage_key/md5/etag/size/content_type/original_name`。
3. 订单详情、AI 助手、OCR 均禁止回退 BOS 公开直链；签名失败应暴露为链路失败，而不是静默降级。
4. STS 仅允许 `cert/*`、`idcard/*`、`dl/*`、`backup/*` 的 `READ/WRITE`，禁止 bucket `LIST` 和非法前缀写入。
5. 云桶当前仍允许已知 key 匿名读取，这是云侧 ACL 残余风险；改私有前必须先确认生产图片展示均通过签名 URL。

### 客户/渠道域

| 操作 | 当前后端规则 |
| --- | --- |
| 下拉选项 `GET /customer-channel/customers|channels` | 已登录用户可读，只返回未删除数据 |
| 管理列表 | `super_admin`、`manager`、`market`、`finance`、`sales` 可读；仅 `super_admin` 可查看已删除 |
| 新增 | `super_admin`、`manager`、`market`、`sales`；`finance` 禁止 |
| 编辑 | `super_admin`、`manager`、`market` 可编辑全部未删除记录；`sales` 仅可编辑自己创建的未删除记录 |
| 删除 | `super_admin`、`manager`、`market` 可软删除全部未删除记录；`sales` 仅可软删除自己创建的未删除记录；`finance` 禁止 |

### 字段配置

| 操作 | 当前后端规则 |
| --- | --- |
| `GET /field-config/form-config` | 已登录用户按角色获取可见/可编辑配置 |
| `GET /field-config` | `super_admin`、`manager` 可查看原始字段配置 |
| `PUT /field-config/{module}/{field_name}` | `super_admin`、`manager`；`view_roles/edit_roles` 只能写入系统已知角色 |

### 报价助手

当前结论：报价助手允许所有系统已知角色进入，但它不是独立的数据权限入口。所有会话按当前用户隔离；所有订单、车主、材料、OCR 任务和报价定位到订单后，必须继续按订单域 ACL 过滤。

| 位置 | 当前行为 |
| --- | --- |
| 前端菜单 | 所有已登录角色可进入 |
| `access_control.require_ai_assistant_access` | 允许 `super_admin/manager/sales/finance/market`；未知角色拒绝 |
| `app/api/v1/ai_assistant.py` | 所有会话接口和聊天接口统一调用权限阀门；聊天上下文只传 JSON 安全字段：当前用户、主角色、团队集合、订单 ID、图片列表 |
| `app/services/ai_assistant_service.py` | 订单查询、车主查询、材料状态、OCR 任务、报价定位订单全部套用订单域 ACL；越权订单按未命中处理 |

角色数据范围继承订单域：`super_admin` 全量；`manager` 限所辖团队；`sales` 限本人订单；`finance/market` 限单团队。OCR 任务通过 `scope_type=order/scope_id` 反查订单 ACL，不能单凭任务号绕过订单权限。

## 优化纪律

1. 每个页面优化前先列出页面、接口、角色范围和数据范围。
2. 任何接口优化不得扩大角色可访问的数据集合。
3. 不修改数据库字段，不修改前端布局样式。
4. 启动期默认只做 schema 检查，不自动建表、补列或补索引；如确需 DDL，必须显式打开环境变量并单独记录。
5. 每个页面优化后必须更新对应页面审查文档，记录问题、改动、自检和残余风险。
6. 后台任务虽然不直接对应页面，也必须记录数据范围、并发策略和对列表查询资源的影响。
