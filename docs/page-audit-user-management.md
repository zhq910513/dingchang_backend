# 账号管理页面/接口审查

更新时间：2026-04-26

页面：前端 `/users`

主要接口：`GET /users`、`POST /users`、`PUT /users/{user_id}`、`DELETE /users/{user_id}`

## 数据抽样

本地库表抽样结果：

| 项目 | 结果 |
| --- | --- |
| 用户总数 | 24 |
| 启用用户 | 24 |
| 角色分布 | `super_admin=1`、`manager=10`、`sales=7`、`finance=4`、`market=2` |
| 无角色账号 | 0 |
| 多角色账号 | 0 |
| 主要层级 | `dingchang_admin` 直接子账号 11 个；`xmc` 子账号 6 个；`wangliang` 子账号 5 个；`测试经理` 子账号 1 个 |
| 历史未过期会话 | 多个账号存在几十到上百条过期时间意义上的陈旧 `expired=0` 会话 |

## 权限结论

| 角色 | 账号管理范围 |
| --- | --- |
| `super_admin` | 可查看/管理所有非超管账号，不可管理自己 |
| `manager` | 只可查看/管理自己创建的 `sales/finance/market` 账号，不可管理经理/超管/自己 |
| `sales`、`finance`、`market` | 不可进入账号管理后端能力 |

团队分配规则：

| 目标角色 | 后端强制规则 |
| --- | --- |
| `manager` | 必须至少分配一个团队，默认团队必须在团队集合内，仅超管可创建/维护 |
| `sales`、`finance`、`market` | 必须且只能分配单团队；经理只能分配自己团队集合内的团队 |

## 发现的问题

1. 鉴权依赖中的 `select(User)`、`select(UserSession)` 受模型关系 `lazy="selectin"` 影响，单次普通查询会暗中触发 22 条 SQL；`select(Role)` 在账号创建中会触发 215 条 SQL。这会拖慢所有接口，不只是账号管理。
2. 后端原先只校验团队名称是否合法，没有校验团队是否属于当前经理可管理范围。直接调用接口时，经理可以尝试把子账号创建/迁移到其他团队。
3. `PUT /users/{id}` 无法区分“字段未传”和“显式传空”，直接调用密码更新接口时存在误清空团队字段的风险。
4. 超管原先只管理自己直接创建的账号。实际业务中经理创建的下级账号如果异常，超管没有兜底入口，不符合超管定位。
5. 前端账号列表接口没有传 `page/page_size`，后端默认 20 条；超管扩大为全量非超管视角后会漏显示超过默认页大小的数据。

## 已完成优化

1. 鉴权和账号服务中的关键 ORM 查询增加 `lazyload("*")`，账号创建角色查询改为 `select(Role.id)` 投影，避免关系树被隐式加载。
2. 用户列表继续保持 `count -> page ids -> projection rows` 的轻量读模型；非超管列表不再计算在线状态，超管在线状态只扫描最近在线窗口内的会话。
3. 服务端强制账号团队规则，经理跨团队创建/迁移子账号直接拒绝。
4. 更新接口按 `exclude_unset` 传参，保留未传字段，避免密码更新误清空团队。
5. 超管账号管理范围调整为所有非超管账号，保留不可管理自己的底线。
6. 前端 `listUsers` 支持 `page/page_size`，账号页固定请求 `page_size=100`，不改布局样式。

## 自检结果

| 测试项 | 结果 |
| --- | --- |
| 后端编译 | `python -m py_compile app/api/deps.py app/api/v1/users.py app/services/auth_service.py app/services/users_service.py app/schemas/user.py` 通过 |
| 前端构建 | `npm run build` 通过 |
| ORM 隐式 SQL | `select(User).options(lazyload("*"))=1 SQL`；`select(UserSession).options(lazyload("*"))=1 SQL`；`select(Role.id)=1 SQL` |
| 账号列表 | 超管全量非超管：23 条，3 SQL，约 19ms；经理 `xmc`：6 条，3 SQL，约 12ms |
| 禁止角色 | `sales` 调用账号列表：0 SQL 直接拒绝 |
| 经理跨团队更新 | `xmc` 将子账号 `zlf` 改到九江团队：403/PermissionError，原团队未变化 |
| 经理跨团队创建 | `xmc` 创建九江团队业务：403/PermissionError，临时账号未落库 |
| 单团队角色缺团队 | 创建 `sales` 不传团队：400/ValueError |
| 经理缺团队集合 | 超管创建 `manager` 不传团队集合：400/ValueError |
| 超管兜底 | 超管对经理创建的间接子账号执行无改动更新：允许 |
| 更新字段保留 | `UserUpdateIn(password="123456")` 的 `exclude_unset` 仅包含 `password`；空更新字段集为空 |

## 残余风险

1. 账号页前端仍然没有分页控件；当前本地只有 24 个用户，`page_size=100` 足够。若账号超过 100，需要在不改变既有视觉风格的前提下补分页交互。
2. 历史陈旧会话仍有大量 `expired=0` 数据。登录链路会清理当前登录用户，账号列表在线状态也已只看最近窗口；后续可以加运维清理任务，但不应在页面请求中批量更新历史数据。
3. 角色表当前没有多角色账号；主角色优先级逻辑保留。若未来启用多角色，需要重新审查账号管理列表过滤是否按“主角色”还是“任一角色”解释。
