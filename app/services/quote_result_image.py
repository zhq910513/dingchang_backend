from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from app.services.storage import StorageService


_storage = StorageService()


def _to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _num_text(value: Any, *, money: bool = False) -> str:
    if value in (None, ""):
        return "0.00" if money else "-"
    try:
        n = float(_to_str(value).replace(",", ""))
    except Exception:
        return _to_str(value)
    if money:
        return f"{n:.2f}"
    if n.is_integer():
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def _safe_card_payload(card: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(dict(card or {}), ensure_ascii=False, default=str))


def _quote_result_rel_path(card: Mapping[str, Any], trace_id: str = "") -> Path:
    raw = json.dumps({"trace_id": trace_id, "card": _safe_card_payload(card)}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return Path(_storage.build_key_by_md5(scene="related", md5_hex=digest, ext=".png"))


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_bbox(draw: Any, xy: Tuple[int, int], text: str, font: Any) -> Tuple[int, int, int, int]:
    try:
        return draw.textbbox(xy, text, font=font)
    except Exception:
        width = int(draw.textlength(text, font=font))
        return xy[0], xy[1], xy[0] + width, xy[1] + int(getattr(font, "size", 14))


def _text_width(draw: Any, text: str, font: Any) -> int:
    box = _text_bbox(draw, (0, 0), text, font)
    return max(0, int(box[2] - box[0]))


def _draw_text(
    draw: Any,
    xy: Tuple[int, int],
    text: Any,
    *,
    font: Any,
    fill: str = "#000000",
    anchor: Optional[str] = None,
) -> None:
    draw.text(xy, _to_str(text), font=font, fill=fill, anchor=anchor)


def _draw_right(draw: Any, right: int, y: int, text: Any, *, font: Any, fill: str = "#000000") -> None:
    value = _to_str(text)
    _draw_text(draw, (right - _text_width(draw, value, font), y), value, font=font, fill=fill)


def _fit_text(draw: Any, text: Any, *, font: Any, max_width: int) -> str:
    value = _to_str(text)
    if _text_width(draw, value, font) <= max_width:
        return value
    ellipsis = "..."
    while value and _text_width(draw, value + ellipsis, font) > max_width:
        value = value[:-1]
    return (value + ellipsis) if value else ellipsis


def _draw_badge(draw: Any, x: int, y: int, label: str, color: str, font: Any) -> None:
    draw.ellipse((x, y, x + 16, y + 16), fill=color, outline="#ffffff", width=1)
    _draw_text(draw, (x + 8, y + 7), label, font=font, fill="#ffffff", anchor="mm")


def _draw_rotated_watermark(image: Any, text: str, x: int, y: int, *, font: Any) -> None:
    from PIL import Image, ImageDraw

    box_width = max(260, _text_width(ImageDraw.Draw(image), text, font) + 24)
    layer = Image.new("RGBA", (box_width, 70), (255, 255, 255, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.text((0, 0), text, font=font, fill=(153, 153, 153, 52))
    try:
        resample = Image.Resampling.BICUBIC
    except Exception:
        resample = Image.BICUBIC
    rotated = layer.rotate(-22, expand=True, resample=resample)
    image.paste(rotated, (x, y), rotated)


def _coverage_items(card: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    rows = card.get("coverage_items")
    return rows if isinstance(rows, list) else []


def render_quote_result_card_png(card: Mapping[str, Any]) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    safe_card = _safe_card_payload(card)
    rows = [dict(x) for x in _coverage_items(safe_card) if isinstance(x, Mapping)]
    width = 567
    coverage_height = max(0, len(rows)) * 25
    height = max(557, 338 + coverage_height)
    image = Image.new("RGB", (width, height), "#fff4f2")
    draw = ImageDraw.Draw(image)

    font12 = _font(12)
    font14 = _font(14)
    font15 = _font(15)
    font16b = _font(16, bold=True)
    font28b = _font(28, bold=True)

    draw.rectangle((0, 0, width, 31), fill="#eeeeee")
    draw.rectangle((0, 31, 18, height), fill="#e7e7e7")
    draw.line((18, 31, 18, height), fill="#f0c8be", width=1)

    watermark = _to_str(safe_card.get("watermark_text")).strip()
    if not watermark:
        account = _to_str(safe_card.get("watermark_user") or safe_card.get("watermark_account") or "报价助手").strip()
        watermark = f"{account} {_to_str(safe_card.get('watermark_time')).strip()}".strip()
    for x, y in [(38, 132), (178, 66), (392, 88), (246, 230), (48, 360), (286, 456), (404, 332)]:
        _draw_rotated_watermark(image, watermark, x, y, font=font15)

    ribbon = [(48, 16), (221, 16), (228, 35), (221, 54), (48, 54), (41, 35)]
    draw.polygon(ribbon, fill="#e84f42")
    draw.line((47, 18, 222, 18), fill="#ffffff", width=1)
    _draw_text(draw, (78, 35), "3", font=font28b, fill="#f8b0aa", anchor="mm")
    _draw_text(draw, (98, 26), _to_str(safe_card.get("title") or "报价结果"), font=font16b, fill="#ffffff")
    draw.rectangle((277, 35, 292, 50), outline="#3157ff", width=1)
    draw.line((280, 42, 285, 47, 290, 38), fill="#3157ff", width=2)
    _draw_text(draw, (298, 35), "含税", font=font14)

    left = 104
    y = 55
    col1 = 166
    col2 = 90
    col3 = 104
    right = left + col1 + col2 + col3
    line = "#b2b2b2"

    _draw_text(draw, (left, y), "险别名称", font=font14)
    _draw_right(draw, left + col1 + col2, y, "保额(元)", font=font14)
    _draw_right(draw, right, y, "保费(元)", font=font14)
    y += 30

    _draw_badge(draw, left, y + 3, "总", "#ff5146", font12)
    _draw_text(draw, (left + 18, y + 3), "总保费", font=font16b)
    _draw_right(draw, right, y + 3, _num_text(safe_card.get("total_premium"), money=True), font=font16b, fill="#ff0000")
    y += 35
    draw.line((left, y, right, y), fill=line, width=1)

    _draw_badge(draw, left, y + 9, "商", "#4f92ff", font12)
    _draw_text(draw, (left + 18, y + 9), "商业险", font=font16b)
    _draw_right(draw, right, y + 9, _num_text(safe_card.get("commercial_premium"), money=True), font=font16b, fill="#ff0000")
    y += 40

    for idx, row in enumerate(rows):
        bg = "#f9dfdd" if idx % 2 == 0 else "#fff8f6"
        draw.rectangle((left, y, left + col1, y + 24), fill=bg)
        draw.rectangle((left + col1 + 3, y, left + col1 + col2, y + 24), fill=bg)
        draw.rectangle((left + col1 + col2 + 3, y, right, y + 24), fill=bg)
        name = _to_str(row.get("name")).replace("机动车车上人员责任保险", "车上人员责任险")
        _draw_text(draw, (left + 10, y + 4), _fit_text(draw, name, font=font14, max_width=col1 - 20), font=font14)
        _draw_right(draw, left + col1 + col2 - 8, y + 4, _num_text(row.get("amount")), font=font14)
        _draw_right(draw, right - 8, y + 4, _num_text(row.get("premium"), money=True), font=font14)
        y += 25

    y += 9
    draw.line((left, y, right, y), fill=line, width=1)
    _draw_badge(draw, left, y + 9, "交", "#caa86c", font12)
    _draw_text(draw, (left + 18, y + 9), "交强险", font=font16b)
    _draw_text(draw, (left + 78, y + 10), "增值税", font=font14, fill="#0065ff")
    _draw_right(draw, right, y + 9, _num_text(safe_card.get("compulsory_premium"), money=True), font=font16b, fill="#ff0000")
    y += 42

    tax_detail_raw = safe_card.get("vehicle_tax_detail")
    tax_detail = tax_detail_raw if isinstance(tax_detail_raw, Mapping) else {}
    _draw_text(draw, (left, y), "车船税", font=font14)
    _draw_text(draw, (left + 56, y), "详情", font=font14, fill="#005eff")
    _draw_text(draw, (left + 94, y + 1), "▲", font=font12, fill="#005eff")
    if not tax_detail:
        _draw_right(draw, right, y, _num_text(safe_card.get("vehicle_tax"), money=True), font=font16b)
    y += 25
    if tax_detail:
        _draw_text(draw, (left + 12, y), f"当年应缴{_num_text(tax_detail.get('current'), money=True)}", font=font14, fill="#7f899c")
        _draw_text(draw, (left + 160, y), f"往年补缴{_num_text(tax_detail.get('back'), money=True)}", font=font14, fill="#7f899c")
        _draw_text(draw, (left + 310, y), f"滞纳金{_num_text(tax_detail.get('late_fee'), money=True)}", font=font14, fill="#7f899c")
        y += 25

    for label, key in [("联合销售", "joint_sales_premium"), ("驾意险", "driver_accident_premium")]:
        _draw_text(draw, (left, y), label, font=font14)
        _draw_right(draw, right, y, _num_text(safe_card.get(key), money=True), font=font16b, fill="#ff0000")
        y += 25

    draw.line((left, y + 2, right, y + 2), fill=line, width=1)
    _draw_text(draw, (left, y + 12), "承保条件改善", font=font14, fill="#005eff")
    y += 42

    _draw_text(draw, (left, y), "理赔信息", font=font14)
    _draw_badge(draw, left + 118, y, "商", "#4f92ff", font12)
    _draw_text(draw, (left + 138, y), safe_card.get("claim_business_count", 0), font=font14)
    _draw_badge(draw, left + 184, y, "交", "#caa86c", font12)
    _draw_text(draw, (left + 204, y), safe_card.get("claim_compulsory_count", 0), font=font14)
    _draw_text(draw, (left + 270, y), "理赔查询", font=font14, fill="#005eff")
    y += 32

    draw.line((left, y, right, y), fill=line, width=1)
    _draw_text(draw, (left, y + 10), "人保风险水平", font=font14)
    _draw_right(draw, right, y + 8, f"{_to_str(safe_card.get('risk_score') or '-')} 分", font=font16b, fill="#ff0000")

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def save_quote_result_card_image(card: Mapping[str, Any], *, trace_id: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(card, Mapping) or not card:
        return None
    rel_path = _quote_result_rel_path(card, trace_id=trace_id)
    storage_key = rel_path.as_posix()
    png_bytes = render_quote_result_card_png(card)
    _storage.put_object(
        storage_key,
        data=png_bytes,
        content_type="image/png",
    )
    url = _storage.object_public_url(storage_key)
    return {
        "kind": "quote_result",
        "slot_key": "related",
        "storage_key": storage_key,
        "url": url,
        "image_url": url,
        "preview_url": url,
        "content_type": "image/png",
        "provider": "bos",
        "size": len(png_bytes),
    }
