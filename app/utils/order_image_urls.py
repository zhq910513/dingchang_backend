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
  images: List[{order_image_id, image_file_id, storage_key, url, md5, etag, size, content_type, original_name,
                created_at, updated_at}]
}]

性能收敛（2026-03-23）：
- 增加进程内展示 URL TTL 缓存，按 storage_key 复用
- 当前请求内保留局部去重缓存

安全收口（2026-05-03）：
- 详情展示 URL 统一走 StorageService.object_url_for_display(signed=None)
- 不再强制公开直链；库中历史 URL 仅在无法生成存储展示 URL 时作为兼容回退
"""

from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable
import time

from app.core.slot_field_config import (
    ordered_slot_keys,
    slot_is_multi_image,
    slot_is_ocr,
    slot_title,
)

_DISPLAY_URL_CACHE_TTL_SECONDS = 45 * 60
_DISPLAY_URL_CACHE_MAX_SIZE = 2048
_DISPLAY_URL_CACHE: Dict[str, Tuple[float, str]] = {}


@runtime_checkable
class _StorageProto(Protocol):
    enabled: bool

    @staticmethod
    def object_url_for_display(
            key: str,
            signed: Optional[bool] = None,
            expires_in: int = 3600,
    ) -> str:
        return "" if (key or signed or expires_in) else ""

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


def _get_cached_display_url(storage_key: str) -> Optional[str]:
    if not storage_key:
        return None

    cached = _DISPLAY_URL_CACHE.get(storage_key)
    if cached is None:
        return None

    expires_at, url = cached
    now_ts = time.monotonic()
    if now_ts >= expires_at:
        _DISPLAY_URL_CACHE.pop(storage_key, None)
        return None

    return url or ""


def _prune_display_url_cache_if_needed() -> None:
    if len(_DISPLAY_URL_CACHE) < _DISPLAY_URL_CACHE_MAX_SIZE:
        return

    now_ts = time.monotonic()
    stale_keys = [
        key
        for key, (expires_at, _) in _DISPLAY_URL_CACHE.items()
        if now_ts >= expires_at
    ]
    for key in stale_keys:
        _DISPLAY_URL_CACHE.pop(key, None)

    if len(_DISPLAY_URL_CACHE) < _DISPLAY_URL_CACHE_MAX_SIZE:
        return

    overflow_count = len(_DISPLAY_URL_CACHE) - _DISPLAY_URL_CACHE_MAX_SIZE + 1
    if overflow_count <= 0:
        return

    oldest_keys = sorted(
        _DISPLAY_URL_CACHE.items(),
        key=lambda item: item[1][0],
    )[:overflow_count]
    for key, _ in oldest_keys:
        _DISPLAY_URL_CACHE.pop(key, None)


def _set_cached_display_url(storage_key: str, url: str) -> None:
    if not storage_key or not url:
        return

    _prune_display_url_cache_if_needed()
    expires_at = time.monotonic() + float(_DISPLAY_URL_CACHE_TTL_SECONDS)
    _DISPLAY_URL_CACHE[storage_key] = (expires_at, url)


def _fallback_url_for_order_image(im: Any) -> str:
    url = _norm_str(getattr(im, "image_url", ""))
    if url:
        return url

    imf = getattr(im, "image_file", None)
    if imf is None:
        return ""

    return _norm_str(getattr(imf, "url", ""))


def _display_url_for_storage_key(storage_key: str, storage: _StorageProto) -> str:
    if not storage_key:
        return ""
    if not getattr(storage, "enabled", False):
        return ""

    cached = _get_cached_display_url(storage_key)
    if cached is not None:
        return cached

    try:
        url = storage.object_url_for_display(
            storage_key,
            signed=None,
            expires_in=60 * 60,
            allow_fallback_public=False,
        )
    except TypeError:
        try:
            url = storage.object_url_for_display(
                storage_key,
                signed=None,
                expires_in=60 * 60,
            )
        except Exception:
            url = ""
    except Exception:
        url = ""

    if url:
        _set_cached_display_url(storage_key, url)

    return url


def _display_url_for_order_image(im: Any, storage: _StorageProto) -> str:
    storage_key = _storage_key_for_order_image(im)
    if storage_key:
        cached = _get_cached_display_url(storage_key)
        if cached is not None:
            return cached

        storage_url = _display_url_for_storage_key(storage_key, storage)
        if storage_url:
            return storage_url

    fallback_url = _fallback_url_for_order_image(im)
    if fallback_url:
        if storage_key:
            _set_cached_display_url(storage_key, fallback_url)
        return fallback_url

    return ""


def ensure_display_urls_for_order_images(images: List[Any], storage: _StorageProto) -> None:
    if not images:
        return

    request_cache: Dict[str, str] = {}
    storage_enabled = bool(getattr(storage, "enabled", False))

    for im in images:
        storage_key = _storage_key_for_order_image(im)
        if storage_key:
            cached = request_cache.get(storage_key)
            if cached is not None:
                if cached:
                    try:
                        im.image_url = cached
                    except Exception:
                        pass
                continue

        url = ""
        if storage_key:
            cached_global = _get_cached_display_url(storage_key)
            if cached_global is not None:
                url = cached_global

        if not url and storage_key and storage_enabled:
            url = _display_url_for_storage_key(storage_key, storage)

        if not url:
            url = _norm_str(getattr(im, "image_url", ""))

        if not url:
            imf = getattr(im, "image_file", None)
            if imf is not None:
                url = _norm_str(getattr(imf, "url", ""))

        if url:
            try:
                im.image_url = url
            except Exception:
                pass

        if storage_key:
            request_cache[storage_key] = url or ""
            if url:
                _set_cached_display_url(storage_key, url)


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
    images: List[Any] = getattr(order, "images", None) or []
    ensure_display_urls_for_order_images(images, storage)

    ordered_keys, slot_meta_map = _build_slot_meta_map()

    nodes: List[Dict[str, Any]] = []
    node_map: Dict[str, Dict[str, Any]] = {}

    append_node = nodes.append

    for slot_key in ordered_keys:
        meta = slot_meta_map[slot_key]
        node = _new_slot_node(meta)
        append_node(node)
        node_map[slot_key] = node

    for im in images:
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

        image_file = getattr(im, "image_file", None)

        storage_key = _storage_key_for_order_image(im)
        url = _norm_str(getattr(im, "image_url", ""))

        item = {
            "order_image_id": getattr(im, "id", None),
            "image_file_id": getattr(im, "image_file_id", None),
            "storage_key": storage_key,
            "url": url,
            "md5": _norm_str(getattr(image_file, "md5", None)) if image_file is not None else "",
            "etag": _norm_str(getattr(image_file, "etag", None)) if image_file is not None else "",
            "size": getattr(image_file, "size", None) if image_file is not None else None,
            "content_type": (
                _norm_str(getattr(image_file, "content_type", None))
                if image_file is not None else ""
            ),
            "original_name": (
                _norm_str(getattr(image_file, "original_name", None))
                if image_file is not None else ""
            ),
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
