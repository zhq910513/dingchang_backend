from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

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
        return "-"
    try:
        n = float(_to_str(value).replace(",", ""))
    except Exception:
        return _to_str(value)
    if money:
        return f"{n:.2f}"
    if n.is_integer():
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def _money_yuan(value: Any, *, zero: str = "-") -> str:
    if value in (None, ""):
        return zero
    return f"{_num_text(value, money=True)}元"


def _safe_card_payload(card: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(dict(card or {}), ensure_ascii=False, default=str))


def _render_scale() -> int:
    try:
        value = int(float(os.getenv("QUOTE_RESULT_IMAGE_SCALE", "2") or "2"))
    except Exception:
        value = 2
    return max(1, min(3, value))


def _quote_result_target_size() -> Tuple[int, int]:
    try:
        width = int(float(os.getenv("QUOTE_RESULT_IMAGE_WIDTH", "1350") or "1350"))
    except Exception:
        width = 1350
    try:
        height = int(float(os.getenv("QUOTE_RESULT_IMAGE_HEIGHT", "650") or "650"))
    except Exception:
        height = 650
    return max(1, width), max(1, height)


def _s(value: Any, scale: int) -> int:
    try:
        return int(round(float(value) * scale))
    except Exception:
        return 0


def _line_width(scale: int, value: int = 1) -> int:
    return max(1, _s(value, scale))


def _scaled_points(points: Iterable[Tuple[int, int]], scale: int) -> List[Tuple[int, int]]:
    return [(_s(x, scale), _s(y, scale)) for x, y in points]


def _resize_resample_filter() -> Any:
    from PIL import Image

    try:
        return Image.Resampling.LANCZOS
    except Exception:
        return Image.LANCZOS


def _fit_quote_result_image(image: Any, *, background: str = "#ffffff") -> Any:
    from PIL import Image

    target_w, target_h = _quote_result_target_size()
    if not image:
        return Image.new("RGB", (target_w, target_h), background)
    source = image.convert("RGB")
    if source.size == (target_w, target_h):
        return source

    source_w, source_h = source.size
    if source_w <= 0 or source_h <= 0:
        return Image.new("RGB", (target_w, target_h), background)
    ratio = min(target_w / source_w, target_h / source_h)
    resized_w = max(1, int(round(source_w * ratio)))
    resized_h = max(1, int(round(source_h * ratio)))
    resized = source.resize((resized_w, resized_h), _resize_resample_filter())
    canvas = Image.new("RGB", (target_w, target_h), background)
    canvas.paste(resized, ((target_w - resized_w) // 2, (target_h - resized_h) // 2))
    return canvas


def _png_bytes(image: Any, *, background: str = "#ffffff") -> bytes:
    from io import BytesIO

    output = BytesIO()
    final_image = _fit_quote_result_image(image, background=background)
    final_image.save(output, format="PNG", compress_level=4)
    return output.getvalue()


def _quote_result_rel_path(card: Mapping[str, Any], trace_id: str = "") -> Path:
    target_w, target_h = _quote_result_target_size()
    raw = json.dumps(
        {
            "trace_id": trace_id,
            "render_scale": _render_scale(),
            "target_size": [target_w, target_h],
            "card": _safe_card_payload(card),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return Path(_storage.build_key_by_md5(scene="related", md5_hex=digest, ext=".png"))


def _font(size: int, *, bold: bool = False, serif: bool = False):
    from PIL import ImageFont

    if serif:
        candidates = [
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/simfang.ttf",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
        ]
    else:
        candidates = []
    candidates.extend(
        [
            "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ]
    )
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


def _text_height(draw: Any, text: str, font: Any) -> int:
    box = _text_bbox(draw, (0, 0), text or "国", font)
    return max(1, int(box[3] - box[1]))


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


def _wrap_text(draw: Any, text: Any, *, font: Any, max_width: int, max_lines: int = 3) -> List[str]:
    value = _to_str(text).strip()
    if not value:
        return [""]
    lines: List[str] = []
    current = ""
    for ch in value:
        candidate = current + ch
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = ch
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if lines and _text_width(draw, lines[-1], font) > max_width:
        lines[-1] = _fit_text(draw, lines[-1], font=font, max_width=max_width)
    if len(lines) == max_lines and "".join(lines) != value:
        lines[-1] = _fit_text(draw, lines[-1], font=font, max_width=max_width)
    return lines or [""]


def _draw_cell_text(
    draw: Any,
    box: Tuple[int, int, int, int],
    text: Any,
    *,
    font: Any,
    fill: str = "#000000",
    bold_font: Any = None,
    align: str = "center",
    max_lines: int = 3,
) -> None:
    left, top, right, bottom = box
    use_font = bold_font or font
    font_size = max(12, int(getattr(use_font, "size", 14) or 14))
    pad_x = max(8, int(round(font_size * 0.7)))
    inner_x = max(4, int(round(font_size * 0.4)))
    line_gap = max(4, int(round(font_size * 0.2)))
    lines = _wrap_text(draw, text, font=use_font, max_width=max(10, right - left - pad_x), max_lines=max_lines)
    line_height = _text_height(draw, "国", use_font) + line_gap
    total_height = line_height * len(lines) - line_gap
    y = top + max(0, (bottom - top - total_height) // 2)
    for line in lines:
        if align == "left":
            x = left + inner_x
            anchor = "lm"
        elif align == "right":
            x = right - inner_x
            anchor = "rm"
        else:
            x = (left + right) // 2
            anchor = "mm"
        _draw_text(draw, (x, y + line_height // 2), line, font=use_font, fill=fill, anchor=anchor)
        y += line_height


def _draw_badge(draw: Any, x: int, y: int, label: str, color: str, font: Any) -> None:
    size = max(16, int(getattr(font, "size", 12) or 12) + 4)
    draw.ellipse((x, y, x + size, y + size), fill=color, outline="#ffffff", width=max(1, size // 16))
    _draw_text(draw, (x + size // 2, y + size // 2 - max(0, size // 16)), label, font=font, fill="#ffffff", anchor="mm")


def _coverage_items(card: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    rows = card.get("coverage_items")
    return rows if isinstance(rows, list) else []


def _render_legacy_quote_card_png(card: Mapping[str, Any]) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    safe_card = _safe_card_payload(card)
    rows = [dict(x) for x in _coverage_items(safe_card) if isinstance(x, Mapping)]
    width = 567
    coverage_height = max(0, len(rows)) * 25
    height = max(582, 363 + coverage_height)
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
    _draw_text(draw, (left + 94, y + 1), "▼", font=font12, fill="#005eff")
    if not tax_detail:
        _draw_right(draw, right, y, _num_text(safe_card.get("vehicle_tax"), money=True), font=font16b)
    y += 25
    if tax_detail:
        _draw_text(draw, (left + 12, y), f"当年应缴{_num_text(tax_detail.get('current'), money=True)}", font=font14, fill="#7f899c")
        _draw_text(draw, (left + 160, y), f"往年补缴{_num_text(tax_detail.get('back'), money=True)}", font=font14, fill="#7f899c")
        _draw_text(draw, (left + 310, y), f"滞纳金{_num_text(tax_detail.get('late_fee'), money=True)}", font=font14, fill="#7f899c")
        y += 25

    joint_premium = _to_str(safe_card.get("joint_sales_premium")).strip()
    joint_amount = _to_str(safe_card.get("joint_sales_amount")).strip()
    if joint_premium or joint_amount:
        joint_label = _to_str(safe_card.get("joint_sales_label")).strip() or "联合销售"
        _draw_text(draw, (left, y), joint_label, font=font14)
        _draw_right(draw, right, y, _num_text(joint_premium, money=True), font=font16b, fill="#ff0000")
        y += 25
        if joint_amount:
            _draw_text(draw, (left + 12, y), "途家安顺保额", font=font14, fill="#7f899c")
            _draw_right(draw, right, y, _num_text(joint_amount), font=font14, fill="#7f899c")
            y += 25

    _draw_text(draw, (left, y), "驾意险", font=font14)
    _draw_right(draw, right, y, _num_text(safe_card.get("driver_accident_premium"), money=True), font=font16b, fill="#ff0000")
    y += 25

    draw.line((left, y + 2, right, y + 2), fill=line, width=1)
    _draw_text(draw, (left, y + 12), "承保条件改善", font=font14, fill="#005eff")
    y += 42

    _draw_text(draw, (left, y), "理赔信息", font=font14)
    _draw_badge(draw, left + 118, y, "商", "#4f92ff", font12)
    _draw_text(draw, (left + 138, y), safe_card.get("claim_business_count", "-"), font=font14)
    _draw_badge(draw, left + 184, y, "交", "#caa86c", font12)
    _draw_text(draw, (left + 204, y), safe_card.get("claim_compulsory_count", "-"), font=font14)
    _draw_text(draw, (left + 270, y), "理赔查询", font=font14, fill="#005eff")
    y += 32

    draw.line((left, y, right, y), fill=line, width=1)
    _draw_text(draw, (left, y + 10), "人保风险水平", font=font14)
    _draw_right(draw, right, y + 8, f"{_to_str(safe_card.get('risk_score') or '-')} 分", font=font16b, fill="#ff0000")

    return _png_bytes(image, background="#ffffff")


def _proposal_info_rows(card: Mapping[str, Any]) -> List[Tuple[str, str, str, str]]:
    info = card.get("proposal_info")
    if isinstance(info, Mapping):
        return [
            ("被保险人姓名", _to_str(info.get("insured_name") or "-"), "车牌号码", _to_str(info.get("plate_no") or "-")),
            ("发动机号", _to_str(info.get("engine_no") or "-"), "车架号", _to_str(info.get("vin") or "-")),
            ("车辆类型", _to_str(info.get("vehicle_type") or "-"), "车辆性质", _to_str(info.get("vehicle_usage") or "-")),
            ("车辆型号", _to_str(info.get("vehicle_model") or "-"), "匹配方式", _to_str(info.get("model_match_method") or "-")),
            ("初登日期", _to_str(info.get("enroll_date") or "-"), "核定载质量", _to_str(info.get("ton_count") or "-")),
            ("核定载客量(包括司机)", _to_str(info.get("seat_count") or "-"), "新车购置价", _to_str(info.get("purchase_price") or "-")),
            ("承保年数、出险次数", _to_str(info.get("claim_summary") or "-"), "商业险起保日期", _to_str(info.get("bi_start_date") or "-")),
            ("交强险起保日期", _to_str(info.get("ci_start_date") or "-"), "", ""),
        ]
    return [
        ("被保险人姓名", _to_str(card.get("owner_name") or "-"), "车牌号码", _to_str(card.get("plate_no") or "-")),
        ("发动机号", _to_str(card.get("engine_no") or "-"), "车架号", _to_str(card.get("vin") or "-")),
        ("车辆类型", _to_str(card.get("vehicle_type") or "-"), "车辆性质", _to_str(card.get("vehicle_usage") or "-")),
            ("车辆型号", _to_str(card.get("vehicle_model") or "-"), "匹配方式", _to_str(card.get("model_match_method") or "-")),
            ("初登日期", _to_str(card.get("enroll_date") or "-"), "核定载质量", _to_str(card.get("ton_count") or "-")),
            ("核定载客量(包括司机)", _to_str(card.get("seat_count") or "-"), "新车购置价", _to_str(card.get("purchase_price") or "-")),
            ("承保年数、出险次数", _to_str(card.get("claim_summary") or "-"), "商业险起保日期", _to_str(card.get("bi_start_date") or "-")),
            ("交强险起保日期", _to_str(card.get("ci_start_date") or "-"), "", ""),
        ]


def _proposal_coverage_rows(card: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows = card.get("proposal_coverage_items")
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    return [dict(row) for row in _coverage_items(card) if isinstance(row, Mapping)]


def _render_picc_proposal_table_png(card: Mapping[str, Any]) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    safe_card = _safe_card_payload(card)
    info_rows = _proposal_info_rows(safe_card)
    coverage_rows = _proposal_coverage_rows(safe_card)

    scale = _render_scale()
    base_width = int(safe_card.get("image_width") or 1323)
    width = _s(base_width, scale)
    line = "#8a8a8a"
    title_h = _s(48, scale)
    info_row_h = _s(28, scale)
    spacer_h = _s(20, scale)
    header_h = _s(46, scale)
    summary_h = _s(28, scale)
    content_font = _font(_s(20, scale), serif=True)
    small_font = _font(_s(18, scale), serif=True)
    bold_font = _font(_s(20, scale), bold=True, serif=True)
    header_font = _font(_s(28, scale), bold=True, serif=True)
    title_font = _font(_s(30, scale), bold=True, serif=True)

    # Table columns match the captured page: four columns above, three columns below.
    info_x = [0, _s(217, scale), _s(451, scale), _s(667, scale), width - 1]
    quote_x = [0, _s(476, scale), _s(1138, scale), width - 1]

    row_heights: List[int] = []
    measure = ImageDraw.Draw(Image.new("RGB", (width, 100), "#ffffff"))
    for row in coverage_rows:
        name_lines = _wrap_text(
            measure,
            row.get("name"),
            font=content_font,
            max_width=quote_x[1] - quote_x[0] - _s(14, scale),
            max_lines=3,
        )
        row_heights.append(_s(54 if len(name_lines) >= 2 else 28, scale))

    summary_rows = [
        ("commercial_total", "商业车险合计", "", safe_card.get("commercial_premium"), True),
        ("compulsory", "交强险", "", safe_card.get("compulsory_premium"), True),
        ("vehicle_tax", "代收车船税", "", safe_card.get("vehicle_tax"), True),
    ]
    joint_premium = safe_card.get("joint_sales_premium")
    joint_amount = safe_card.get("joint_sales_amount")
    if _to_str(joint_premium).strip() or _to_str(joint_amount).strip():
        summary_rows.append(
            (
                "joint_sales",
                _to_str(safe_card.get("joint_sales_display_label") or "途顺家安组合保险"),
                _money_yuan(joint_amount),
                joint_premium,
                False,
            )
        )
    summary_rows.extend(
        [
            ("total_without_tax", "保费合计（不含车船税）", "", safe_card.get("total_without_vehicle_tax"), True),
            ("total_with_tax", "保费合计（含车船税）", "", safe_card.get("total_with_vehicle_tax") or safe_card.get("total_premium"), True),
        ]
    )

    height = (
        title_h
        + len(info_rows) * info_row_h
        + spacer_h
        + header_h
        + sum(row_heights)
        + len(summary_rows) * summary_h
        + _s(4, scale)
    )
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)

    line_w = _line_width(scale)
    y = _s(4, scale)
    draw.rectangle((0, y, width - 1, y + title_h - 1), outline=line, width=line_w)
    _draw_cell_text(draw, (0, y, width - 1, y + title_h - 1), safe_card.get("title") or "中国人保投保方案", font=title_font, bold_font=title_font)
    y += title_h

    for label1, value1, label2, value2 in info_rows:
        top = y
        bottom = y + info_row_h
        draw.rectangle((0, top, width - 1, bottom), outline=line, width=line_w)
        for x in info_x[1:-1]:
            draw.line((x, top, x, bottom), fill=line, width=line_w)
        _draw_cell_text(draw, (info_x[0], top, info_x[1], bottom), label1, font=content_font, max_lines=1)
        _draw_cell_text(draw, (info_x[1], top, info_x[2], bottom), value1, font=content_font, max_lines=1)
        _draw_cell_text(draw, (info_x[2], top, info_x[3], bottom), label2, font=content_font, max_lines=1)
        value_font = small_font if _text_width(draw, _to_str(value2), content_font) > (info_x[4] - info_x[3] - _s(14, scale)) else content_font
        _draw_cell_text(draw, (info_x[3], top, info_x[4], bottom), value2, font=value_font, max_lines=2)
        y += info_row_h

    draw.rectangle((0, y, width - 1, y + spacer_h), outline=line, width=line_w)
    y += spacer_h

    top = y
    bottom = y + header_h
    draw.rectangle((0, top, width - 1, bottom), outline=line, width=line_w)
    for x in quote_x[1:-1]:
        draw.line((x, top, x, bottom), fill=line, width=line_w)
    _draw_cell_text(draw, (quote_x[0], top, quote_x[1], bottom), "险别名称", font=header_font, bold_font=header_font)
    _draw_cell_text(draw, (quote_x[1], top, quote_x[2], bottom), "保额（元）", font=header_font, bold_font=header_font)
    _draw_cell_text(draw, (quote_x[2], top, quote_x[3], bottom), "保费（元）", font=header_font, bold_font=header_font)
    y += header_h

    for row, row_h in zip(coverage_rows, row_heights):
        top = y
        bottom = y + row_h
        draw.rectangle((0, top, width - 1, bottom), outline=line, width=line_w)
        for x in quote_x[1:-1]:
            draw.line((x, top, x, bottom), fill=line, width=line_w)
        _draw_cell_text(draw, (quote_x[0], top, quote_x[1], bottom), row.get("name"), font=content_font, max_lines=3)
        _draw_cell_text(draw, (quote_x[1], top, quote_x[2], bottom), row.get("amount_text") or row.get("amount"), font=content_font, max_lines=2)
        _draw_cell_text(draw, (quote_x[2], top, quote_x[3], bottom), _money_yuan(row.get("premium")), font=content_font, align="right", max_lines=1)
        y += row_h

    for kind, label, amount_text, premium, merged in summary_rows:
        top = y
        bottom = y + summary_h
        draw.rectangle((0, top, width - 1, bottom), outline=line, width=line_w)
        if merged:
            draw.line((quote_x[2], top, quote_x[2], bottom), fill=line, width=line_w)
            _draw_cell_text(draw, (quote_x[0], top, quote_x[2], bottom), label, font=content_font, bold_font=bold_font, max_lines=1)
        else:
            for x in quote_x[1:-1]:
                draw.line((x, top, x, bottom), fill=line, width=line_w)
            _draw_cell_text(draw, (quote_x[0], top, quote_x[1], bottom), label, font=content_font, max_lines=1)
            _draw_cell_text(draw, (quote_x[1], top, quote_x[2], bottom), amount_text, font=content_font, max_lines=1)
        _draw_cell_text(draw, (quote_x[2], top, quote_x[3], bottom), _money_yuan(premium), font=content_font, bold_font=bold_font, align="right", max_lines=1)
        y += summary_h

    return _png_bytes(image, background="#ffffff")


def render_quote_result_card_png(card: Mapping[str, Any]) -> bytes:
    safe_card = _safe_card_payload(card)
    if _to_str(safe_card.get("style")).strip() == "picc_proposal_table":
        return _render_picc_proposal_table_png(safe_card)
    return _render_legacy_quote_card_png(safe_card)


def save_quote_result_card_image(card: Mapping[str, Any], *, trace_id: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(card, Mapping) or not card:
        return None
    render_scale = _render_scale()
    target_w, target_h = _quote_result_target_size()
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
        "render_scale": render_scale,
        "width": target_w,
        "height": target_h,
    }
