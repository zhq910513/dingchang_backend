# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.v1.ai_assistant import _normalize_quote_image_content_type


def test_quote_image_upload_accepts_missing_mime_when_extension_is_image() -> None:
    content_type, ext = _normalize_quote_image_content_type("card.HEIC", "application/octet-stream")

    assert content_type == "image/heic"
    assert ext == ".heic"


def test_quote_image_upload_rejects_unknown_extension_when_mime_is_missing() -> None:
    with pytest.raises(HTTPException):
        _normalize_quote_image_content_type("payload.exe", "application/octet-stream")


def test_quote_image_upload_accepts_valid_image_mime_without_extension() -> None:
    content_type, ext = _normalize_quote_image_content_type("upload", "image/png")

    assert content_type == "image/png"
    assert ext == ".png"
