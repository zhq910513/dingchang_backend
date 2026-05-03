# 页面审查：登录
更新时间：2026-04-26

本页审查范围：`/login`、`POST /auth/login`、`POST /auth/logout`、会话鉴权依赖。后续登录链路优化必须先满足 `docs/role-permissions.md`。

权限依据：`权限.txt` 主要定义财务域角色边界；登录页自身没有业务数据权限，但它决定登录后返回的主角色、团队范围和默认跳转，因此必须与 `docs/role-permissions.md` 的角色优先级和团队规则保持一致。

## 前端入口

| 项目 | 当前实现 |
| --- | --- |
| 页面 | `D:\Projects\dingchang_frontend_full\src\views\Login.vue` |
| API | `D:\Projects\dingchang_frontend_full\src\api\auth.js` |
| 状态 | `D:\Projects\dingchang_frontend_full\src\store\session.js`，使用 `sessionStorage` 保存 `sessionToken/roleName/userInfo` |
| 登录请求 | `POST /auth/login`，请求体 `{ username, password }` |
| 登出请求 | `POST /auth/logout` |
| 鉴权头 | Axios 拦截器自动注入 `X-Session-Token` |

## 后端入口

| 项目 | 当前实现 |
| --- | --- |
| 路由 | `app/api/v1/auth.py` |
| 服务 | `app/services/auth_service.py` |
| 密码 | `app/core/security.py`，PBKDF2-SHA256，兼容 legacy sha256 |
| Session 表 | `user_session_new` |
| 当前用户依赖 | `app/api/deps.py` |

## 登录业务规则

1. `username/password` 非空，且通过 `LoginIn` 长度约束：`username <= 50`、`password <= 256`。
2. 用户必须存在且 `status == 1`；禁用账号返回 403。
3. 用户必须至少配置一个角色；无角色账号返回 403，避免产生无权限边界的登录态。
4. 密码校验通过后，如果旧密码哈希需要升级，则在登录事务内更新为当前 PBKDF2 格式。
5. 主角色按业务优先级选择：`super_admin > manager > finance > market > sales`。
6. 团队范围来自 `user.team_name` 和 `user.team_names`，登录响应返回 `team_name/team_names`。
7. 成功登录创建一条未过期 session，并清理同用户过期/超量 session；默认同用户最多保留 8 条未过期 session，可通过 `AUTH_MAX_ACTIVE_SESSIONS_PER_USER` 调整。
8. 登出不物理删除 session，改为 `expired=1` 并更新 `last_active_at`，保留登录/登出审计痕迹。

## 登录后跳转规则

| 角色 | 默认首页 | 登录 redirect 限制 |
| --- | --- | --- |
| `market` | `/channels` | 当前仅继承通用限制 |
| `finance` | `/finance` | 禁止跳转到 `/orders*` 和 `/users*` |
| `sales` | `/orders/all` | 禁止跳转到 `/finance*` 和 `/users*` |
| `super_admin`、`manager` | `/orders/all` | 当前仅继承通用限制 |

注意：登录页 redirect 规则与 Dashboard 菜单规则不是完全同一份逻辑，后续如果做前端权限收敛，应统一抽成同一权限模块；本轮不改布局。

## 已发现问题与处理状态

| 编号 | 问题 | 风险 | 处理状态 |
| --- | --- | --- | --- |
| LOGIN-001 | 用户不存在时直接返回，不执行密码哈希校验 | 可能存在用户名枚举时间侧信道 | 已处理：不存在用户和禁用用户会执行固定 PBKDF2 失败成本 |
| LOGIN-002 | 登出旧实现物理删除 session | 丢失登录/登出审计痕迹 | 已处理：改为标记 `expired=1` |
| LOGIN-003 | 本地抽样 24 个用户、1669 条 session、945 条未过期 session | session 长期膨胀，影响在线判断和运维审计 | 已处理：登录成功时清理同用户过期/超量 session |
| LOGIN-004 | 登录响应只返回主角色，不返回权限能力矩阵 | 前端多处自行判断，长期易漂移 | 暂不改响应结构，避免兼容风险；纳入后续权限能力矩阵设计 |
| LOGIN-005 | 登录 redirect 规则与 Dashboard 菜单规则分散 | 角色入口可能漂移 | 后续前端权限模块化，保持布局不变 |
| LOGIN-006 | `ai-assistant` 菜单和后端权限函数不一致 | 已登录用户实际均可访问 AI 接口 | 已在报价助手专项审查中处理：后端入口统一调用权限阀门，业务数据按订单域 ACL 收口 |

## 自检结果

1. 编译检查通过：`python -m py_compile app/schemas/auth.py app/api/v1/auth.py app/services/auth_service.py app/api/deps.py`。
2. 不存在用户：`POST /auth/login` 返回 401，不创建 session；最新抽样耗时约 119.0ms。
3. 错误密码：`POST /auth/login` 返回 401，不创建 session；最新抽样耗时约 151.8ms。
4. 空用户名：`LoginIn` 校验拒绝。
5. 成功登录：使用本地种子管理员账号验证，返回 token、主角色 `super_admin`、团队范围。
6. Session 清理：首次验证时管理员账号未过期 session 从 196 条收敛到 1 条；复测时从 0 条变为 1 条，默认上限 8 条约束持续生效。
7. 登出：新 session 记录保留，`expired=1`。
8. 禁用账号：本地抽样无 `status != 1` 用户，无法用现有数据做真实样本；代码路径已保持 403。

## 本轮状态

登录页后端链路已完成第一轮优化：没有修改数据库字段，没有修改前端样式布局，没有扩大任何角色权限。当前保留事项：登录响应能力矩阵需要跨页面统一设计；AI 助手权限不一致已在报价助手页面专项审查中收口。
