# app/services/order_detail_builder.py
# encoding: utf-8
"""
订单详情加载（内部工具）

承重墙（2026-03-05）：
- 对外详情契约已统一为 schemas.order.OrderOut（由 services.order_read_model.to_order_out 产出）
- 本文件不再产出/维护任何“blocks 契约”（{id, base, sections}），避免与 API response_model 冲突
- 本文件仅保留“预加载订单及关联关系”的工具能力，供 read_model / API 层按需调用
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import Optional

from app.models.order import Order, OrderImage


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


async def fetch_order_with_relations(db: AsyncSession, order_id: int) -> Optional[Order]:
    """
    预加载订单及其常用关联关系（best-effort）：
    - creator / salesperson / customer_group / channel_group / order_info / images.image_file
    """
    stmt = (
        select(Order)
        .where(Order.id == int(order_id))
        .options(
            *tuple(
                [
                    x
                    for x in [
                    _maybe_selectinload(Order, "creator"),
                    _maybe_selectinload(Order, "salesperson"),
                    _maybe_selectinload(Order, "customer_group"),
                    _maybe_selectinload(Order, "channel_group"),
                    _maybe_selectinload(Order, "order_info"),
                    _maybe_selectinload_nested(Order, "images", OrderImage, "image_file"),
                ]
                    if x is not None
                ]
            )
        )
    )
    return (await db.execute(stmt)).scalars().first()
