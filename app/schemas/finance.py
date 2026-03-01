# encoding: utf-8
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class FinanceOrderStatusUpdate(BaseModel):
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None
