# 启动期与后台任务链路审查记录

审查日期：2026-04-26

## 链路范围

本轮审查不对应单一前端页面，但会直接影响列表查询体验。

| 链路 | 入口 | 对列表性能的影响 |
| --- | --- | --- |
| 启动期 schema 检查 | `app/main.py` lifespan | 若默认执行 DDL，会在启动时抢占数据库元数据锁和连接资源 |
| 订单查询投影补偿 | `ORDER_FACT_BACKFILL_ENABLED=1` | 扫描订单表并回填 `order_fact_new`，若多 worker 同时执行会放大数据库压力 |
| OCR 任务轮询 | `OCR_POLL_ENABLED=1` | OCR 完成后写订单动态字段和查询投影，投影不一致会导致列表搜索结果漂移 |
| OCR worker | `app/services/ocr_worker.py` | 订单识别完成后同步列表搜索字段 |

本轮未修改数据库字段，未修改前端布局样式。

## 本地数据抽样

| 项目 | 抽样结果 |
| --- | --- |
| 订单总数 | `3177` |
| 订单查询投影数 | `3177` |
| 缺失查询投影 | `0` |
| OCR 任务数 | `2919` |
| 抽样订单 | `3219` |
| 查询投影字段 | `engine_no, first_register_date, id_number, owner_name, plate_no, vehicle_model, vin` |

## 已发现问题与处理

| 编号 | 问题 | 风险 | 处理 |
| --- | --- | --- | --- |
| BKG-001 | OCR worker 内部维护了一份独立 `OrderFact` 拼装逻辑 | OCR 结果、人工保存、启动补偿三条链路可能生成不同投影，导致列表搜索命中不一致 | OCR worker 改为调用统一的 `sync_order_fact_from_dynamic_data` |
| BKG-002 | 订单投影启动补偿不加锁 | 多 worker 部署时每个进程都可能扫订单表并回填，启动期挤压列表查询资源 | 新增 `STARTUP_LOCK_FACT_BACKFILL_KEY` 分布式锁；有 Redis 时单实例执行，无 Redis 时保留单机兼容 |
| BKG-003 | 启动期默认自动建表、补列、补索引 | 与“不改库表/字段”的约束冲突，也可能在启动时触发 DDL 锁 | 默认改为只做 schema 检查；`AUTO_CREATE_TABLES/AUTO_ADD_COLUMNS/AUTO_ADD_INDEXES` 必须显式设为 `1` 才执行 |

## 当前策略

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTO_SCHEMA_CHECK` | `1` | 启动时检查模型与数据库差异并输出日志 |
| `AUTO_CREATE_TABLES` | `0` | 默认不建表 |
| `AUTO_ADD_COLUMNS` | `0` | 默认不补字段 |
| `AUTO_ADD_INDEXES` | `0` | 默认不补索引 |
| `ORDER_FACT_BACKFILL_ENABLED` | `1` | 默认补偿缺失查询投影 |
| `STARTUP_LOCK_FACT_BACKFILL_KEY` | `dingchang:startup:order_fact_backfill` | 多 worker 下保护投影补偿只跑一个实例 |
| `STARTUP_LOCK_FACT_BACKFILL_TTL_SECONDS` | `600` | 投影补偿锁 TTL |

## 自检结果

| 检查 | 结果 |
| --- | --- |
| `app/main.py` AST + 导入 | 通过 |
| `app/services/ocr_worker.py` AST + 导入 | 通过 |
| `app/services/order_fact_service.py` AST + 导入 | 通过 |
| 默认 schema 开关探针 | `AUTO_CREATE_TABLES=False, AUTO_ADD_COLUMNS=False, AUTO_ADD_INDEXES=False` |
| 本地投影完整性 | `orders=3177, order_facts=3177, missing_order_facts=0` |
| OCR 投影重复实现检查 | 旧 `_upsert_order_fact_from_dynamic_data` / `OrderFact` 直接引用已移除 |

## 残余风险

1. 无 Redis 且多 worker 启动时，投影补偿仍会按单机兼容路径执行；生产多 worker 建议配置 Redis，确保启动锁生效。
2. 默认不执行 DDL 后，如果数据库结构缺失，应用会在启动检查中暴露差异，但不会自动修复；这符合当前“不改库表/字段”的约束。
