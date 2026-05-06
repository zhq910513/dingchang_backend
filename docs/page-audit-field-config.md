# 字段配置接口审查

更新时间：2026-04-26

主要接口：`GET /field-config/form-config`、`GET /field-config`、`PUT /field-config/{module}/{field_name}`

## 数据抽样

| 项目 | 结果 |
| --- | --- |
| 字段配置 | `order` 模块 76 个字段 |
| 字段分组 | `order` 模块 4 个分组 |
| 分组映射 | 67 条 |
| 角色专属字段 | 当前本地未配置 `view_roles/edit_roles` 非空字段 |

## 权限结论

| 接口 | 权限 |
| --- | --- |
| `GET /field-config/form-config` | 已登录用户可按主角色读取表单配置 |
| `GET /field-config` | 仅 `super_admin`、`manager` 可读原始配置 |
| `PUT /field-config/{module}/{field_name}` | 仅 `super_admin`、`manager` 可写 |

## 发现的问题

1. `form-config` 冷启动读取 7 SQL，原因是 `FieldGroup` / `FieldGroupField` 的 `lazy="selectin"` 关系被隐式预加载，而接口已经手动查询分组、映射、字段。
2. `GET /field-config` 原先没有鉴权，会暴露原始字段配置和角色配置。
3. `view_roles/edit_roles` 没有校验角色名，错误角色写入后会造成字段不可见或权限表现异常。
4. `upsert` 的业务 `ValueError` 未转为 400，错误输入可能表现为 500。

## 已完成优化

1. `form-config` 三段查询全部增加 `lazyload("*")`，冷启动从 7 SQL 降到 3 SQL；进程内缓存命中仍为 0 SQL。
2. `GET /field-config` 加管理权限，仅 `super_admin/manager` 可访问。
3. `view_roles/edit_roles` 在服务层归一化并校验，只允许系统已知角色。
4. `upsert` 的 `ValueError` 统一返回 400，避免错误输入冒泡为 500。
5. 写入成功后继续按 `module` 失效缓存，避免旧配置残留。

## 自检结果

| 测试项 | 结果 |
| --- | --- |
| 后端编译 | `python -m py_compile app/api/v1/field_config.py app/services/field_config_service.py app/schemas/field_config.py` 通过 |
| `form-config` 冷启动 | `super_admin/order`：3 SQL，4 个分组，67 个字段 |
| `form-config` 缓存命中 | 0 SQL |
| 原始配置读取 | `super_admin`：1 SQL，76 条 |
| 原始配置拒绝 | `sales`：0 SQL，403 |
| 非法角色写入 | `view_roles=["bad_role"]`：0 SQL，直接 `ValueError`，不会落库 |

## 残余风险

1. 字段配置缓存是进程内缓存，多进程部署时某个 worker 写入后只能失效本进程缓存。若生产多 worker 且频繁改字段配置，需要 Redis pub/sub 或版本号缓存。
2. 当前没有字段配置管理前端页面；原始配置接口虽然已收权限，但仍应避免非必要开放。
