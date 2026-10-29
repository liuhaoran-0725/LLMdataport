from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image


class PaddleOCRVLRegionReader:
    def __init__(
        self,
        *,
        model_path: Path,
        device: str = "auto",
        max_new_tokens: int = 256,
    ) -> None:
        self.model_path = model_path
        self.requested_device = device
        self.max_new_tokens = max_new_tokens
        self._loaded = False
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self.device = "cpu"
        self.backend = "unloaded"

    def read_text(
        self,
        image_path: Path,
        *,
        content_type: str | None = None,
        rough_read: str | None = None,
    ) -> str:
        self._load()
        torch = self._torch
        image = Image.open(image_path).convert("RGB")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": build_ocr_prompt(content_type, rough_read)},
                ],
            }
        ]
        image_processor = getattr(self._processor, "image_processor", None)
        min_pixels = getattr(image_processor, "min_pixels", 112896)
        max_pixels = getattr(image_processor, "max_pixels", 1280 * 28 * 28)
        template_kwargs = {
            "add_generation_prompt": True,
            "tokenize": True,
            "return_dict": True,
            "return_tensors": "pt",
            "images_kwargs": {
                "size": {
                    "shortest_edge": min_pixels,
                    "longest_edge": max_pixels,
                }
            },
        }
        try:
            inputs = self._processor.apply_chat_template(messages, **template_kwargs)
        except TypeError:
            template_kwargs.pop("images_kwargs", None)
            inputs = self._processor.apply_chat_template(messages, **template_kwargs)
        inputs = inputs.to(self._model.device)

        with torch.inference_mode():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
            )
        input_length = inputs["input_ids"].shape[-1]
        generated = outputs[0][input_length:-1]
        if len(generated) == 0:
            generated = outputs[0][input_length:]
        return self._processor.decode(generated, skip_special_tokens=True).strip()

    def _load(self) -> None:
        if self._loaded:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor
        from transformers.utils import logging as transformers_logging

        transformers_logging.set_verbosity_error()
        transformers_logging.disable_progress_bar()

        if self.requested_device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = self.requested_device

        dtype = torch.bfloat16 if self.device.startswith("cuda") else torch.float32
        last_error: Exception | None = None

        try:
            from transformers import AutoModelForImageTextToText
        except ImportError as exc:
            last_error = exc
        else:
            try:
                self._model = AutoModelForImageTextToText.from_pretrained(
                    str(self.model_path),
                    dtype=dtype,
                ).to(self.device)
            except Exception as exc:
                last_error = exc
            else:
                self.backend = "transformers-image-text-to-text"
                self._processor = AutoProcessor.from_pretrained(str(self.model_path))

        if self._model is None:
            patch_default_rope_init(torch)
            patch_create_causal_mask()
            try:
                self._model = AutoModelForCausalLM.from_pretrained(
                    str(self.model_path),
                    dtype=dtype,
                    trust_remote_code=True,
                ).to(self.device)
            except Exception as exc:
                last_error = exc
            else:
                patch_create_causal_mask()
                self.backend = "transformers-remote-causal-lm"
                self._processor = AutoProcessor.from_pretrained(
                    str(self.model_path),
                    trust_remote_code=True,
                )
        if self._model is None or self._processor is None:
            raise RuntimeError(f"Failed to load OCR model from {self.model_path}") from last_error
        self._model.eval()
        self._torch = torch
        self._loaded = True


def build_ocr_prompt(content_type: str | None, rough_read: str | None) -> str:
    kind = str(content_type or "").lower()
    rough = str(rough_read or "").strip()
    rough_line = f"\nRough visual read from the page agent: {rough}" if rough else ""

    if "number" in kind:
        return (
            "Read only the handwritten number in this crop. Return only digits and necessary "
            "range symbols such as - or ~. Do not output printed labels. If unreadable, "
            "return unclear."
            f"{rough_line}"
        )
    if "date" in kind:
        return (
            "Read only the handwritten date in this crop. Return only the date characters, "
            "digits, and separators. Do not output printed labels or nearby instructions. "
            "If unreadable, return unclear."
            f"{rough_line}"
        )
    if "text" in kind or "other" in kind:
        return (
            "Transcribe only the handwritten answer in this crop exactly as written. Preserve "
            "Japanese script; do not translate, romanize, or change kanji into katakana. Do not "
            "describe circle/check/X marks. If the crop contains only a selection mark or is "
            "unreadable, return unclear."
            f"{rough_line}"
        )
    return (
        "OCR this crop. Transcribe only visible handwritten answer text. Do not describe marks "
        "or printed labels. If unreadable, return unclear."
        f"{rough_line}"
    )


def apply_page_ocr(
    *,
    page_result: dict[str, Any],
    image_path: Path,
    output_dir: Path,
    reader: PaddleOCRVLRegionReader,
    padding: int = 8,
    force: bool = False,
) -> list[dict[str, Any]]:
    requests = normalize_ocr_requests(page_result)
    if not requests:
        page_result.setdefault("ocr_results", [])
        return []
    existing = page_result.get("ocr_results")
    if existing and not force:
        return existing

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, request in enumerate(requests, start=1):
        result = {
            "request_id": request["request_id"],
            "question_id": request.get("question_id"),
            "subquestion_id": request.get("subquestion_id"),
            "content_type": request.get("content_type"),
            "rough_read": request.get("rough_read"),
            "status": "pending",
        }
        try:
            crop_path, resolved_bbox, image_size = crop_request_region(
                image_path=image_path,
                request=request,
                output_dir=output_dir,
                index=index,
                padding=padding,
            )
            result.update(
                {
                    "bbox": resolved_bbox,
                    "image_size": image_size,
                    "crop_path": str(crop_path),
                }
            )
        except Exception as exc:
            result.update({"status": "crop_error", "error": str(exc)})
            results.append(result)
            continue

        try:
            recognized_text = reader.read_text(
                crop_path,
                content_type=result.get("content_type"),
                rough_read=result.get("rough_read"),
            )
        except Exception as exc:
            result.update({"status": "ocr_error", "error": str(exc)})
        else:
            result.update(
                {
                    "status": "ok",
                    "source": "ocr",
                    "ocr_engine": "paddleocr_vl",
                    "ocr_backend": reader.backend,
                    "recognized_text": recognized_text,
                }
            )
        results.append(result)

    page_result["ocr_results"] = results
    merge_ocr_results_into_answers(page_result, results)
    return results


def normalize_ocr_requests(page_result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_requests = page_result.get("ocr_requests") or []
    if not isinstance(raw_requests, list):
        return []

    normalized: list[dict[str, Any]] = []
    logical_page = page_result.get("logical_page", "page")
    for index, request in enumerate(raw_requests, start=1):
        if not isinstance(request, dict):
            continue
        copied = dict(request)
        copied["request_id"] = str(
            copied.get("request_id") or f"ocr_{logical_page}_{index}"
        )
        copied["subquestion_id"] = normalize_optional_id(copied.get("subquestion_id"))
        normalized.append(copied)
    return normalized


def crop_request_region(
    *,
    image_path: Path,
    request: dict[str, Any],
    output_dir: Path,
    index: int,
    padding: int,
) -> tuple[Path, dict[str, int], dict[str, int]]:
    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        x1, y1, x2, y2 = resolve_bbox(
            request.get("bbox"),
            bbox_units=str(request.get("bbox_units") or "image_pixels"),
            image_width=width,
            image_height=height,
            padding=padding,
        )
        crop = rgb.crop((x1, y1, x2, y2))
        request_id = safe_filename(str(request.get("request_id") or f"ocr_{index}"))
        crop_path = output_dir / f"{index:03d}_{request_id}.jpg"
        crop.save(crop_path, "JPEG", quality=95, optimize=True)
    return (
        crop_path,
        {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        {"width": width, "height": height},
    )


def resolve_bbox(
    bbox: Any,
    *,
    bbox_units: str,
    image_width: int,
    image_height: int,
    padding: int,
) -> tuple[int, int, int, int]:
    coords = bbox_to_float_tuple(bbox)
    normalized = "normal" in bbox_units.lower() or all(0.0 <= value <= 1.0 for value in coords)
    if normalized:
        x1, y1, x2, y2 = (
            coords[0] * image_width,
            coords[1] * image_height,
            coords[2] * image_width,
            coords[3] * image_height,
        )
    else:
        x1, y1, x2, y2 = coords
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    x1 = max(0, round(x1) - padding)
    y1 = max(0, round(y1) - padding)
    x2 = min(image_width, round(x2) + padding)
    y2 = min(image_height, round(y2) + padding)
    if x2 - x1 < 2 or y2 - y1 < 2:
        raise ValueError(f"Invalid OCR bbox after clamping: {(x1, y1, x2, y2)}")
    return x1, y1, x2, y2


def bbox_to_float_tuple(bbox: Any) -> tuple[float, float, float, float]:
    if isinstance(bbox, dict):
        values = [bbox.get(key) for key in ("x1", "y1", "x2", "y2")]
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        values = list(bbox[:4])
    else:
        raise ValueError(f"Expected bbox as dict or four-item list, got {bbox!r}")
    try:
        return tuple(float(value) for value in values)  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"OCR bbox contains non-numeric values: {bbox!r}") from exc


def merge_ocr_results_into_answers(
    page_result: dict[str, Any],
    ocr_results: list[dict[str, Any]],
) -> None:
    answers = page_result.setdefault("answers", [])
    if not isinstance(answers, list):
        return

    for result in ocr_results:
        text = str(result.get("recognized_text") or "").strip()
        if result.get("status") != "ok" or not text:
            continue
        answer = find_answer(
            answers,
            question_id=result.get("question_id"),
            subquestion_id=result.get("subquestion_id"),
        )
        accepted, reason = should_apply_ocr_result(result, text)
        result["applied"] = accepted
        result["apply_reason"] = reason
        ocr_result = build_answer_ocr_result(result, text, applied=accepted)
        if not accepted:
            if answer is not None:
                answer["ocr_result"] = ocr_result
                answer["ocr_text"] = text
                answer["ocr_request_id"] = result.get("request_id")
                answer["ocr_crop_path"] = result.get("crop_path")
                answer.setdefault("value_source", "base_llm")
                answer["needs_review"] = True
                append_evidence(
                    answer,
                    f"OCR read handwritten bbox {result.get('bbox')} as {text!r}, but it was not applied: {reason}.",
                )
            continue
        if answer is None:
            answer = {
                "question_id": result.get("question_id"),
                "subquestion_id": result.get("subquestion_id"),
                "answer_status": "answered",
                "value": text,
                "raw_visible_answer": text,
                "confidence": 0.75,
                "evidence": "",
            }
            answers.append(answer)
        previous_value = answer.get("value")
        if previous_value not in (None, "", text):
            answer.setdefault("_base_llm_value", previous_value)
        answer["answer_status"] = "answered"
        answer["value"] = text
        answer["raw_visible_answer"] = text
        answer["value_source"] = "ocr"
        answer["ocr_text"] = text
        answer["ocr_request_id"] = result.get("request_id")
        answer["ocr_crop_path"] = result.get("crop_path")
        answer["ocr_result"] = ocr_result
        answer["confidence"] = max(to_float(answer.get("confidence"), 0.0), 0.75)
        append_evidence(
            answer,
            f"OCR read handwritten bbox {result.get('bbox')} as {text!r}.",
        )


def should_apply_ocr_result(result: dict[str, Any], text: str) -> tuple[bool, str]:
    content_type = str(result.get("content_type") or "").lower()
    rough_read = str(result.get("rough_read") or "").strip()
    recognized_digits = normalize_digits(text)
    rough_digits = normalize_digits(rough_read)

    if is_unclear_text(text):
        return False, "OCR returned an unclear/unreadable marker"
    if "other" in content_type and (is_mark_only_text(rough_read) or is_mark_only_text(text)):
        return False, "OCR appears to be reading a selection mark rather than answer text"
    if looks_like_common_hallucination(text):
        return False, "OCR text looks like a generic hallucination"
    if "text" in content_type and contains_japanese(rough_read) and is_ascii_sentence(text):
        return False, "OCR text is ASCII/English but the rough read is Japanese"

    if "number" in content_type or "date" in content_type:
        if not recognized_digits:
            return False, "OCR text has no digits for a numeric/date field"
        if rough_digits and recognized_digits != rough_digits:
            return False, f"OCR digits {recognized_digits!r} differ from Base LLM rough_read digits {rough_digits!r}"
    return True, "accepted"


def build_answer_ocr_result(
    result: dict[str, Any],
    text: str,
    *,
    applied: bool,
) -> dict[str, Any]:
    return {
        "source": "ocr",
        "engine": result.get("ocr_engine"),
        "backend": result.get("ocr_backend"),
        "request_id": result.get("request_id"),
        "recognized_text": text,
        "applied": applied,
        "apply_reason": result.get("apply_reason"),
        "bbox": result.get("bbox"),
        "crop_path": result.get("crop_path"),
        "content_type": result.get("content_type"),
        "rough_read": result.get("rough_read"),
    }


def normalize_digits(value: str) -> str:
    translated = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return "".join(character for character in translated if character.isdigit())


def is_unclear_text(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"", "unclear", "unknown", "unreadable", "illegible", "不明", "判読不能"}


def is_mark_only_text(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value)
    if not normalized:
        return False
    mark_chars = set("0oOqQ〇○◯。．.●◎口□☑✓✔xX×/＼\\-ー_")
    return len(normalized) <= 3 and all(character in mark_chars for character in normalized)


def looks_like_common_hallucination(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    return any(
        phrase in normalized
        for phrase in (
            "the quick brown fox",
            "lorem ipsum",
            "hello world",
        )
    )


def contains_japanese(value: str) -> bool:
    return any(
        "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u9fff"
        for character in value
    )


def is_ascii_sentence(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    has_ascii_letter = any(("A" <= character <= "Z") or ("a" <= character <= "z") for character in stripped)
    return has_ascii_letter and not contains_japanese(stripped)


def find_answer(
    answers: list[Any],
    *,
    question_id: Any,
    subquestion_id: Any,
) -> dict[str, Any] | None:
    target_question_id = str(question_id)
    target_subquestion_id = normalize_optional_id(subquestion_id)
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        if str(answer.get("question_id")) != target_question_id:
            continue
        if normalize_optional_id(answer.get("subquestion_id")) != target_subquestion_id:
            continue
        return answer
    return None


def append_evidence(answer: dict[str, Any], evidence: str) -> None:
    current = answer.get("evidence")
    if isinstance(current, str) and current.strip():
        answer["evidence"] = f"{current.rstrip()} {evidence}"
    else:
        answer["evidence"] = evidence


def normalize_optional_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "ocr"


def to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def patch_default_rope_init(torch: Any) -> None:
    try:
        from transformers.modeling_rope_utils import ROPE_INIT_FUNCTIONS
    except ImportError:
        return
    if "default" in ROPE_INIT_FUNCTIONS:
        return

    def compute_default_rope_parameters(config: Any, device: Any = None, seq_len: Any = None, **_: Any) -> tuple[Any, float]:
        del seq_len
        base = getattr(config, "rope_theta", 10000.0)
        partial_rotary_factor = getattr(config, "partial_rotary_factor", 1.0)
        head_dim = getattr(config, "head_dim", None)
        if head_dim is None:
            head_dim = getattr(config, "hidden_size") // getattr(config, "num_attention_heads")
        dim = int(head_dim * partial_rotary_factor)
        inv_freq = 1.0 / (
            base
            ** (
                torch.arange(0, dim, 2, dtype=torch.int64, device=device).float()
                / dim
            )
        )
        return inv_freq, 1.0

    ROPE_INIT_FUNCTIONS["default"] = compute_default_rope_parameters
    if not hasattr(torch.nn.Module, "compute_default_rope_parameters"):
        def module_default_rope_parameters(_: Any, config: Any) -> tuple[Any, float]:
            return compute_default_rope_parameters(config)

        torch.nn.Module.compute_default_rope_parameters = module_default_rope_parameters


def patch_create_causal_mask() -> None:
    try:
        import transformers.masking_utils as masking_utils
    except ImportError:
        return

    original = getattr(masking_utils, "create_causal_mask", None)
    if original is None:
        return
    if getattr(original, "_base_llm_compat", False):
        patched = original
    else:
        def patched(*args: Any, **kwargs: Any) -> Any:
            if "inputs_embeds" in kwargs and "input_embeds" not in kwargs:
                kwargs["input_embeds"] = kwargs.pop("inputs_embeds")
            return original(*args, **kwargs)

        patched._base_llm_compat = True
        masking_utils.create_causal_mask = patched

    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").endswith("modeling_paddleocr_vl"):
            if hasattr(module, "create_causal_mask"):
                module.create_causal_mask = patched
