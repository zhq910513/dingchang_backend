# encoding: utf-8
"""
MODEL PACKAGE (Redesigned, first-deploy mode)

说明：
- 本目录包含项目所有 ORM Model 定义（与 MySQL DDL 对齐，适用于首次部署 create_all）。
- 额外包含“订单事实层”新表：order_slot_result / order_fact（用于卡槽识别事实与订单级投影）。
- 全字段均提供中文注释（Column.comment），便于审计与维护。

注意：
- 若后续你再次启用“DDL 锁定/生产对齐”，可将新增表导入改为按开关控制。
"""

from app.models.channel_group import ChannelGroup  # noqa: F401
from app.models.customer_group import CustomerGroup  # noqa: F401
from app.models.field_config import FieldConfig, FieldGroup, FieldGroupField  # noqa: F401
from app.models.finance import FinanceRecord  # noqa: F401
from app.models.image_file import ImageFile  # noqa: F401
from app.models.image_ocr_result import ImageOcrResult  # noqa: F401
from app.models.ocr_image_cache import OcrImageCache  # noqa: F401
from app.models.ocr_task import OcrTask  # noqa: F401
from app.models.order import Order, OrderImage  # noqa: F401
from app.models.order_fact import OrderFact  # noqa: F401
from app.models.order_info import OrderInfo  # noqa: F401
# 新增事实层（首次部署可直接建表）
from app.models.order_slot_result import OrderSlotResult  # noqa: F401
from app.models.role import Role  # noqa: F401
from app.models.session import UserSession  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.user_role import UserRole  # noqa: F401

__all__ = [
    "Role",
    "User",
    "UserRole",
    "UserSession",
    "CustomerGroup",
    "ChannelGroup",
    "FieldConfig",
    "FieldGroup",
    "FieldGroupField",
    "ImageFile",
    "ImageOcrResult",
    "OcrImageCache",
    "OcrTask",
    "Order",
    "OrderImage",
    "OrderInfo",
    "FinanceRecord",
    "OrderSlotResult",
    "OrderFact",
]
