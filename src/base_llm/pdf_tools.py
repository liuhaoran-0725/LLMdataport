from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


def require_poppler() -> None:
    missing = [name for name in ("pdftoppm", "pdftotext", "pdfinfo") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"Missing Poppler tools on PATH: {', '.join(missing)}")


def extract_text(pdf_path: Path, output_txt: Path) -> str:
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), str(output_txt)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return output_txt.read_text(encoding="utf-8", errors="replace")


def render_pdf(pdf_path: Path, output_dir: Path, prefix: str, dpi: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = output_dir / prefix
    subprocess.run(
        ["pdftoppm", "-png", "-r", str(dpi), str(pdf_path), str(out_prefix)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return sorted(output_dir.glob(f"{prefix}-*.png"), key=_page_sort_key)


def split_logical_pages(
    rendered_pages: list[Path],
    output_dir: Path,
    *,
    max_width: int = 1400,
    blank_threshold: float = 0.006,
) -> list[dict[str, object]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    logical_pages: list[dict[str, object]] = []
    logical_index = 1
    for physical_index, image_path in enumerate(rendered_pages, start=1):
        with Image.open(image_path) as img:
            rgb = img.convert("RGB")
            width, height = rgb.size
            crops: list[tuple[str, Image.Image]]
            if width > height * 1.15:
                mid = width // 2
                crops = [
                    ("left", rgb.crop((0, 0, mid, height))),
                    ("right", rgb.crop((mid, 0, width, height))),
                ]
            else:
                crops = [("full", rgb)]

            for side, crop in crops:
                if is_mostly_blank(crop, threshold=blank_threshold):
                    continue
                prepared = resize_for_llm(crop, max_width=max_width)
                out_path = output_dir / f"logical_page_{logical_index:03d}_{physical_index}_{side}.jpg"
                prepared.save(out_path, "JPEG", quality=88, optimize=True)
                logical_pages.append(
                    {
                        "logical_page": logical_index,
                        "physical_page": physical_index,
                        "side": side,
                        "image_path": str(out_path),
                    }
                )
                logical_index += 1
    return logical_pages


def is_mostly_blank(image: Image.Image, threshold: float) -> bool:
    gray = image.convert("L")
    white = Image.new("L", gray.size, 255)
    diff = ImageChops.difference(gray, white)
    stat = ImageStat.Stat(diff)
    mean_diff = stat.mean[0] / 255.0
    return mean_diff < threshold


def resize_for_llm(image: Image.Image, max_width: int) -> Image.Image:
    width, height = image.size
    if width <= max_width:
        return image
    scale = max_width / float(width)
    new_size = (max_width, int(math.ceil(height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _page_sort_key(path: Path) -> int:
    stem = path.stem
    try:
        return int(stem.rsplit("-", 1)[-1])
    except ValueError:
        return 0
