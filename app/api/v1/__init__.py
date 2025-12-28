# encoding: utf-8
"""
API v1 router aggregator
"""

from fastapi import APIRouter

from .auth import router as auth_router
from .users import router as users_router
from .orders import router as orders_router
from .finance import router as finance_router
from .field_config import router as field_config_router
from .customer_channel import router as customer_channel_router  # ✅ /customer-groups /channel-groups

router = APIRouter()

router.include_router(auth_router)
router.include_router(users_router)
router.include_router(customer_channel_router)
router.include_router(orders_router)
router.include_router(finance_router)
router.include_router(field_config_router)
