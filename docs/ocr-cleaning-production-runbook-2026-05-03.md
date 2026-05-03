# OCR 清洗规则与线上回填手册（2026-05-03）

## 目标

- 不修改数据库字段和表结构。
- 保留 `order_new.ocr_raw_json` 原始 OCR 返回，作为追溯和复核依据。
- 在写入/回填 `order_new.dynamic_data` 前统一清洗，并同步 `order_fact_new` 投影表。
- 线上回填必须先本地真实数据验证，再线上备份，再 dry-run，再正式 apply。

## 规则版本

- 当前规则版本：`ocr-cleaner-2026-05-03-v2`。
- 代码入口：`app/services/ocr_cleaner.py`。
- 所有审计、备份、回填报告都会记录 `rule_version`。

## 已沉淀规则

- 通用文本：NFKC 归一化、去零宽字符、压缩空白、识别空占位值，如 `-`、`暂无`、`无`、`未识别`、`识别失败`。
- 字段标签：去除 OCR 常见前缀，如 `姓名:`、`号牌号码:`、`车辆识别代号:`、`公民身份号码:`、`发动机号:`。
- 日期：兼容 `YYYYMMDD`、`YYYYMM`、`YYYY-MM-DD`、`YYYY/M/D`、`YYYY年M月D日`，统一成 `YYYY-MM-DD` 或月份级 `YYYY-MM`。
- VIN：转大写，去空格/标点/单位文本，抽取 17 位 VIN；OCR 易混字符 `O/I/Q` 归一成 `0/1/0`；明显无效值置空。
- 车牌：去空格/标点，提取中国车牌形态；`未上牌`、`无牌`、`新车未上牌` 置空。
- 发动机号：转大写，只保留字母数字；异常长度置空。
- 证件号：优先抽取 18 位居民身份证并校验出生日期；否则保留含字母的 18 位统一社会信用代码；公司名称或混入中文的无效值置空。
- 身份证有效期：从 `id_validity` 拆出 `id_valid_from` / `id_valid_to`，支持 `长期`。
- 核定载客：支持 `5人`、`2+3人`、`五人`，统一成数字字符串。
- 合格证厂家/品牌：联系人、电话、检测项目、功率项等噪声置空；`中国`、颜色、泛车型词置空。
- 历史别名：`id_birth`、`id_nation`、`register_date`、`id_valid_period`、`id_issue_authority`、`dla_approved_passengers` 会回填到规范字段后移除；所有 `dl_*` 旧键移除。

## 本地真实数据验证流程

在清空本地库并导入线上真实快照后，按下面顺序执行：

```powershell
cd D:\Projects\dingchang\dingchang_backend

C:\Python312\python.exe scripts\ocr_cleaner_retest.py

C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode audit `
  --db-label local-online-snapshot `
  --batch-size 500 `
  --report-path logs\ocr-cleaning-audit-local-online-snapshot.json

C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode backup `
  --db-label local-online-snapshot `
  --batch-size 500 `
  --backup-path logs\ocr-cleaning-backup-local-online-snapshot.jsonl.gz `
  --report-path logs\ocr-cleaning-backup-local-online-snapshot.json

C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode apply `
  --dry-run `
  --confirm APPLY_CLEANED_OCR_DATA `
  --db-label local-online-snapshot `
  --batch-size 500 `
  --backup-path logs\ocr-cleaning-backup-local-online-snapshot.jsonl.gz `
  --report-path logs\ocr-cleaning-apply-dryrun-local-online-snapshot.json
```

准入条件：

- `ocr_cleaner_retest.py` 通过。
- audit 报告中的 `risk_examples` 需要人工/脚本复核，重点看 `vin`、`plate_no`、`id_number` 被置空的样例。
- backup 报告中 `order_rows` 必须等于本次审计范围订单数。
- apply dry-run 的 `changed_rows` 必须与 audit 的 `changed_rows` 基本一致。
- apply/restore 默认校验备份文件内的 `db_label`，防止误拿本地备份操作线上库。

## 线上备份与回填流程

线上库建议先做 DBA/native 级备份，再做本工具 JSONL 业务备份。工具支持通过 `OCR_DB_*` 环境变量指向线上库，不需要修改 `.env`：

```powershell
$env:OCR_DB_HOST = "线上数据库地址"
$env:OCR_DB_PORT = "3306"
$env:OCR_DB_USER = "线上只读/写入账号"
$env:OCR_DB_PASSWORD = "线上密码"
$env:OCR_DB_NAME = "线上库名"
$env:OCR_DB_LABEL = "prod"
```

线上审计：

```powershell
C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode audit `
  --db-label prod `
  --batch-size 500 `
  --report-path logs\ocr-cleaning-audit-prod.json
```

线上业务备份：

```powershell
C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode backup `
  --db-label prod `
  --batch-size 500 `
  --backup-path logs\ocr-cleaning-backup-prod.jsonl.gz `
  --report-path logs\ocr-cleaning-backup-prod.json
```

线上 dry-run：

```powershell
C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode apply `
  --dry-run `
  --confirm APPLY_CLEANED_OCR_DATA `
  --db-label prod `
  --batch-size 500 `
  --backup-path logs\ocr-cleaning-backup-prod.jsonl.gz `
  --report-path logs\ocr-cleaning-apply-dryrun-prod.json
```

线上正式回填：

```powershell
C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode apply `
  --confirm APPLY_CLEANED_OCR_DATA `
  --db-label prod `
  --batch-size 500 `
  --backup-path logs\ocr-cleaning-backup-prod.jsonl.gz `
  --report-path logs\ocr-cleaning-apply-prod.json
```

回填后复核：

```powershell
C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode audit `
  --db-label prod-after-apply `
  --batch-size 500 `
  --report-path logs\ocr-cleaning-audit-prod-after-apply.json
```

期望：`changed_rows` 大幅下降，关键字段风险样例清零或只剩需要人工判断的数据。

## 回滚

如线上回填后发现问题，使用回填前生成的 `logs\ocr-cleaning-backup-prod.jsonl.gz` 回滚：

```powershell
C:\Python312\python.exe scripts\ocr_cleaning_migration.py `
  --mode restore `
  --confirm RESTORE_OCR_BACKUP `
  --db-label prod `
  --batch-size 500 `
  --backup-path logs\ocr-cleaning-backup-prod.jsonl.gz `
  --report-path logs\ocr-cleaning-restore-prod.json
```

回滚会恢复 `order_new.dynamic_data`、`order_new.ocr_raw_json` 和 `order_fact_new` 备份前状态。

## 当前本地预演结果

- `scripts/ocr_cleaner_retest.py`：通过。
- `scripts/ocr_cleaner_api_retest.py`：真实 HTTP 72 步，通过，0 失败。
- 当前本地库只读审计：3177 条订单，2979 条会被规则规范化。
- 当前本地库业务备份：3177 条订单，3177 条投影记录，已写入 `logs/ocr-cleaning-backup-local-current.jsonl.gz`。
- 当前本地库 apply dry-run：扫描 3177 条，预计变更 2979 条，未真实写库。
