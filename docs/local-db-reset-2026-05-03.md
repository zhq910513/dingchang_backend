# 本地数据库清理记录（2026-05-03）

## 执行目标

- 清理本地 `order_system` 数据库，为导入线上真实快照做准备。
- 删除当前项目不再使用的旧表。
- 清空当前 ORM 使用表，并通过 `TRUNCATE` 重置自增。
- 按当前 SQLAlchemy 模型补齐表与索引，不修改字段定义。

## 安全保护

- 执行脚本：`scripts/local_db_reset_for_online_snapshot.py`。
- 执行确认口令：`RESET_LOCAL_DB_FOR_ONLINE_SNAPSHOT`。
- 脚本默认只允许 `DB_HOST=127.0.0.1`、`localhost`、`::1`。
- 本次 `.env` 指向 `127.0.0.1:3306/order_system`。

## 删除的旧表

- `channel_group`
- `customer_group`
- `field_config`
- `field_group`
- `field_group_field`
- `finance_record`
- `image_file`
- `image_ocr_result`
- `ocr_image_cache`
- `ocr_task`
- `order`
- `order_image`
- `order_info`
- `role`
- `user`
- `user_role`
- `user_session`

## 保留并清空的当前表

- `channel_group_new`
- `customer_group_new`
- `field_config_new`
- `field_group_field_new`
- `field_group_new`
- `finance_record_new`
- `image_file_new`
- `image_ocr_result_new`
- `ocr_image_cache_new`
- `ocr_task_new`
- `order_fact_new`
- `order_image_new`
- `order_info_new`
- `order_new`
- `order_slot_result_new`
- `role_new`
- `user_new`
- `user_role_new`
- `user_session_new`

## 自检结果

- 清理前数据库表数：36。
- 删除旧表：17。
- 清空当前表：19。
- 清理后数据库表数：19。
- 清理后非空表数量：0。
- 清理后多余表数量：0。
- 清理后缺失模型表数量：0。
- 清理后缺失模型索引签名数量：0。
- 报告文件：`logs/local-db-reset-20260503.json`。

## 注意事项

- 当前库已完全清空，登录账号、角色、字段配置、订单、图片、OCR 缓存全部为空。
- 如果在导入线上快照前启动后端，`.env` 中 `AUTO_SEED_AUTH=1`、`AUTO_SEED_FIELDS=1` 可能自动写入默认账号/字段配置；为了保持纯净快照，建议先导入线上数据，再启动后端做验证。
