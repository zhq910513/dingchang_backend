# dingchang_backend

# tree /A /F > tree-src.txt

# # Select-String -Path .\app\**\*.py -Pattern "order_import_task|import_task|ocr_record|utils\.baidu_ocr|bos_storage|baidu_ocr_cards" -List


### 1) 安装依赖
pip install -r requirements.txt


### 2) 开发（直接跑）
uvicorn app.main:app --reload


### 3) 生产（高并发 + 稳定）—— gunicorn + uvicorn worker
gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w 4 \
  --threads 4 \
  --bind 0.0.0.0:8000 \
  --max-requests 10000 \
  --max-requests-jitter 1000 \
  --timeout 60


### 默认配置在 app/core/config.py，也可用 .env 覆盖：
ENV=dev
DB_USER=root
DB_PASSWORD=root123456
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=order_system

REDIS_URL=redis://127.0.0.1:6379/0

SESSION_TIMEOUT_SECONDS=7200
LOG_LEVEL=INFO

### 获取表单配置：
GET /field-config/form-config?module=order

### 日志
输出到控制台与 logs/app.log
采用滚动策略，总量约 50MB（10MB * (4 备份 + 1 当前)）



# 代码审查协议（必须严格执行） ## 目标（高优先级） 1. 审查数据库设计字段是否合理（含类型、长度、默认值、索引、约束、可空性等）。 2. 审查 数据库 ↔ 后端 ↔ 前端 字段命名一致、值类型一致、空值安全。 3. 重点排查：前端和后端是否对齐（最高优先级）。 4. 核查完不需要的文件可以删除 ## 全量覆盖（一个不漏） - 前后端项目中所有文件都要逐一审查。 - 前端样式不能更改,实在要更改也要先咨询我 - 最终输出必须包含： 1) 全量整改点清单（按文件归类、编号不重复） 2) 数据库↔后端↔前端字段对照表（命名/类型/可空/默认值/校验） 3) 未使用/多余文件列表与删除建议（同步更新到进度面板） ## 时间与格式统一（强制） - 所有时间统一转换为北京时间（Asia/Shanghai） - 输出格式仅允许：%Y-%m-%d 或 %Y-%m-%d %H:%M:%S - 前端时间组件使用中文（zh-CN） ## 防混乱约束（必须遵守） - 必须维持：已接收清单、已审核清单、待审清单、整改点编号与计数。 - 禁止：重复索要已上传文件、进度面板计数前后矛盾、遗漏文件未审。 接下来我将上传我的前后端项目压缩文件给你，你解析成项目结构树，我提供运行时报的错误给你，你根据相关链帮我修复代码，记住每个改动都要查看完整相关链以防改奔溃项目，特别注意：如果遇到文件无法查看到全部内容时，请及时跟我交互，告知我文件名，我会提供给你


# 删除库表
-- 确认当前库
SELECT DATABASE();

-- 关闭外键检查（解决删除顺序问题）
SET FOREIGN_KEY_CHECKS = 0;

-- 如果表很多，避免 GROUP_CONCAT 被截断
SET SESSION group_concat_max_len = 1024 * 1024;

-- 拼接并执行 DROP TABLE
SET @tables = (
  SELECT GROUP_CONCAT(CONCAT('`', table_name, '`') SEPARATOR ',')
  FROM information_schema.tables
  WHERE table_schema = DATABASE()
    AND table_type = 'BASE TABLE'
);

SET @sql = IFNULL(CONCAT('DROP TABLE IF EXISTS ', @tables, ';'), 'SELECT 1;');

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 恢复外键检查
SET FOREIGN_KEY_CHECKS = 1;
