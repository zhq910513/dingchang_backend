# 报价助手页面接口审查与优化记录

审查日期：2026-04-26

## 页面与接口

前端页面：`/ai-assistant`

后端接口：

| 接口 | 用途 |
| --- | --- |
| `GET /ai-assistant/sessions` | 当前用户会话列表 |
| `POST /ai-assistant/sessions` | 创建当前用户会话 |
| `DELETE /ai-assistant/sessions/{session_id}` | 删除当前用户会话 |
| `GET /ai-assistant/sessions/{session_id}/history` | 当前用户会话历史 |
| `POST /ai-assistant/chat` | 规则引擎聊天、订单/车主/材料/OCR/报价查询 |
| `GET /ai-assistant/health` | 模块健康检查 |

本轮未修改数据库字段，未修改前端布局样式。

## 权限结论

报价助手允许系统已知角色进入：`super_admin/manager/sales/finance/market`。它只负责交互入口，不新增独立业务数据权限。

| 角色 | 订单/OCR 数据范围 |
| --- | --- |
| `super_admin` | 全量 |
| `manager` | 所辖团队 |
| `sales` | 本人作为业务员的订单 |
| `finance` | 本人单团队 |
| `market` | 本人单团队 |

会话数据按 `owner_user_id` 隔离。订单、车主、材料状态、OCR 任务、报价定位订单时全部继承订单域 ACL；越权订单和越权 OCR 任务统一表现为未命中，不回显目标数据。

## 本地数据抽样

| 项目 | 抽样结果 |
| --- | --- |
| 角色分布 | `finance=4`、`manager=10`、`market=2`、`sales=7`、`super_admin=1` |
| 订单总数 | `3177` |
| OCR 任务总数 | `2919` |
| 销售样本 | `xundl(id=22, 九江团队)`，本人订单 `3213`，跨人订单 `3219` |
| 经理样本 | `xmc(id=7, 南昌团队)`，本团队订单 `3219`，跨团队订单 `3218` |

## 已发现问题与处理

| 编号 | 问题 | 风险 | 处理 |
| --- | --- | --- | --- |
| AI-001 | 前端允许所有角色进入，但后端权限函数仍写成仅超管，且 API 未调用该函数 | 权限口径漂移，后续改动容易误收或误放 | `require_ai_assistant_access` 统一为已知角色可进入，AI 会话/聊天接口全部调用该阀门 |
| AI-002 | 聊天服务按订单号、车牌、手机号查订单时没有带当前用户角色/团队上下文 | 可绕过订单列表/详情 ACL 直接查跨团队订单 | API 将 `current_user_id/role_name/team_names` 写入 JSON 安全上下文，服务层所有订单定位统一套 ACL |
| AI-003 | OCR 任务按任务号查询只看 `task_id`，没有校验任务关联订单是否可读 | 可通过任务号探测跨团队订单任务状态 | `_db_get_ocr_task` 通过 `scope_type=order/scope_id` 做订单 ACL 校验，越权返回未命中 |
| AI-004 | OCR 任务权限校验若直接加载完整订单，会多查图片/详情 | 任务状态查询不必要地放大 SQL | 新增轻量订单 ACL 探针，只查 `Order.id`；需要展示摘要时再加载完整订单 |
| AI-005 | 聊天入参缺少长度约束 | 空消息/超长消息可能造成无意义处理或日志膨胀 | `AiChatIn.message` 增加 `1..2000` 长度约束，`order_id >= 1`，会话标题最大 100 |

## 自检结果

| 检查 | 结果 |
| --- | --- |
| AST 语法校验 | 通过：`ai_assistant.py`、`ai_assistant_service.py`、`ai_assistant.py(schema)`、`access_control.py` |
| `python -B` 导入校验 | 通过 |
| `py_compile` | 本地 `__pycache__` 写入权限拒绝，改用不写字节码的 AST + import 校验 |
| 权限阀门 | 已知 5 类角色允许，未知角色拒绝 |
| Schema | 合法消息通过；空消息拒绝 |
| 销售订单 ACL | 本人订单 `3213`：4 SQL 命中；跨人订单 `3219`：1 SQL 返回空 |
| 销售订单自然语言查询 | 本人订单返回 `success`；跨人订单返回 `empty` |
| 销售 OCR ACL | 本人 OCR 任务 `2914`：2 SQL 命中；跨人 OCR 任务：2 SQL 返回空 |
| 经理订单 ACL | 本团队订单 `3219`：4 SQL 命中；跨团队订单 `3218`：1 SQL 返回空 |

## 残余风险

1. 报价助手当前是规则引擎 + JSON 文件会话存储，适合当前数据量；如果后续会话数量增大，应迁移到数据库表或 Redis，并保留 `owner_user_id` 隔离。
2. 订单查询会返回完整订单 payload（动态字段、订单信息、图片槽位）。当前已按订单 ACL 收口；如果业务希望 `finance/market` 在 AI 助手里只看财务子集，需要单独设计字段级脱敏策略。
3. 本轮没有改变前端入口和布局；若后续需要隐藏某些角色的 AI 菜单，应与后端权限矩阵同时调整。
