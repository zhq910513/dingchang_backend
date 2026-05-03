# 页面审查：订单创建 / 导入 / OCR
更新时间：2026-04-26

本页审查范围：`/orders/import`、`/orders/create`，以及 `POST /orders/draft`、`POST /orders/finalize`、`GET /orders/ocr-tasks`、OCR worker。

## 权限边界

| 角色 | 创建/导入 | finalize 图片/动态字段 | OCR 任务 |
| --- | --- | --- | --- |
| `super_admin` | 允许 | 允许 | 可看全量 |
| `manager` | 允许辖区业务员订单 | 允许辖区订单 | 只看辖区订单任务 |
| `sales` | 只允许自己的订单 | 只允许自己的订单 | 只看自己的订单任务 |
| `finance` | 禁止普通创建/导入 | 仅允许已完成订单的 `related` 备用图 | 可看单团队订单任务 |
| `market` | 禁止创建/导入/finalize | 禁止 | 禁止 |

本轮未扩大写权限。`finance` 在 finalize 中仍只能操作 `related`，不能改业务员、客户、渠道、动态字段或 `order_info`。

## 已发现问题与处理状态

| 编号 | 问题 | 风险 | 处理状态 |
| --- | --- | --- | --- |
| ORDER-IMPORT-001 | `/orders/draft` 旧逻辑未前置校验客户/渠道必填和存在性 | 直接调用 API 可留下不完整草稿订单 | 已处理：draft 阶段强制客户/渠道必填并校验存在 |
| ORDER-IMPORT-002 | OCR worker 加载订单时受模型默认 `lazy=selectin` 影响，可能加载无关关系 | 后台 OCR 任务 DB 查询膨胀 | 已处理：worker 使用 `lazyload("*")`，只白名单加载 `images.image_file` |
| ORDER-IMPORT-003 | finalize 对每张图片逐个 upsert `ImageFile`、逐个查 `OrderImage` | 多图 related 上传时存在 N+1 写路径 | 当前 related 上限较小，暂保留；后续若多图大量上传，可批量预取 storage_key |
| ORDER-IMPORT-004 | OCR 任务列表按角色过滤正确，但 super_admin 全量列表随任务量增长会扫描更多任务 | 任务量大时任务抽屉打开变慢 | 当前 2919 条任务仍可接受；后续可加 `(scope_type, id)` 或归档任务 |

## 已实施优化

1. `app/api/v1/orders.py`：`create_order_draft` 前置 `_ensure_required_customer_channel`，并校验 `customer_group_id/channel_group_id` 存在。
2. `app/services/ocr_worker.py`：OCR worker 查询订单时显式 `lazyload("*")`，避免加载非 OCR 所需关系。
3. 保持 `order_fact_new` 同步：draft/create/update/finalize/OCR worker 都会同步订单搜索投影，确保列表筛选能命中新字段。

## 自检结果

1. 编译检查通过：`python -m py_compile app/api/v1/orders.py app/services/ocr_worker.py`。
2. `/orders/draft` 缺少客户/渠道：返回 400，订单总数保持 3177 不变。
3. `market` 调用 `/orders/draft`：返回 403，订单总数保持 3177 不变。
4. OCR 任务列表抽样：`super_admin` 50 条约 21.52ms；`manager` 约 6.20ms；`sales` 约 12.99ms；`finance` 约 4.95ms。

## 残余风险

导入链路是“两段式”：先 draft，再 finalize。若 finalize 因上传元数据错误失败，会保留一张未完成草稿订单。这符合当前业务入口“未完成订单继续补录”的设计；后续如业务希望自动回滚草稿，需要新增显式草稿清理策略，而不是在失败时盲删订单。
