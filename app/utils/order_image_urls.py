# app/utils/order_image_urls.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, List

from app.models.order import OrderImage
from app.services.storage import StorageService


# 多图卡槽（订单/财务通用）
_MULTI_SLOTS = {"related"}


def order_image_storage_key(im: OrderImage) -> str:
    sk = (getattr(im, "storage_key", "") or "").strip().lstrip("/")
    if sk:
        return sk
    imf = getattr(im, "image_file", None)
    sk2 = (getattr(imf, "storage_key", "") or "").strip().lstrip("/")
    return sk2


def display_url_for_order_image(im: OrderImage, storage: StorageService) -> str:
    """
    生成“可展示 URL”：
    - storage.enabled=True：优先签名 URL（短有效期），失败再尝试 public url
    - 否则回退到 OrderImage.image_url / ImageFile.url
    """
    sk = order_image_storage_key(im)
    if sk and getattr(storage, "enabled", False):
        try:
            return storage.object_url_for_display(sk, expires_in=60*60)
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
    ✅ 批量回填 image_url：
    - 增加同批次缓存：相同 storage_key 仅签一次（列表页性能/稳定性）
    """
    if not images:
        return

    cache: Dict[str, str] = {}

    for im in images:
        sk = order_image_storage_key(im)

        # 1) 有 storage_key 且缓存命中：直接复用
        if sk and sk in cache:
            if cache[sk]:
                im.image_url = cache[sk]
            continue

        # 2) 正常生成
        u = display_url_for_order_image(im, storage)

        # 3) 写回 + 进缓存（仅对有 storage_key 的情况缓存）
        if u:
            im.image_url = u
        if sk:
            cache[sk] = u or ""


def safe_image_urls(order: Any, storage: StorageService) -> Dict[str, Any]:
    """
    ✅ 返回“按卡槽组织”的图片链接，便于详情页按卡槽渲染

    返回示例：
    {
      "id_card_front": "https://...",
      "driving_license": "https://...",
      "related": ["https://...", "https://..."],
      "_all": ["https://...", "https://..."]   # 兼容旧前端的平铺兜底
    }

    说明：
    - 单图卡槽：返回字符串
    - 多图卡槽（如 related）：返回数组
    - 同时附带 _all 平铺列表，降低前端兼容成本
    """
    imgs = getattr(order, "images", None) or []
    ensure_display_urls_for_order_images(imgs, storage)

    out: Dict[str, Any] = {}
    all_urls: List[str] = []

    for im in imgs:
        url = (getattr(im, "image_url", None) or "").strip()
        if not url:
            continue

        slot_key = (getattr(im, "slot_key", None) or getattr(im, "slot", None) or "").strip()
        if not slot_key:
            # 无 slot 的脏数据也别丢，放 unknown
            slot_key = "unknown"

        all_urls.append(url)

        if slot_key in _MULTI_SLOTS:
            arr = out.get(slot_key)
            if not isinstance(arr, list):
                arr = []
                out[slot_key] = arr
            arr.append(url)
        else:
            # 单图卡槽：同 slot 多张时，默认保留最后一张（符合 finalize 单图槽覆盖语义）
            out[slot_key] = url

    out["_all"] = all_urls
    return out
