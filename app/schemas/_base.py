# encoding: utf-8
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OrmBaseModel(BaseModel):
    """Base for *Out models that are created from SQLAlchemy ORM objects."""
    model_config = ConfigDict(from_attributes=True)
