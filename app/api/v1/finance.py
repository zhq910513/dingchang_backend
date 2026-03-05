# app/api/v1/finance.py
# encoding: utf-8
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/finance", tags=["finance"])
