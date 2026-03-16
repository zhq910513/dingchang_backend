# app/utils/order_image_urls.py
# encoding: utf-8
from __future__ import annotations

"""订单图片卡槽输出（唯一口径）

- 只产出 schemas.order.SlotImageNodeOut / SlotImageItemOut 所需字段
- 不再输出旧结构（slot -> url list / image_urls 等）
- 不做任何历史字段兼容（dl_* 等）

输出结构（固定字段，不多不少）：
slot_images: List[{
  slot_key, title, multi, ocr,
  images: List[{order_image_id, image_file_id, storage_key, url, md5, etag, size, content_type, original_name, created_at, updated_at}]
}]
"""

from typing import Any, Dict, List
from typing import Protocol, runtime_checkable

from app.core.slot_field_config import ordered_slot_keys, slot_is_multi_image, slot_title, slot_is_ocr


@runtime_checkable
class _StorageProto(Protocol):
    enabled: bool

    @staticmethod
    def object_url_for_display(key: str, expires_in: int = 3600) -> str:
        return "" if (key or expires_in) else ""

    @staticmethod
    def object_public_url(key: str) -> str:
        return "" if key else ""


def _norm_str(v: Any) -> str:
    return (str(v or "").strip()) if v is not None else ""


def _storage_key_for_order_image(im: Any) -> str:
    sk = _norm_str(getattr(im, "storage_key", "")).lstrip("/")
    if sk:
        return sk
    imf = getattr(im, "image_file", None)
    sk2 = _norm_str(getattr(imf, "storage_key", "")).lstrip("/")
    return sk2


def _display_url_for_storage_key(storage_key: str, storage: _StorageProto) -> str:
    if not storage_key:
        return ""
    if not getattr(storage, "enabled", False):
        return ""
    try:
        return storage.object_url_for_display(storage_key, expires_in=60 * 60)
    except Exception:
        try:
            return storage.object_public_url(storage_key)
        except Exception:
            return ""


def _display_url_for_order_image(im: Any, storage: _StorageProto) -> str:
    sk = _storage_key_for_order_image(im)
    u = _display_url_for_storage_key(sk, storage)
    if u:
        return u

    url = _norm_str(getattr(im, "image_url", ""))
    if url:
        return url

    imf = getattr(im, "image_file", None)
    url2 = _norm_str(getattr(imf, "url", ""))
    return url2


def ensure_display_urls_for_order_images(images: List[Any], storage: _StorageProto) -> None:
    if not images:
        return

    cache: Dict[str, str] = {}

    for im in images:
        sk = _storage_key_for_order_image(im)

        if sk and sk in cache:
            if cache[sk]:
                try:
                    im.image_url = cache[sk]
                except Exception:
                    pass
            continue

        u = _display_url_for_order_image(im, storage)

        if u:
            try:
                im.image_url = u
            except Exception:
                pass
        if sk:
            cache[sk] = u or ""


def build_slot_images(order: Any, storage: _StorageProto) -> List[Dict[str, Any]]:
    imgs: List[Any] = getattr(order, "images", None) or []
    ensure_display_urls_for_order_images(imgs, storage)

    nodes: List[Dict[str, Any]] = []
    node_map: Dict[str, Dict[str, Any]] = {}

    for sk in ordered_slot_keys():
        sks = _norm_str(sk)
        if not sks:
            continue
        node = {
            "slot_key": sks,
            "title": slot_title(sks),
            "multi": bool(slot_is_multi_image(sks)),
            "ocr": bool(slot_is_ocr(sks)),
            "images": [],
        }
        nodes.append(node)
        node_map[sks] = node

    for im in imgs:
        slot_key = _norm_str(getattr(im, "slot_key", None) or getattr(im, "slot", None) or "") or "unknown"
        if slot_key not in node_map:
            node = {
                "slot_key": slot_key,
                "title": slot_title(slot_key),
                "multi": bool(slot_is_multi_image(slot_key)),
                "ocr": bool(slot_is_ocr(slot_key)),
                "images": [],
            }
            nodes.append(node)
            node_map[slot_key] = node

        imf = getattr(im, "image_file", None)

        item = {
            "order_image_id": getattr(im, "id", None),
            "image_file_id": getattr(im, "image_file_id", None),
            "storage_key": _storage_key_for_order_image(im),
            "url": _norm_str(getattr(im, "image_url", "")),
            "md5": _norm_str(getattr(imf, "md5", None)),
            "etag": _norm_str(getattr(imf, "etag", None)),
            "size": getattr(imf, "size", None),
            "content_type": _norm_str(getattr(imf, "content_type", None)),
            "original_name": _norm_str(getattr(imf, "original_name", None)),
            "created_at": _norm_str(getattr(im, "created_at", None)),
            "updated_at": _norm_str(getattr(im, "updated_at", None)),
        }
        node_map[slot_key]["images"].append(item)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if bool(node.get("multi", False)):
            continue
        arr = node.get("images")
        if isinstance(arr, list) and len(arr) > 1:
            node["images"] = [arr[-1]]

    return nodes