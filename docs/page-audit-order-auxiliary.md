# 页面审查：订单辅助接口 / 下拉 / 上传

审查日期：2026-04-26

## 页面与接口

涉及页面：订单列表筛选、订单创建、订单详情、订单导入、报价助手上传。

后端接口：

| 接口 | 用途 |
| --- | --- |
| `GET /orders/customer-groups` | 订单模块 legacy 客户下拉 |
| `GET /orders/channel-groups` | 订单模块 legacy 渠道下拉 |
| `GET /orders/teams` | 团队筛选下拉 |
| `GET /orders/salespersons` | 业务员筛选下拉 |
| `GET /orders/ocr-tasks` | 导入页 OCR 任务列表 |
| `GET /orders/bos-sts` | 前端直传 BOS 临时凭证 |
| `POST /orders/bos-upload` | 后端代理上传 BOS |

本轮未修改数据库字段，未修改前端布局样式。

## 权限边界

| 接口 | 当前规则 |
| --- | --- |
| 客户/渠道 legacy 下拉 | 已登录且有订单域读权限；只返回未删除数据 |
| 团队下拉 | `super_admin` 全团队；`manager` 所辖团队；`sales/finance/market` 单团队 |
| 业务员下拉 | `super_admin` 全量销售；`manager` 所辖团队销售；`sales` 仅本人；`finance/market` 单团队销售 |
| OCR 任务列表 | 继承订单域 ACL；`market` 禁止；`finance` 仅完成订单 |
| BOS STS | 普通订单写权限；`finance/market` 禁止 |
| BOS 代理上传 | `sales/manager/super_admin` 可上传订单图；`finance` 仅可上传 `related` 备用图；`market` 禁止 |

## 本地数据抽样

| 项目 | 抽样结果 |
| --- | --- |
| 客户 legacy 下拉 | 1 SQL，`432` 条 |
| 渠道 legacy 下拉 | 1 SQL，`223` 条 |
| 经理业务员下拉 | 1 SQL，样本 `2` 条 |
| 财务 OCR 任务列表 | 1 SQL，样本 `50` 条，未完成订单 `0` 条 |

## 已发现问题与处理

| 编号 | 问题 | 风险 | 处理 |
| --- | --- | --- | --- |
| ORDER-AUX-001 | 客户/渠道 legacy 下拉使用 ORM 实体查询 | 模型关系默认 `selectin` 时可能额外加载 creator/orders 等关系，首屏下拉变慢 | 改为列投影，只取 `id/code/name` |
| ORDER-AUX-002 | OCR 任务列表直接返回任务实体 | 虽当前无关系，但实体加载不是必要开销 | 改为列投影，只取列表展示字段 |
| ORDER-AUX-003 | 财务角色 OCR 任务列表仅按团队过滤 | 与“财务只能读完成订单”的订单域边界不完全一致 | OCR 任务 ACL 中补充 `Order.is_finished = true` |
| ORDER-AUX-004 | BOS 代理上传无单文件大小上限 | 异常大文件会占用 worker 读流、MD5 计算和对象存储线程 | 增加 20MB 单文件上限，超出返回 `413` |

## 自检结果

| 检查 | 结果 |
| --- | --- |
| `app/api/v1/orders.py` AST + 导入 | 通过 |
| `git diff --check` | 通过 |
| 客户/渠道 legacy 下拉 SQL 数 | 均为 1 SQL |
| 业务员下拉 SQL 数 | 1 SQL |
| 财务 OCR 任务越界探针 | 样本 50 条中未完成订单 0 条 |

## 残余风险

1. 当前前端主要使用 `/customer-channel/customers|channels` 分页下拉，`/orders/customer-groups|channel-groups` 属于 legacy 兼容接口；已优化但不建议继续扩大使用。
2. 20MB 上传上限按当前图片/OCR场景设定；如果未来允许 PDF 或高清视频，应单独设计文件类型、大小和异步处理策略。
