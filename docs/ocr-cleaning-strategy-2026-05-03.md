# OCR 入库清洗策略（2026-05-03）

## 目标

- 不改数据库字段，只在写入 `order_new.dynamic_data` 前做规范化。
- 保留 `ocr_raw_json` 原始 OCR 返回，用于追溯、复核和后续算法优化。
- 清洗后的 `dynamic_data` 再同步到 `order_fact_new`，保证列表查询、财务查询和条件搜索使用同一套规范字段。
- 规则坚持保守原则：能确定的清洗，不能确定的不猜测；无效值置为 `null`，避免污染列表索引字段。

## 抽样发现

- 近 1600 条订单中，OCR 字段主要集中在 `id_name`、`vin`、`engine_no`、`first_register_date`、`id_number`、`plate_no`、`owner_name`。
- 常见脏数据包括：字段前后空格、空字符串/横杠占位、日期 `YYYYMMDD` 未统一、VIN 后拼入 `5人`、身份证号后拼入 `公`、企业名称误入 `id_number`。
- 合格证 OCR 噪声较大，存在 `manufacturer_name` 写入联系人/联系电话/检测项，`vehicle_brand_name` 只写入“中国”等不可用值。
- 历史动态字段仍有别名：`id_birth`、`id_nation`、`register_date`、`id_valid_period`、`id_issue_authority`、`dla_approved_passengers`。

## 清洗规则

- 通用文本：全角转半角、去零宽字符、收敛空白、删除字段名前缀，如 `姓名:`、`号牌号码:`。
- 日期：支持 `YYYY-MM-DD`、`YYYYMMDD`、`YYYY/MM/DD`、`YYYY年MM月DD日`，统一为 `YYYY-MM-DD`；月份级日期允许保留为 `YYYY-MM`；非法日期置空。
- VIN：全角转半角、去空格和单位文本、转大写，抽取 17 位 VIN；明显不足 17 位或不符合 VIN 字符集的值置空。
- 车牌：去空格/标点、转大写，抽取中国车牌形态；无法识别为车牌的值置空。
- 发动机号：转大写，保留字母数字，长度异常置空。
- 证件号：优先抽取 18 位居民身份证并校验出生日期；否则保留 18 位统一社会信用代码；纯企业名称或混入中文的无效值置空。
- 身份证有效期：从 `id_validity` 中拆出起止日期；`长期` 保留为 `id_valid_to=长期`。
- 核定载人数：`5人`、`5 人` 统一为 `5`，超出合理范围置空。
- 合格证厂家/品牌：保留公司/厂家类文本；联系人、电话、检测项、纯国家/颜色/车型泛词置空。
- 历史别名：先回填到规范字段，再删除旧别名和 `dl_*` 历史键。

## 入库链路

- 手工创建、草稿、详情编辑、上传完成：都通过 `_clean_dynamic_data_for_write()`，底层统一调用 `clean_dynamic_data_for_ocr()`。
- OCR worker：先清洗订单已有 `dynamic_data`，再清洗本次 OCR 抽取值，然后只填充空字段，最后再次清洗入库。
- `order_fact_new`：由清洗后的 `dynamic_data` 构建，列表页和财务页继续走投影表，不新增库表字段。
- 详情读取：旧数据读取时也经过同一清洗器输出，减少历史脏数据对展示的影响。

## 自检

- 回归脚本：`scripts/ocr_cleaner_retest.py`。
- 覆盖场景：VIN 拼入人数、身份证混入中文、企业统一社会信用代码、非法日期、合格证脏厂家、历史别名回填、投影表 payload。
