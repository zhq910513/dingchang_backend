# 线上项目拓扑与数据库快照复刻记录（2026-05-03）

## 服务器拓扑

- 公网 IP：`180.76.46.121`
- 后端代码目录：`/data/backend/dingchang_backend`
- 前端代码目录：`/data/frontend/dingchang_frontend`
- 鼎昌 compose 目录：`/data/compose/dingchang`
- 后端当前远程仓库：`git@github.com:zhq910513/dingchang_backend.git`
- 前端当前远程仓库：`git@github.com:zhq910513/dingchang_frontend.git`

## Docker 分布

- `dingchang_mysql`：MySQL 8.0，数据库 `order_system`
- `dingchang_backend`：鼎昌后端容器，连接 `mysql:3306/order_system`
- `edge_nginx`：边缘 Nginx，暴露 `80`
- `gateway_redis`：Redis 7
- `aistock_backend`：另一个后端服务
- `tron-watcher`：历史/其他服务，当前非鼎昌主链路

本次操作中观察到服务器所有业务容器在 `2026-05-03 20:11` 左右处于退出状态；为完成只读数据库导出，先单独启动了 `dingchang_mysql`。快照复刻完成后，为恢复到最初观察到的线上运行状态，使用 `docker start` 启动了 `gateway_redis`、`edge_nginx`、`aistock_backend`、`dingchang_backend`，未执行 `git pull`、未重建镜像、未主动执行线上 SQL 写入。恢复后后端按线上原配置运行，包括原有启动 seed/OCR 轮询逻辑。

最终健康状态：

- `dingchang_backend`：healthy
- `dingchang_mysql`：healthy
- `edge_nginx`：up，80 端口开放
- `gateway_redis`：up
- `aistock_backend`：up

## Nginx 安全修复

侦察日志发现外部扫描请求 `/.env`、`/.git/config` 曾返回 `200`，原因是前端 SPA fallback 未拦截 dotfile。已在 `/data/nginx/conf.d/default.conf` 增加拦截规则并热加载：

- `/.env`：404
- `/.git/config`：404
- `/`：200

配置备份保留在服务器 `/data/nginx/conf.d/default.conf.bak.*`。由于敏感路径曾经暴露过，后续建议轮换 `.env` 中出现过的服务密钥、数据库密码、BOS/BAIDU 密钥。

## 线上数据库表

线上 `order_system` 全库快照包含 47 张表，包括当前 `_new` 表、历史旧表、备份表和 `migration_state`。

主要当前业务表行数：

- `order_new`：5535
- `order_info_new`：5535
- `order_image_new`：18837
- `image_file_new`：18010
- `image_ocr_result_new`：13332
- `ocr_image_cache_new`：13332
- `ocr_task_new`：5061
- `customer_group_new`：574
- `channel_group_new`：276
- `user_new`：23
- `role_new`：5
- `order_fact_new`：0

重要发现：线上 `order_fact_new` 当前为空。后续如果启用投影表列表查询，需要先在本地基于真实快照验证投影回填，再安排线上安全回填。

## 快照文件

远程 dump：

- `/data/backup/dingchang_db_snapshots/order_system_full_20260503201443.sql.gz`

本地 dump：

- `logs/order_system_full_online_20260503201443.sql.gz`

校验：

- SHA256：`39d2c58cd0253b3561f4297a746cbea018dc4c6e79b0d73421930297aefb9ec7`
- 文件大小：17814742 bytes

传输报告：

- `logs/online-db-dump-transfer-20260503201443.json`

## 本地导入校验

导入脚本：

- `scripts/import_mysql_dump_with_pymysql.py`

导入报告：

- `logs/mysql-dump-import-online-20260503201443.json`

导入结果：

- 本地表数：47
- 非空表数：42
- 本地总行数：173214

线上/本地逐表行数比对：

- 报告：`logs/online-local-rowcount-compare-20260503201443.json`
- 线上表数：47
- 本地表数：47
- 线上总行数：173214
- 本地总行数：173214
- 差异表数：0

线上/本地索引比对：

- 报告：`logs/online-local-index-compare-20260503201443.json`
- 线上索引签名数：193
- 本地索引签名数：193
- 差异索引数：0

## OCR 清洗审计

基于本地线上真实快照执行只读审计：

- 报告：`logs/ocr-cleaning-audit-local-online-snapshot-20260503201443.json`
- 订单总数：5535
- 预计清洗变更订单：4140
- 无变化订单：1395

重点风险样例：

- `id_number` 置空样例：15
- `plate_no` 置空样例：20
- `vin` 置空样例：20

下一步需要先人工/脚本复核风险样例，再在本地执行清洗 dry-run 和投影表回填验证。
