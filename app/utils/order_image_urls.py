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

from typing import Any, Dict, List, Tuple
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
    if v is None:
        return ""
    return str(v).strip()


def _storage_key_for_order_image(im: Any) -> str:
    sk = _norm_str(getattr(im, "storage_key", "")).lstrip("/")
    if sk:
        return sk

    imf = getattr(im, "image_file", None)
    if imf is None:
        return ""

    return _norm_str(getattr(imf, "storage_key", "")).lstrip("/")


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
    if sk:
        u = _display_url_for_storage_key(sk, storage)
        if u:
            return u

    url = _norm_str(getattr(im, "image_url", ""))
    if url:
        return url

    imf = getattr(im, "image_file", None)
    if imf is None:
        return ""

    return _norm_str(getattr(imf, "url", ""))


def ensure_display_urls_for_order_images(images: List[Any], storage: _StorageProto) -> None:
    if not images:
        return

    cache: Dict[str, str] = {}
    storage_enabled = bool(getattr(storage, "enabled", False))

    for im in images:
        sk = _storage_key_for_order_image(im)

        if sk:
            cached = cache.get(sk)
            if cached is not None:
                if cached:
                    try:
                        im.image_url = cached
                    except Exception:
                        pass
                continue

        u = ""
        if sk and storage_enabled:
            u = _display_url_for_storage_key(sk, storage)

        if not u:
            u = _norm_str(getattr(im, "image_url", ""))
            if not u:
                imf = getattr(im, "image_file", None)
                if imf is not None:
                    u = _norm_str(getattr(imf, "url", ""))

        if u:
            try:
                im.image_url = u
            except Exception:
                pass

        if sk:
            cache[sk] = u or ""


def _build_slot_meta_map() -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    ordered_keys: List[str] = []
    meta_map: Dict[str, Dict[str, Any]] = {}

    for sk in ordered_slot_keys():
        sks = _norm_str(sk)
        if not sks:
            continue
        ordered_keys.append(sks)
        meta_map[sks] = {
            "slot_key": sks,
            "title": slot_title(sks),
            "multi": bool(slot_is_multi_image(sks)),
            "ocr": bool(slot_is_ocr(sks)),
        }

    return ordered_keys, meta_map


def _new_slot_node(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slot_key": meta["slot_key"],
        "title": meta["title"],
        "multi": meta["multi"],
        "ocr": meta["ocr"],
        "images": [],
    }


def build_slot_images(order: Any, storage: _StorageProto) -> List[Dict[str, Any]]:
    imgs: List[Any] = getattr(order, "images", None) or []
    ensure_display_urls_for_order_images(imgs, storage)

    ordered_keys, slot_meta_map = _build_slot_meta_map()

    nodes: List[Dict[str, Any]] = []
    node_map: Dict[str, Dict[str, Any]] = {}

    append_node = nodes.append

    for sk in ordered_keys:
        meta = slot_meta_map[sk]
        node = _new_slot_node(meta)
        append_node(node)
        node_map[sk] = node

    for im in imgs:
        slot_key = _norm_str(getattr(im, "slot_key", None) or getattr(im, "slot", None) or "")
        if not slot_key:
            slot_key = "unknown"

        node = node_map.get(slot_key)
        if node is None:
            meta = slot_meta_map.get(slot_key)
            if meta is None:
                meta = {
                    "slot_key": slot_key,
                    "title": slot_title(slot_key),
                    "multi": bool(slot_is_multi_image(slot_key)),
                    "ocr": bool(slot_is_ocr(slot_key)),
                }
                slot_meta_map[slot_key] = meta

            node = _new_slot_node(meta)
            append_node(node)
            node_map[slot_key] = node

        imf = getattr(im, "image_file", None)

        storage_key = _storage_key_for_order_image(im)
        url = _norm_str(getattr(im, "image_url", ""))

        item = {
            "order_image_id": getattr(im, "id", None),
            "image_file_id": getattr(im, "image_file_id", None),
            "storage_key": storage_key,
            "url": url,
            "md5": _norm_str(getattr(imf, "md5", None)) if imf is not None else "",
            "etag": _norm_str(getattr(imf, "etag", None)) if imf is not None else "",
            "size": getattr(imf, "size", None) if imf is not None else None,
            "content_type": _norm_str(getattr(imf, "content_type", None)) if imf is not None else "",
            "original_name": _norm_str(getattr(imf, "original_name", None)) if imf is not None else "",
            "created_at": _norm_str(getattr(im, "created_at", None)),
            "updated_at": _norm_str(getattr(im, "updated_at", None)),
        }
        node["images"].append(item)

    for node in nodes:
        if bool(node.get("multi", False)):
            continue
        arr = node.get("images")
        if isinstance(arr, list) and len(arr) > 1:
            node["images"] = [arr[-1]]

    return nodes
