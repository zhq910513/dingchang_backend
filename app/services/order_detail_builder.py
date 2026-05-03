# app/services/order_detail_builder.py
# encoding: utf-8
"""
订单详情加载（内部工具）

承重墙（2026-03-05）：
- 对外详情契约已统一为 schemas.order.OrderOut（由 services.order_read_model.to_order_out 产出）
- 本文件不再产出/维护任何“blocks 契约”（{id, base, sections}），避免与 API response_model 冲突
- 本文件仅保留“预加载订单及关联关系”的工具能力，供 read_model / API 层按需调用

性能收敛（2026-03-23）：
- 详情输出当前仅消费订单标量字段、customer_group、channel_group、order_info、images.image_file
- 不再预加载 creator / salesperson，避免无产出的关联补载
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderImage


def _maybe_selectinload(model, attr_name: str):
    """Return selectinload(model.attr) if attr exists, else None."""
    try:
        attr = getattr(model, attr_name, None)
        if attr is None:
            return None
        return selectinload(attr)
    except Exception:
        return None


def _maybe_selectinload_nested(
    parent_model,
    parent_attr: str,
    child_model,
    child_attr: str,
):
    """Return selectinload(parent.attr).selectinload(child.attr) if both attrs exist; else best-effort."""
    try:
        parent = getattr(parent_model, parent_attr, None)
        if parent is None:
            return None

        child = getattr(child_model, child_attr, None)
        if child is None:
            return selectinload(parent)

        return selectinload(parent).selectinload(child)
    except Exception:
        return None


def _build_order_detail_options() -> List:
    opts: List = []

    append_opt = opts.append

    for attr_name in ("customer_group", "channel_group", "order_info"):
        opt = _maybe_selectinload(Order, attr_name)
        if opt is not None:
            append_opt(opt)

    image_opt = _maybe_selectinload_nested(Order, "images", OrderImage, "image_file")
    if image_opt is not None:
        append_opt(image_opt)

    return opts


_ORDER_DETAIL_OPTIONS = _build_order_detail_options()


async def fetch_order_with_relations(
    db: AsyncSession,
    order_id: int,
) -> Optional[Order]:
    """
    预加载订单及其详情输出所需关联关系（best-effort）：
    - customer_group / channel_group / order_info / images.image_file
    """
    stmt = select(Order).where(Order.id == int(order_id)).options(*_ORDER_DETAIL_OPTIONS)
    return (await db.execute(stmt)).scalars().first()
