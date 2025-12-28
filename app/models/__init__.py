# encoding: utf-8
"""
确保所有 SQLAlchemy 模型在启动时被 import，从而被 Base.metadata 收集到。
否则 Base.metadata.create_all 可能建出“缺列/缺表”的旧结构。
"""

from app.models.user import User  # noqa: F401
from app.models.role import Role  # noqa: F401
from app.models.user_role import UserRole  # noqa: F401

from app.models.customer_group import CustomerGroup  # noqa: F401
from app.models.channel_group import ChannelGroup  # noqa: F401

from app.models.field_config import FieldConfig, FieldGroup, FieldGroupField  # noqa: F401

from app.models.image_file import ImageFile  # noqa: F401
from app.models.image_ocr_result import ImageOcrResult  # noqa: F401
from app.models.ocr_image_cache import OcrImageCache  # noqa: F401

from app.models.order import Order, OrderImage  # noqa: F401
from app.models.order_info import OrderInfo  # noqa: F401
from app.models.ocr_task import OcrTask  # noqa: F401

from app.models.finance import FinanceRecord  # noqa: F401
from app.models.session import UserSession  # noqa: F401
