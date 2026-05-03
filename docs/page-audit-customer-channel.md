# 客户/渠道管理页面/接口审查

更新时间：2026-04-26

页面：前端 `/customers`、`/channels`

主要接口：`/customer-channel/customers`、`/customer-channel/channels`、`/customer-channel/customer-groups`、`/customer-channel/channel-groups`

## 数据抽样

| 项目 | 结果 |
| --- | --- |
| 客户组 | 共 437 条，未删除 432 条，已删除 5 条 |
| 渠道组 | 共 236 条，未删除 223 条，已删除 13 条 |
| 客户主要创建人 | `wangliang=261`、`xmc=60`、`lhj=53`、`wukecheng=43`、`zlf=15` |
| 渠道主要创建人 | `wangliang=137`、`xmc=41`、`wukecheng=39`、`zlf=13` |
| 样本特征 | 客户/渠道是全局字典数据，但保留 `created_by`，可支持销售仅维护自己创建的数据 |

## 权限结论

| 角色 | 客户/渠道权限 |
| --- | --- |
| `super_admin` | 可读、可新增、可编辑、可软删除；可查看已删除 |
| `manager` | 可读、可新增、可编辑、可软删除；不可查看已删除 |
| `market` | 可读、可新增、可编辑、可软删除；不可查看已删除 |
| `sales` | 可读、可新增；仅可编辑/软删除自己创建的未删除记录 |
| `finance` | 只读；不可新增、编辑、删除 |

## 发现的问题

1. 原规则允许 `sales` 删除客户/渠道，但不允许编辑，且删除没有限定 `created_by`。直接调接口时，业务员可软删除全局字典数据，风险高。
2. 原规则只在页面 meta 上表达按钮能力，但更新/删除接口没有做销售行级所有权判断。
3. `get_customer_group_by_id` / `get_channel_group_by_id` 使用 ORM 实体读取，受模型 `lazy="selectin"` 关系影响，存在把 `orders` 等关系隐式加载出来的风险。
4. `finance` 这类明确无写权限角色，原删除/编辑链路会先查目标行再拒绝，既浪费 SQL，也有不必要的信息探测面。

## 已完成优化

1. 客户/渠道 ACL 增加 `current_user_id`，行级能力按 `created_by` 判断销售是否拥有该记录。
2. `sales` 的编辑/删除能力收敛到“自己创建且未删除”的记录；`finance` 写入前置拒绝，不再查询目标行。
3. `get_*_by_id` 改为显式 `lazyload("*")`，需要创建人时只 `selectinload(creator)`，避免关联订单树被隐式加载。
4. 列表保持投影查询，不返回 ORM 实体；管理页列表维持 2 SQL（count + page rows）。
5. 权限基线文档已同步更新。

## 自检结果

| 测试项 | 结果 |
| --- | --- |
| 后端编译 | `python -m py_compile app/api/v1/customer_channel.py app/services/customer_channel_service.py app/schemas/customer_channel.py app/models/customer_group.py app/models/channel_group.py` 通过 |
| 客户列表 | `super_admin` 432 条，2 SQL；`sales` 432 条，2 SQL；`finance` 432 条，2 SQL |
| 渠道列表 | `super_admin` 223 条，2 SQL；`finance` 223 条，2 SQL |
| 销售自有客户 | `sales(id=8)` 查询 `DCKH1001` 返回 `customer.update=true/customer.delete=true` |
| 销售自有渠道 | `sales(id=8)` 查询 `HTQD0309` 返回 `channel.update=true/channel.delete=true` |
| 财务删除 | `finance` 删除客户前置 403，0 SQL |
| 详情读取 | `get_customer_group_by_id(..., with_creator=False)=1 SQL`；`with_creator=True=2 SQL`；渠道同样 1/2 SQL |
| 索引命中 | 客户/渠道默认列表命中 `ix_*_list_is_deleted_updated_id`，按 `updated_at,id` 倒序无 filesort |

## 残余风险

1. 客户/渠道关键字搜索仍是 `%keyword%` 模糊匹配，当前 437/236 条数据没有性能问题；若增长到 10 万级，需要引入搜索列或专门检索能力。
2. 客户/渠道仍是全局字典，未按团队隔离。当前订单创建依赖全局可选项；如后续业务要求团队独立客户池，需要设计迁移方案，不能简单按 `created_by` 改成团队隔离。
3. 软删除没有做“是否已有订单引用”的阻断。当前语义是从下拉隐藏，不破坏历史订单；若业务希望禁止删除被引用记录，需要在订单引用审查中补规则。
