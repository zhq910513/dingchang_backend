# encoding: utf-8
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/finance", tags=["finance"])


class FinanceOrderStatusUpdate(BaseModel):
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None
