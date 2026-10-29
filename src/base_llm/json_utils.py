from __future__ import annotations

import json
import re
from typing import Any


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            value = salvage_page_extraction(cleaned)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object from LLM response.")
    return value


def salvage_page_extraction(text: str) -> dict[str, Any]:
    answers = salvage_array_objects(text, "answers")
    ocr_requests = salvage_array_objects(text, "ocr_requests")
    if not answers and not ocr_requests:
        raise json.JSONDecodeError("Could not recover JSON object from LLM response.", text, 0)
    return {
        "logical_page": regex_int_field(text, "logical_page"),
        "data_category": regex_string_field(text, "data_category"),
        "page_kind": regex_string_field(text, "page_kind") or "unknown",
        "answers": answers,
        "ocr_requests": ocr_requests,
        "page_notes": ["Recovered partial JSON from malformed LLM output."],
    }


def salvage_array_objects(text: str, field_name: str) -> list[dict[str, Any]]:
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*\[', text)
    if not match:
        return []
    decoder = json.JSONDecoder()
    index = match.end()
    items: list[dict[str, Any]] = []
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n,":
            index += 1
        if index >= len(text) or text[index] == "]":
            break
        if text[index] != "{":
            next_object = text.find("{", index)
            if next_object < 0:
                break
            index = next_object
        try:
            value, consumed = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            search_at = index + 1
            next_brace = -1
            while search_at < len(text):
                next_object = text.find('"question_id"', search_at)
                if next_object < 0:
                    break
                candidate = text.rfind("{", index + 1, next_object)
                if candidate > index:
                    next_brace = candidate
                    break
                search_at = next_object + len('"question_id"')
            if next_brace < 0:
                break
            index = next_brace
            continue
        if isinstance(value, dict):
            items.append(value)
        index += consumed
    return items


def regex_string_field(text: str, field_name: str) -> str | None:
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*"([^"]*)"', text)
    return match.group(1) if match else None


def regex_int_field(text: str, field_name: str) -> int | None:
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*(\d+)', text)
    return int(match.group(1)) if match else None


def dump_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
