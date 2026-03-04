# encoding: utf-8
"""Pydantic Schemas（接口契约层）

硬规则：
- 本包仅描述 API 入参/出参契约，必须与冻结的 ORM Models 一致（新表口径）。
- 不做任何历史字段/结构兼容；如需数据修复，请通过迁移/回填完成。
"""

from app.schemas._base import OrmBaseModel  # noqa: F401
from app.schemas.ai_assistant import AiChatIn, AiChatOut, AiSessionListOut  # noqa: F401
from app.schemas.auth import LoginIn, LoginOut  # noqa: F401
from app.schemas.customer_channel import OptionItem, OptionListOut  # noqa: F401
from app.schemas.field_config import FieldConfigOut, FieldConfigListOut  # noqa: F401
from app.schemas.finance import FinanceOrderStatusUpdate  # noqa: F401
from app.schemas.order import (  # noqa: F401
    SlotImageItemOut,
    SlotImageNodeOut,
    OrderImageOut,
    OrderInfoIn,
    OrderInfoOut,
    OrderCreate,
    OrderUpdate,
    OrderStatusUpdate,
    OrderOut,
    OrderListItemOut,
    OrderListMeta,
    OrderListResponse,
)
from app.schemas.user import UserOut, UserListOut  # noqa: F401

__all__ = [
    "OrmBaseModel",
    "LoginIn",
    "LoginOut",
    "UserOut",
    "UserListOut",
    "SlotImageItemOut",
    "SlotImageNodeOut",
    "OrderImageOut",
    "OrderInfoIn",
    "OrderInfoOut",
    "OrderCreate",
    "OrderUpdate",
    "OrderStatusUpdate",
    "OrderOut",
    "OrderListItemOut",
    "OrderListMeta",
    "OrderListResponse",
    "FinanceOrderStatusUpdate",
    "OptionItem",
    "OptionListOut",
    "FieldConfigOut",
    "FieldConfigListOut",
    "AiChatIn",
    "AiChatOut",
    "AiSessionListOut",
]
