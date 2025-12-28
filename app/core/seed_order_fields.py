# app/core/seed_order_fields.py
# -*- coding: utf-8 -*-

"""
车辆单证字段 v4（对齐：最新前端布局 + 默认展示列策略 + OCR 字段含义）

分组：
- 订单详情（车辆合格证信息）
- 身份证信息
- 行驶证信息
- 行驶证副件信息

策略：
1) Upsert FieldGroup
2) Upsert FieldConfig
3) 重建 FieldGroupField 映射
4) options 始终写 dict（{"items":[...] }）
5) 预设列表默认展示列（extra.show_in_list）
6) ✅ 预设财务列表默认展示列（extra.show_in_finance_list）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.field_config import FieldConfig, FieldGroup, FieldGroupField

MODULE = "order"


def _options_items(items: Optional[List[Any]] = None) -> Dict[str, Any]:
    return {"items": items or []}


def _merge_extra(base: Optional[Dict[str, Any]], patch: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if base is None and patch is None:
        return None
    out: Dict[str, Any] = {}
    if isinstance(base, dict):
        out.update(base)
    if isinstance(patch, dict):
        out.update(patch)
    # ✅ 避免把空 dict 写进库里（更干净）
    return out or None


async def _get_or_create_group(
    db: AsyncSession,
    module: str,
    group_key: str,
    group_name: str,
    order_index: int,
) -> FieldGroup:
    q = select(FieldGroup).where(
        FieldGroup.module == module,
        FieldGroup.group_key == group_key,
    )
    obj = (await db.execute(q)).scalar_one_or_none()
    if obj:
        obj.group_name = group_name
        obj.order_index = order_index
        return obj

    obj = FieldGroup(
        module=module,
        group_key=group_key,
        group_name=group_name,
        order_index=order_index,
    )
    db.add(obj)
    await db.flush()
    return obj


async def _get_or_create_field(
    db: AsyncSession,
    module: str,
    field_name: str,
    label: str,
    type_: str = "text",
    required: bool = False,
    visible: bool = True,
    editable: bool = True,
    sort: int = 0,
    options: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> FieldConfig:
    q = select(FieldConfig).where(
        FieldConfig.module == module,
        FieldConfig.field_name == field_name,
    )
    obj = (await db.execute(q)).scalar_one_or_none()

    if obj:
        obj.label = label
        obj.type = type_
        obj.required = required
        obj.visible = visible
        obj.editable = editable
        obj.sort = sort
        obj.options = options
        obj.extra = extra
        return obj

    obj = FieldConfig(
        module=module,
        field_name=field_name,
        label=label,
        type=type_,
        required=required,
        visible=visible,
        editable=editable,
        sort=sort,
        options=options,
        extra=extra,
    )
    db.add(obj)
    await db.flush()
    return obj


async def _rebuild_group_links(
    db: AsyncSession,
    group_id: int,
    field_ids_in_order: List[int],
):
    await db.execute(delete(FieldGroupField).where(FieldGroupField.group_id == group_id))
    for idx, fid in enumerate(field_ids_in_order):
        db.add(FieldGroupField(group_id=group_id, field_id=fid, order_index=idx))
    await db.flush()


def build_seed_spec() -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """
    注意：
    - 这里的 name 必须与你前端 CERT_LAYOUT / ID_FIELDS / DL_FIELDS / DLA_FIELDS 的 key 对齐
    - “默认展示列”通过 extra.show_in_list 控制（useOrderFieldConfig.js 会读取）
    - ✅ “财务默认展示列”通过 extra.show_in_finance_list 控制
    """

    # -------------------------
    # ✅ 订单列表默认展示列（顺序必须固定）
    # -------------------------
    default_list_order = [
        "vehicle_model",
        "vin",
        "engine_no",
        "approved_passenger_count",
        "id_name",
        "id_number",
        "dl_plate_no",
        "dl_owner",
    ]
    default_list_index = {k: i for i, k in enumerate(default_list_order)}

    def _list_extra(field_name: str, width: int = 160) -> Dict[str, Any]:
        if field_name not in default_list_index:
            return {}
        return {
            "show_in_list": True,
            "list_width": width,
            "ui_default_list": True,
            "ui_default_list_order": default_list_index[field_name],
        }

    # -------------------------
    # ✅ 财务列表默认展示列（稳定 & 可控）
    # 说明：
    # - 财务页主列本来就有固定列：渠道群/客户群/业务员/财务状态
    # - 动态字段建议精简：车辆型号、车架号、号牌号码（展开行仍可看更多）
    # -------------------------
    default_finance_order = [
        "vehicle_model",
        "vin",
        "dl_plate_no",
    ]
    default_finance_index = {k: i for i, k in enumerate(default_finance_order)}

    def _finance_extra(field_name: str, width: int = 160) -> Dict[str, Any]:
        if field_name not in default_finance_index:
            return {}
        # ✅ 复用 ui_default_list_order（前端 financeFields 也按它排序）
        # 这里让 finance 的顺序“与订单默认列同源”，避免两套 order 编号冲突
        return {
            "show_in_finance_list": True,
            "finance_list_width": width,
        }

    def _patch_extra(field_name: str, base: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        patch = _merge_extra(_list_extra(field_name), _finance_extra(field_name))
        return _merge_extra(base, patch)

    # -------------------------
    # 订单详情（车辆合格证信息）
    # -------------------------
    order_fields = [
        {"name": "cert_no", "label": "合格证编号", "type": "text"},
        {"name": "cert_issue_date", "label": "发证日期", "type": "date"},
        {"name": "manufacturer_name", "label": "车辆制造企业名称", "type": "text"},

        {"name": "vehicle_brand_name", "label": "车辆品牌/车辆名称", "type": "text"},
        {"name": "vehicle_model", "label": "车辆型号", "type": "text", "extra": _patch_extra("vehicle_model", {"list_width": 180, "finance_list_width": 180})},
        {"name": "vin", "label": "车辆识别代号/车架号", "type": "text", "extra": _patch_extra("vin", {"list_width": 210, "finance_list_width": 210})},

        {"name": "body_color", "label": "车身颜色", "type": "text"},

        {"name": "chassis_model_id", "label": "底盘型号/底盘ID", "type": "text"},
        {"name": "chassis_cert_no", "label": "底盘合格证编号", "type": "text"},

        {"name": "engine_model", "label": "发动机型号", "type": "text"},
        {"name": "engine_no", "label": "发动机号", "type": "text", "extra": _patch_extra("engine_no", {"list_width": 180})},

        {"name": "fuel_type", "label": "燃料种类", "type": "text"},
        {"name": "displacement_and_power", "label": "排量和功率(mL/kW)", "type": "text"},

        {"name": "emission_standard", "label": "排放标准", "type": "text"},
        {"name": "fuel_consumption", "label": "油耗", "type": "text"},

        {"name": "overall_dimensions", "label": "外廓尺寸(mm)", "type": "text"},
        {"name": "cargo_dimensions", "label": "货箱内部尺寸(mm)", "type": "text"},

        {"name": "leaf_spring_count", "label": "钢板弹簧片数(片)", "type": "text"},
        {"name": "tire_count", "label": "轮胎数", "type": "text"},
        {"name": "tire_spec", "label": "轮胎规格", "type": "text"},

        {"name": "wheel_track", "label": "轮距(前/后)(mm)", "type": "text"},
        {"name": "wheel_base", "label": "轴距(mm)", "type": "text"},

        {"name": "axle_load_kg", "label": "轴荷(kg)", "type": "text"},
        {"name": "axle_count", "label": "轴数", "type": "text"},

        {"name": "curb_weight", "label": "整备质量(kg)", "type": "text"},
        {"name": "steering_type", "label": "转向形式", "type": "text"},

        {"name": "gross_weight", "label": "总质量(kg)", "type": "text"},
        {"name": "rated_load", "label": "额定载质量(kg)", "type": "text"},

        {"name": "rated_traction_weight", "label": "额定牵引总质量(kg)", "type": "text"},
        {"name": "load_utilization_factor", "label": "载质量利用系数", "type": "text"},

        {"name": "allowed_traction_weight", "label": "准牵引总质量(kg)", "type": "text"},
        {"name": "semi_trailer_weight", "label": "半挂车鞍座最大允许总质量(kg)", "type": "text"},

        {"name": "cab_passenger_count", "label": "驾驶室准乘人数(人)", "type": "text"},
        {"name": "approved_passenger_count", "label": "额定载客(人)", "type": "text", "extra": _patch_extra("approved_passenger_count", {"list_width": 150})},

        {"name": "max_design_speed", "label": "最高设计车速(km/h)", "type": "text"},
        {"name": "manufacture_date", "label": "车辆制造日期", "type": "date"},
    ]

    # -------------------------
    # 身份证信息
    # -------------------------
    id_fields = [
        {"name": "id_name", "label": "姓名", "type": "text", "extra": _patch_extra("id_name", {"list_width": 120})},
        {
            "name": "id_gender",
            "label": "性别",
            "type": "select",
            "options": _options_items([
                {"label": "男", "value": "male"},
                {"label": "女", "value": "female"},
            ]),
        },
        {"name": "id_ethnicity", "label": "民族", "type": "text"},
        {"name": "id_birth_date", "label": "出生日期", "type": "date"},
        {"name": "id_address", "label": "住址", "type": "text"},
        {"name": "id_number", "label": "身份证号码", "type": "text", "extra": _patch_extra("id_number", {"list_width": 190})},
        {"name": "id_issuer", "label": "签发机关", "type": "text"},
        {"name": "id_validity", "label": "有效期限", "type": "text"},
    ]

    # -------------------------
    # 行驶证信息（主页）
    # -------------------------
    driving_fields = [
        {"name": "dl_plate_no", "label": "号牌号码", "type": "text", "extra": _patch_extra("dl_plate_no", {"list_width": 120, "finance_list_width": 140})},
        {"name": "dl_vehicle_type", "label": "车辆类型", "type": "text"},
        {"name": "dl_owner", "label": "所有人", "type": "text", "extra": _patch_extra("dl_owner", {"list_width": 120})},
        {"name": "dl_address", "label": "住址", "type": "text"},
        {"name": "dl_use_nature", "label": "使用性质", "type": "text"},
        {"name": "dl_brand_model", "label": "品牌型号", "type": "text"},
        {"name": "dl_vin", "label": "车辆识别代码", "type": "text"},
        {"name": "dl_engine_no", "label": "发动机号码", "type": "text"},
        {"name": "dl_register_date", "label": "注册日期", "type": "date"},
        {"name": "dl_issue_date", "label": "发证日期", "type": "date"},
        {"name": "dl_issuer_org", "label": "发证机关", "type": "text"},
    ]

    # -------------------------
    # 行驶证副件信息（副页）
    # -------------------------
    driving_attach_fields = [
        {"name": "dla_plate_no", "label": "号牌号码", "type": "text"},
        {"name": "dla_archive_no", "label": "档案编号", "type": "text"},
        {"name": "dla_approved_passengers", "label": "核定载人数", "type": "text"},
        {"name": "dla_gross_weight", "label": "总质量", "type": "text"},
        {"name": "dla_curb_weight", "label": "整备质量", "type": "text"},
        {"name": "dla_approved_load", "label": "核定载质量", "type": "text"},
        {"name": "dla_overall_dimensions", "label": "外廓尺寸", "type": "text"},
        {"name": "dla_max_tow_weight", "label": "准牵引总质量", "type": "text"},
        {"name": "dla_remark", "label": "备注", "type": "text"},
        {"name": "dla_check_record", "label": "检验记录", "type": "text"},
        {"name": "dla_vehicle_type", "label": "车辆类型", "type": "text"},
        {"name": "dla_core_no", "label": "证芯编号", "type": "text"},
        {"name": "dla_fuel_type", "label": "燃油类型", "type": "text"},
    ]

    # 给 order_fields 补全 extra（避免覆盖已有 extra），并给 sort 稳定
    patched_order_fields: List[Dict[str, Any]] = []
    for idx, f in enumerate(order_fields):
        name = f["name"]
        merged = _patch_extra(name, f.get("extra"))
        nf = dict(f)
        nf["extra"] = merged
        nf.setdefault("sort", idx)
        patched_order_fields.append(nf)

    return [
        ({"key": "order_detail", "name": "订单详情（车辆合格证信息）", "order": 1}, patched_order_fields),
        ({"key": "id_card", "name": "身份证信息", "order": 2}, id_fields),
        ({"key": "driving_license", "name": "行驶证信息", "order": 3}, driving_fields),
        ({"key": "driving_attach", "name": "行驶证副件信息", "order": 4}, driving_attach_fields),
    ]


async def seed_order_fields(db: AsyncSession):
    spec = build_seed_spec()

    group_objs: List[FieldGroup] = []
    group_to_field_objs: Dict[str, List[FieldConfig]] = {}

    for group_meta, fields in spec:
        g = await _get_or_create_group(
            db,
            module=MODULE,
            group_key=group_meta["key"],
            group_name=group_meta["name"],
            order_index=group_meta["order"],
        )
        group_objs.append(g)

        field_objs: List[FieldConfig] = []
        for idx, f in enumerate(fields):
            options = f.get("options")
            if f.get("type") == "select" and options is None:
                options = _options_items([])

            obj = await _get_or_create_field(
                db,
                module=MODULE,
                field_name=f["name"],
                label=f["label"],
                type_=f.get("type", "text"),
                required=bool(f.get("required", False)),
                visible=bool(f.get("visible", True)),
                editable=bool(f.get("editable", True)),
                sort=int(f.get("sort", idx)),
                options=options,
                extra=f.get("extra"),
            )
            field_objs.append(obj)

        group_to_field_objs[group_meta["key"]] = field_objs

    for g in group_objs:
        fields = group_to_field_objs.get(g.group_key, [])
        await _rebuild_group_links(db, g.id, [x.id for x in fields])

    await db.commit()
