# app/services/order_read_model.py
# encoding: utf-8
"""
订单读取读模型（Read Model）

目标：
- 统一 Order -> OrderOut 的映射逻辑（orders / finance 共用）
- 统一预加载（selectinload）清单，避免“财务列表字段为空”这类分裂
- 不做任何权限判断：权限/团队过滤由路由层（orders.py / finance.py）负责

注意：
- 本模块只做“读/组装”，不做写操作、不做 ACL。
- models/schemas 冻结：这里只修“输出阶段”的契约一致性与运行风险。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.access_control import (
    pick_manager_id_from_salesperson as _ac_pick_manager_id_from_salesperson,
    pick_manager_name_inline as _ac_pick_manager_name_inline,
    split_team_names_any as _ac_split_team_names_any,
)
from app.models.order import Order, OrderImage
from app.models.user import User
from app.schemas.order import OrderInfoOut, OrderOut
from app.services.storage import StorageService
from app.utils.order_image_urls import ensure_display_urls_for_order_images


def _maybe_selectinload(model, attr_name: str):
    """Return selectinload(model.attr) if attr exists, else None."""
    try:
        if hasattr(model, attr_name):
            return selectinload(getattr(model, attr_name))
    except Exception:
        return None
    return None


def _maybe_selectinload_nested(parent_model, parent_attr: str, child_model, child_attr: str):
    """Return selectinload(parent.attr).selectinload(child.attr) if both attrs exist; else best-effort."""
    try:
        if hasattr(parent_model, parent_attr) and hasattr(child_model, child_attr):
            return selectinload(getattr(parent_model, parent_attr)).selectinload(getattr(child_model, child_attr))
        if hasattr(parent_model, parent_attr):
            return selectinload(getattr(parent_model, parent_attr))
    except Exception:
        return None
    return None


def preload_options():
    """统一的 Order 列表预加载清单（orders / finance 共用）。

    ✅ models 冻结时可能不存在 relationship（只有 *_id 外键列）。
    这里做防守式预加载：有就 preload，没有就跳过，避免 AttributeError。
    """
    opts = []
    for name in ("creator", "salesperson", "customer_group", "channel_group", "order_info"):
        opt = _maybe_selectinload(Order, name)
        if opt is not None:
            opts.append(opt)

    # images -> image_file
    opt_img = _maybe_selectinload_nested(Order, "images", OrderImage, "image_file")
    if opt_img is not None:
        opts.append(opt_img)

    return tuple(opts)


def _user_display_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    return getattr(u, "full_name", None) or getattr(u, "real_name", None) or getattr(u, "username", None)


def _group_display_name(g) -> Optional[str]:
    if not g:
        return None
    return (
        getattr(g, "channel_name", None)
        or getattr(g, "customer_name", None)
        or getattr(g, "group_name", None)
        or getattr(g, "name", None)
        or getattr(g, "customer_code", None)
        or getattr(g, "channel_code", None)
    )


def _group_code_name(g) -> Optional[str]:
    if not g:
        return None

    code = (
        getattr(g, "channel_code", None)
        or getattr(g, "customer_code", None)
        or getattr(g, "group_code", None)
        or getattr(g, "code", None)
    )
    name = (
        getattr(g, "channel_name", None)
        or getattr(g, "customer_name", None)
        or getattr(g, "group_name", None)
        or getattr(g, "name", None)
    )

    code_s = str(code).strip() if code is not None and str(code).strip() else ""
    name_s = str(name).strip() if name is not None and str(name).strip() else ""

    if code_s and name_s:
        return f"{code_s} - {name_s}"
    if name_s:
        return name_s
    if code_s:
        return code_s

    fallback = _group_display_name(g)
    return str(fallback).strip() if fallback is not None and str(fallback).strip() else None


async def _build_manager_maps(db: AsyncSession, orders: List[Order]) -> Tuple[Dict[int, int], Dict[int, User]]:
    """批量计算：salesperson_id -> manager_id，以及 manager_id -> User."""
    manager_ids: Set[int] = set()
    salesperson_to_manager_id: Dict[int, int] = {}

    for o in orders:
        sp = getattr(o, "salesperson", None)
        if not sp:
            continue
        mid = _ac_pick_manager_id_from_salesperson(sp)
        if mid:
            sid = int(getattr(sp, "id", 0) or 0)
            if sid > 0:
                salesperson_to_manager_id[sid] = int(mid)
            manager_ids.add(int(mid))

    managers_by_id: Dict[int, User] = {}
    if manager_ids:
        mgr_rows = (await db.execute(select(User).where(User.id.in_(list(manager_ids))))).scalars().all()
        managers_by_id = {int(getattr(u, "id", 0) or 0): u for u in mgr_rows if getattr(u, "id", None) is not None}

    return salesperson_to_manager_id, managers_by_id


def _order_info_out(info) -> Optional[OrderInfoOut]:
    if not info:
        return None
    return OrderInfoOut.from_orm(info)


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    return False


def _ymd_from_8digits(s: str) -> Optional[str]:
    ss = str(s or "").strip()
    if len(ss) == 8 and ss.isdigit():
        return f"{ss[0:4]}-{ss[4:6]}-{ss[6:8]}"
    return None


def _pick_first_nonblank(*vals: Any) -> Optional[str]:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str):
            s = v.strip()
            if s:
                return s
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _normalize_dynamic_data(dynamic_data: Any, ocr_raw_json: Any) -> Dict[str, Any]:
    """输出阶段强制规范化：只在规范键为空时回填（不把旧键当展示口径输出）。"""
    d: Dict[str, Any] = dict(dynamic_data or {}) if isinstance(dynamic_data, dict) else {}
    ocr: Dict[str, Any] = dict(ocr_raw_json or {}) if isinstance(ocr_raw_json, dict) else {}

    # 规范键 -> 候选旧键/ocr键
    # 只在规范键为空时回填
    def fill_str(key: str, *candidates: Any) -> None:
        if not _is_blank(d.get(key)):
            return
        v = _pick_first_nonblank(*candidates)
        if v is not None:
            d[key] = v

    # 日期：允许 8 位转 YYYY-MM-DD；否则仅在明显可用时填入
    def fill_date_ymd(key: str, *candidates: Any) -> None:
        if not _is_blank(d.get(key)):
            return
        picked = _pick_first_nonblank(*candidates)
        if picked is None:
            return
        ymd = _ymd_from_8digits(picked) or str(picked).strip()
        if ymd:
            d[key] = ymd

    fill_str("vin", d.get("dl_vin"), ocr.get("vin"), ocr.get("dl_vin"))
    fill_str("plate_no", d.get("dl_plate_no"), ocr.get("plate_no"), ocr.get("dl_plate_no"))
    fill_str("owner_name", d.get("dl_owner"), ocr.get("owner_name"), ocr.get("dl_owner"))
    fill_str("engine_no", d.get("dl_engine_no"), ocr.get("engine_no"), ocr.get("dl_engine_no"))
    fill_str(
        "vehicle_model",
        d.get("dl_vehicle_model"),
        d.get("dl_brand_model"),
        ocr.get("vehicle_model"),
        ocr.get("dl_vehicle_model"),
        ocr.get("dl_brand_model"),
    )
    fill_date_ymd("first_register_date", d.get("dl_register_date"), ocr.get("first_register_date"), ocr.get("dl_register_date"))
    fill_str("id_number", d.get("dl_id_number"), ocr.get("id_number"), ocr.get("dl_id_number"))

    return d


def _looks_like_url(s: str) -> bool:
    ss = str(s or "").strip()
    if not ss:
        return False
    return ss.startswith("http://") or ss.startswith("https://")


def _extract_image_urls_from_images(images: List[OrderImage]) -> List[str]:
    """image_urls 的唯一来源：OrderImage.image_url / OrderImage.image_file.url."""
    out: List[str] = []
    seen: Set[str] = set()

    for im in images or []:
        u = (getattr(im, "image_url", "") or "").strip()
        if not u:
            imf = getattr(im, "image_file", None)
            u = (getattr(imf, "url", "") or "").strip()
        if not u:
            continue
        if not _looks_like_url(u):
            # 严格：不输出非 URL（避免把任何配置/字典字符串混入 image_urls）
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)

    return out


def _safe_get_loaded_images(o: Order) -> Optional[List[OrderImage]]:
    """避免 async 下未 preload 的 relationship 懒加载触发异常。"""
    try:
        # SQLAlchemy relationship 若未加载，通常不在 __dict__
        if hasattr(o, "__dict__") and "images" in o.__dict__:
            imgs = o.__dict__.get("images")
            if isinstance(imgs, list):
                return imgs
            return list(imgs) if imgs else []
        return None
    except Exception:
        return None


async def _prefetch_order_images(db: AsyncSession, order_ids: List[int]) -> Dict[int, List[OrderImage]]:
    """当 Order.images 不可用/未预加载时，防守式按 order_id 批量拉取 OrderImage（含 image_file）。"""
    if not order_ids:
        return {}

    stmt = select(OrderImage).where(getattr(OrderImage, "order_id").in_(order_ids))
    opt = _maybe_selectinload(OrderImage, "image_file")
    if opt is not None:
        stmt = stmt.options(opt)

    rows = (await db.execute(stmt)).scalars().all()

    mp: Dict[int, List[OrderImage]] = {}
    for im in rows:
        oid = getattr(im, "order_id", None)
        if oid is None:
            continue
        oid_int = int(oid)
        mp.setdefault(oid_int, []).append(im)

    return mp


def to_order_out(
    o: Order,
    *,
    storage: StorageService,
    salesperson_to_manager_id: Dict[int, int],
    managers_by_id: Dict[int, User],
    images_by_order_id: Dict[int, List[OrderImage]],
) -> OrderOut:
    """统一 Order ORM -> OrderOut 映射。"""

    # images：优先使用已 preload 的 relationship；否则用批量预取的映射
    imgs_loaded = _safe_get_loaded_images(o)
    if imgs_loaded is not None:
        images = imgs_loaded
    else:
        images = images_by_order_id.get(int(getattr(o, "id", 0) or 0), []) or []

    # 先把 image_url 回填为可展示 URL（签名/公网/回退）
    ensure_display_urls_for_order_images(images, storage)

    cg = getattr(o, "customer_group", None)
    sp = getattr(o, "salesperson", None)

    team_name_val = (getattr(sp, "team_name", None) or None) if sp else None
    team_names_val = _ac_split_team_names_any(getattr(sp, "team_names", None)) if sp else []
    if not team_names_val and team_name_val and str(team_name_val).strip():
        team_names_val = [str(team_name_val).strip()]

    manager_id_val = None
    manager_name_val = None
    if sp:
        manager_name_val = _ac_pick_manager_name_inline(sp)
        sp_id_int = int(getattr(sp, "id", 0) or 0)
        mid = salesperson_to_manager_id.get(sp_id_int) or _ac_pick_manager_id_from_salesperson(sp)
        if mid:
            manager_id_val = int(mid)
            if not manager_name_val:
                manager_name_val = _user_display_name(managers_by_id.get(int(mid)))

    # ✅ 输出阶段规范化：只在规范键为空时回填
    dyn_norm = _normalize_dynamic_data(getattr(o, "dynamic_data", None), getattr(o, "ocr_raw_json", None))

    # ✅ image_urls 唯一来源：只从 order_image/image_file.url 产出 URL 列表
    image_urls = _extract_image_urls_from_images(images)

    return OrderOut(
        id=o.id,
        created_by=o.created_by,
        salesperson_id=o.salesperson_id,
        customer_group_id=o.customer_group_id,
        channel_group_id=o.channel_group_id,
        manager_id=manager_id_val,
        manager_name=manager_name_val,
        team_name=(str(team_name_val).strip() if team_name_val is not None and str(team_name_val).strip() else None),
        team_names=team_names_val,
        is_finished=bool(o.is_finished),
        is_rebate=bool(getattr(o, "is_rebate", False)),
        is_paid=bool(getattr(o, "is_paid", False)),
        dynamic_data=dyn_norm,
        image_urls=image_urls,
        images=images,
        created_at=getattr(o, "created_at", None),
        updated_at=getattr(o, "updated_at", None),
        customer_group_name=_group_code_name(cg),
        channel_group_name=_group_code_name(getattr(o, "channel_group", None)),
        salesperson_name=_user_display_name(getattr(o, "salesperson", None)),
        customer_group_market=getattr(cg, "market", None) if cg else None,
        order_info=_order_info_out(getattr(o, "order_info", None)),
    )


async def orders_to_out_list(db: AsyncSession, orders: List[Order], *, storage: StorageService) -> List[OrderOut]:
    if not orders:
        return []

    salesperson_to_manager_id, managers_by_id = await _build_manager_maps(db, orders)

    # 防守式：如果未 preload images（或 relationship 不存在/不可用），批量预取 OrderImage
    order_ids: List[int] = []
    for o in orders:
        oid = getattr(o, "id", None)
        if oid is None:
            continue
        try:
            order_ids.append(int(oid))
        except Exception:
            continue

    images_by_order_id: Dict[int, List[OrderImage]] = {}
    # 仅在“无法确认 images 已加载”的情况下预取（即便多取一次也不影响正确性）
    try:
        images_by_order_id = await _prefetch_order_images(db, order_ids)
    except Exception:
        images_by_order_id = {}

    return [
        to_order_out(
            o,
            storage=storage,
            salesperson_to_manager_id=salesperson_to_manager_id,
            managers_by_id=managers_by_id,
            images_by_order_id=images_by_order_id,
        )
        for o in orders
    ]