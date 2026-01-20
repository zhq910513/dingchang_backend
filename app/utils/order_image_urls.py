# app/utils/order_image_urls.py
# encoding: utf-8
from __future__ import annotations

from typing import List, Optional, Any

from app.models.order import OrderImage
from app.services.storage import StorageService


def order_image_storage_key(im: OrderImage) -> str:
    sk = (getattr(im, "storage_key", "") or "").strip().lstrip("/")
    if sk:
        return sk
    imf = getattr(im, "image_file", None)
    sk2 = (getattr(imf, "storage_key", "") or "").strip().lstrip("/")
    return sk2


def display_url_for_order_image(im: OrderImage, storage: StorageService) -> str:
    sk = order_image_storage_key(im)
    if sk and getattr(storage, "enabled", False):
        try:
            return storage.object_url_for_display(sk, expires_in=900)
        except Exception:
            try:
                return storage.object_public_url(sk)
            except Exception:
                pass

    url = (getattr(im, "image_url", "") or "").strip()
    if url:
        return url
    imf = getattr(im, "image_file", None)
    url2 = (getattr(imf, "url", "") or "").strip()
    return url2


def ensure_display_urls_for_order_images(images: List[OrderImage], storage: StorageService) -> None:
    if not images:
        return
    for im in images:
        u = display_url_for_order_image(im, storage)
        if u:
            im.image_url = u


def safe_image_urls(order: Any, storage: StorageService) -> list[str]:
    imgs = getattr(order, "images", None) or []
    ensure_display_urls_for_order_images(imgs, storage)
    out: list[str] = []
    for im in imgs:
        url = (getattr(im, "image_url", None) or "").strip()
        if url:
            out.append(url)
    return out
