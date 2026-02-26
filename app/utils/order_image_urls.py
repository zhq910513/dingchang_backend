# app/utils/order_image_urls.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, List

from app.core.slot_field_config import ordered_slot_keys, slot_is_multi_image, slot_title
from app.models.order import OrderImage
from app.services.storage import StorageService


# 走 OCR 的卡槽（与订单模块口径一致）
_OCR_SLOTS = {
    "vehicle_cert",
    "idcard_front",
    "idcard_back",
    "driving_license_main",
    "driving_license_sub",
}


def order_image_storage_key(im: OrderImage) -> str:
    sk = (getattr(im, "storage_key", "") or "").strip().lstrip("/")
    if sk:
        return sk
    imf = getattr(im, "image_file", None)
    sk2 = (getattr(imf, "storage_key", "") or "").strip().lstrip("/")
    return sk2


def display_url_for_order_image(im: OrderImage, storage: StorageService) -> str:
    """
    生成可展示 URL：
    - storage.enabled=True：优先签名 URL（短有效期），失败再尝试 public url
    - 否则回退到 OrderImage.image_url / ImageFile.url
    """
    sk = order_image_storage_key(im)
    if sk and getattr(storage, "enabled", False):
        try:
            return storage.object_url_for_display(sk, expires_in=60 * 60)
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
    """
    批量回填 image_url：
    - 同批次缓存：相同 storage_key 仅签一次（列表页性能/稳定性）
    """
    if not images:
        return

    cache: Dict[str, str] = {}

    for im in images:
        sk = order_image_storage_key(im)

        # 有 storage_key 且缓存命中：直接复用
        if sk and sk in cache:
            if cache[sk]:
                im.image_url = cache[sk]
            continue

        # 正常生成
        u = display_url_for_order_image(im, storage)

        # 写回 + 进缓存（仅对有 storage_key 的情况缓存）
        if u:
            im.image_url = u
        if sk:
            cache[sk] = u or ""


def _is_ocr_slot(slot_key: str) -> bool:
    return str(slot_key or "").strip() in _OCR_SLOTS


def _slot_meta(slot_key: str) -> Dict[str, Any]:
    sk = str(slot_key or "").strip()
    return {
        "slot_key": sk,
        "title": slot_title(sk),
        "multi": bool(slot_is_multi_image(sk)),
        "ocr": _is_ocr_slot(sk),
        "images": [],
    }


def safe_image_urls(order: Any, storage: StorageService) -> Dict[str, Any]:
    """
    标准卡槽输出（不返回兼容 _all / slot->string 映射）

    返回示例：
    {
      "vehicle_cert": {
        "slot_key": "vehicle_cert",
        "title": "车辆合格证",
        "multi": false,
        "ocr": true,
        "images": ["https://..."]
      },
      "related": {
        "slot_key": "related",
        "title": "相关图片",
        "multi": true,
        "ocr": false,
        "images": ["https://...", "https://..."]
      }
    }

    规则：
    - 固定按 slot_field_config 的顺序输出卡槽骨架（即使无图也返回空 images）
    - 未知 slot（脏数据/未来扩展）兜底追加到末尾
    - 所有槽位统一 images 数组口径（单图槽也是数组，前端按 multi 渲染）
    """
    imgs = getattr(order, "images", None) or []
    ensure_display_urls_for_order_images(imgs, storage)

    out: Dict[str, Any] = {}

    # 1) 先按配置生成固定骨架（标准输出）
    known_slot_keys: List[str] = ordered_slot_keys()
    for sk in known_slot_keys:
        sks = str(sk or "").strip()
        if not sks:
            continue
        out[sks] = _slot_meta(sks)

    # 2) 填充图片；未知 slot 兜底追加
    for im in imgs:
        url = (getattr(im, "image_url", None) or "").strip()
        if not url:
            continue

        slot_key = (getattr(im, "slot_key", None) or getattr(im, "slot", None) or "").strip()
        if not slot_key:
            slot_key = "unknown"

        if slot_key not in out:
            out[slot_key] = _slot_meta(slot_key)

        arr = out[slot_key].get("images")
        if not isinstance(arr, list):
            out[slot_key]["images"] = []
            arr = out[slot_key]["images"]
        arr.append(url)

    # 3) 单图槽收口：只保留最后一张（与 finalize 覆盖语义一致）
    for sk, node in out.items():
        if not isinstance(node, dict):
            continue
        if bool(node.get("multi", False)):
            continue
        arr = node.get("images")
        if not isinstance(arr, list):
            node["images"] = []
            continue
        if len(arr) > 1:
            node["images"] = [arr[-1]]

    return out
