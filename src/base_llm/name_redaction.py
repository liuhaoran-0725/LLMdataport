from __future__ import annotations

import math
from pathlib import Path
from typing import Any



NAME_REDACTION_VERSION = 1
_ASSET_DIR = Path(__file__).with_name("assets")
_LABEL_ANCHOR = _ASSET_DIR / "name_id_label_anchor.png"
_ID_ANCHOR = _ASSET_DIR / "id_prefix_anchor.png"

# Relative to the upper-left of the printed “氏名・ID” label anchor.
# The box covers its furigana and name rows and ends before the separate ID row.
_NAME_BOX_OFFSETS = (58, 55, 206, 109)
_ID_ANCHOR_OFFSETS = (53, 113)
_LABEL_MATCH_SCORE = 0.72
_ID_MATCH_SCORE = 0.66


class NameRedactionError(RuntimeError):
    """Raised when the first-page name cannot be masked safely."""


def _load_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise NameRedactionError(
            "姓名打码需要 OpenCV。请在项目 .venv312 环境中重新安装 requirements.txt；未打码页面不会发送给模型。"
        ) from exc
    return cv2


def _read_gray(cv2, path: Path):
    if not path.is_file():
        raise NameRedactionError(f"缺少姓名打码定位素材：{path.name}")
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        raise NameRedactionError(f"无法读取姓名打码定位素材：{path.name}")
    return image


def _best_template_match(cv2, image_gray, template_gray, roi, scales):
    image_height, image_width = image_gray.shape[:2]
    x0, y0, x1, y1 = (int(value) for value in roi)
    x0 = max(0, min(image_width, x0))
    x1 = max(0, min(image_width, x1))
    y0 = max(0, min(image_height, y0))
    y1 = max(0, min(image_height, y1))
    search = image_gray[y0:y1, x0:x1]
    if search.size == 0:
        return None

    search_height, search_width = search.shape[:2]
    best = None
    for scale in scales:
        if scale <= 0:
            continue
        template_width = max(8, int(round(template_gray.shape[1] * scale)))
        template_height = max(8, int(round(template_gray.shape[0] * scale)))
        if template_width > search_width or template_height > search_height:
            continue
        interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        resized = cv2.resize(
            template_gray,
            (template_width, template_height),
            interpolation=interpolation,
        )
        response = cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(response)
        if not math.isfinite(score):
            continue
        if best is None or score > best["score"]:
            best = {
                "x": x0 + int(location[0]),
                "y": y0 + int(location[1]),
                "width": template_width,
                "height": template_height,
                "scale": float(scale),
                "score": float(score),
            }
    return best


def redact_first_page_name(image_path: Path) -> dict[str, Any]:
    """Mask the first-page name and furigana, while checking that the ID stays clear.

    Matching uses printed, non-personal form labels. If either label cannot be
    aligned or the calculated mask would approach the ID text, this raises and
    the caller must stop before sending the page to a model.
    """
    cv2 = _load_cv2()
    label_template = _read_gray(cv2, _LABEL_ANCHOR)
    id_template = _read_gray(cv2, _ID_ANCHOR)
    image_gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image_gray is None or image_gray.size == 0:
        raise NameRedactionError("无法读取第一页图像，姓名未打码；该页未发送给模型。")

    height, width = image_gray.shape[:2]
    label_match = _best_template_match(
        cv2,
        image_gray,
        label_template,
        (
            int(width * 0.025),
            int(height * 0.58),
            int(width * 0.56),
            int(height * 0.81),
        ),
        [round(0.60 + 0.05 * step, 2) for step in range(19)],
    )
    if label_match is None or label_match["score"] < _LABEL_MATCH_SCORE:
        raise NameRedactionError(
            "无法可靠定位第一页的“氏名・ID”栏，未打码页面不会发送给模型。请确认扫描页方向和版式。"
        )

    expected_id_x = label_match["x"] + round(_ID_ANCHOR_OFFSETS[0] * label_match["scale"])
    expected_id_y = label_match["y"] + round(_ID_ANCHOR_OFFSETS[1] * label_match["scale"])
    id_scale = label_match["scale"]
    id_radius_x = max(8, round(14 * id_scale))
    id_radius_y = max(8, round(14 * id_scale))
    id_match = _best_template_match(
        cv2,
        image_gray,
        id_template,
        (
            expected_id_x - id_radius_x,
            expected_id_y - id_radius_y,
            expected_id_x + round(id_template.shape[1] * id_scale) + id_radius_x,
            expected_id_y + round(id_template.shape[0] * id_scale) + id_radius_y,
        ),
        [round(id_scale * factor, 3) for factor in (0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15)],
    )
    if id_match is None or id_match["score"] < _ID_MATCH_SCORE:
        raise NameRedactionError(
            "无法确认姓名栏下方的 ID 位置，未打码页面不会发送给模型。"
        )

    scale = label_match["scale"]
    offset_x, offset_y, end_x, end_y = _NAME_BOX_OFFSETS
    left = label_match["x"] + round(offset_x * scale)
    top = label_match["y"] + round(offset_y * scale)
    right = label_match["x"] + round(end_x * scale)
    bottom = label_match["y"] + round(end_y * scale)

    _, id_ink = cv2.threshold(id_template, 185, 255, cv2.THRESH_BINARY_INV)
    id_ink_points = cv2.findNonZero(id_ink)
    if id_ink_points is None:
        raise NameRedactionError("无法确认 ID 标记，未打码页面不会发送给模型。")
    id_ink_top_offset = int(id_ink_points[:, 0, 1].min())
    id_ink_top = id_match["y"] + round(id_ink_top_offset * id_match["scale"])
    safety_gap = max(2, round(2 * scale))

    if (
        left < 0
        or top < 0
        or right > width
        or bottom > height
        or right <= left
        or bottom <= top
        or bottom > id_ink_top - safety_gap
    ):
        raise NameRedactionError(
            "姓名打码范围无法与 ID 安全分开，未打码页面不会发送给模型。"
        )

    redacted = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if redacted is None or redacted.size == 0:
        raise NameRedactionError("无法读取第一页图像，姓名未打码；该页未发送给模型。")
    cv2.rectangle(
        redacted,
        (left, top),
        (right - 1, bottom - 1),
        color=(0, 0, 0),
        thickness=cv2.FILLED,
    )
    temporary_path = image_path.with_name(f".{image_path.stem}.redacted{image_path.suffix}")
    try:
        if not cv2.imwrite(
            str(temporary_path),
            redacted,
            [cv2.IMWRITE_JPEG_QUALITY, 95],
        ):
            raise NameRedactionError("无法保存打码后的第一页；该页未发送给模型。")
        temporary_path.replace(image_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return {
        "status": "applied",
        "version": NAME_REDACTION_VERSION,
        "method": "printed_label_alignment",
        "fields": ["name_reading", "name"],
        "id_preserved": True,
        "mask_bbox": [left, top, right, bottom],
        "label_match_score": round(label_match["score"], 4),
        "id_match_score": round(id_match["score"], 4),
    }
