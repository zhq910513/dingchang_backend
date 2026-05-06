# 系统基础线审查记录

审查日期：2026-04-28

## 链路范围

系统基础线不对应单一页面，但会影响每个页面接口的固定成本。

| 模块 | 文件 | 影响 |
| --- | --- | --- |
| 配置 | `app/core/config.py` | 生产环境必填项、DB/Redis/BOS/OCR 开关 |
| DB / Redis | `app/core/db.py`, `app/main.py` | 连接池、schema 检查、启动任务、分布式锁 |
| 鉴权依赖 | `app/api/deps.py` | 每个受保护接口的固定 SQL 成本和权限上下文 |
| 密码 / 会话安全 | `app/core/security.py`, `app/services/auth_service.py`, `app/models/session.py` | 登录安全、会话过期、活跃会话清理 |
| 对象存储 / STS | `app/services/storage.py`, `app/services/bce_sts.py`, `/orders/bos-sts` | 上传凭证、图片展示、外部网络调用 |
| OCR 外部服务 | `app/services/baidu_ocr.py`, `app/services/ocr_worker.py` | OCR 超时、重试、任务状态 |

本轮未修改数据库字段，未修改前端布局样式。

## 已发现问题与处理

| 编号 | 问题 | 风险 | 处理 |
| --- | --- | --- | --- |
| SYS-001 | 每个受保护接口鉴权链路固定需要查 session、user、roles | 列表接口即使业务 SQL 已优化，也会被固定鉴权 SQL 成本垫高 | `get_current_session` 改为一次 join 取 session + user + roles，并在请求内复用 |
| SYS-002 | `/orders/bos-sts` 在 STS 缓存未命中时同步请求外部服务 | 外部网络慢时可能阻塞 async event loop，影响同 worker 其他请求 | 改为 `anyio.to_thread.run_sync` 在线程池执行 STS 获取 |
| SYS-003 | 启动期默认 DDL 开关不符合“不改库表/字段”约束 | 无感建表/补列/补索引可能引入结构变更和启动期锁 | 已在后台链路审查中改为默认只检查 schema |

## 本地自检

| 检查 | 结果 |
| --- | --- |
| 鉴权链路 SQL 探针 | 事务内临时 session：`get_current_session -> get_current_user -> get_current_user_context` 合计 `1 SQL` |
| 角色解析 | 样本 `user_id=1`，主角色 `super_admin`，角色集合 `('super_admin',)` |
| `app/api/deps.py` AST + 导入 | 通过 |
| `app/api/v1/orders.py` AST + 导入 | 通过 |
| `git diff --check` | 通过 |

## 当前基础线判断

| 项目 | 结论 |
| --- | --- |
| 生产配置 | `ENV=prod` 时强校验 `SECRET_KEY/DB_PASSWORD/BOS/OCR` 必填项 |
| 密码安全 | PBKDF2-HMAC-SHA256 + pepper；兼容旧 SHA256，并在登录时重哈希 |
| 会话安全 | DB-first 校验；超时标记过期；心跳更新默认 30 秒节流 |
| 外部服务 | OCR 和 STS 均有超时和重试；订单 STS 入口已避免阻塞事件循环 |
| schema 行为 | 默认只检查，不默认执行 DDL |

## 残余风险

1. 鉴权仍坚持 DB-first，不使用 Redis session 读缓存；这是安全优先选择，代价是每个请求至少 1 条鉴权 SQL。
2. BOS STS session policy 当前按 bucket 授权，实际可写范围还依赖云端角色策略；若生产要更细粒度隔离，可进一步按业务前缀收窄云端策略。
3. 若生产使用多 worker 且 Redis 未启用，后台分布式锁会退回单机兼容路径；多 worker 部署建议必须配置 Redis。
