"""Deterministic database export; never asks an LLM to reinterpret answers."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

SCHEMA_VERSION = "1.0"
EXPORT_REVISION = "2026-10-05"
STATUSES = {"answered", "not_answered", "unclear", "skipped", "conflict"}
DOMAIN_FIELDS = (
    "participant_id", "participant_name", "normalized_key", "measurement_group",
    "test_name", "test_component", "unit", "category", "source_row", "row_number",
    "source_table", "row_label", "column_label", "field_role",
)
CONTEXT_LABELS = {
    "タイトル", "説明文", "ページ番号", "フッター", "アンケート協力依頼1",
    "アンケート回答の目的説明", "測定結果説明", "ご協力ありがとうございました。",
}
NO_MARK = re.compile(
    r"no\s+(?:visible\s+)?(?:circle|check|mark|selection)|not\s+(?:circled|selected|marked)"
    r"|unselected|(?:○|〇|丸|チェック|マーク|印)(?:印)?(?:は|が|の)?(?:なし|無し|ない|見えない)"
    r"|○印および記入なし",
    re.I,
)
SINGLE_CHOICE = re.compile(r"(?:1|１|一)つ|single.?choice|one\s+(?:option|answer)", re.I)
PRINTED_CONTEXT = re.compile(
    r"printed|page\s+title|header\s+text|section\s+(?:header|heading)|footer|heading"
    r"|label.+visible|印刷|タイトル|見出し|フッター|ページ番号|ページ.{0,20}表示", re.I,
)
RESPONSE_MARK = re.compile(r"handwrit|手書|circled|selected|checkmark|チェックあり|○印あり", re.I)


def _observations(answer: dict[str, Any]) -> list[str]:
    evidence = answer.get("evidence")
    items = evidence if isinstance(evidence, list) else ([] if evidence is None else [evidence])
    return list(dict.fromkeys(str(item) for item in items if str(item).strip()
                             and not re.fullmatch(r"logical_page=\d+", str(item).strip())))


def _page_number(answer: dict[str, Any]) -> Any:
    if answer.get("logical_page") is not None:
        return answer["logical_page"]
    evidence = answer.get("evidence")
    items = evidence if isinstance(evidence, list) else [evidence]
    for item in items:
        match = re.fullmatch(r"logical_page=(\d+)", str(item).strip())
        if match:
            return int(match.group(1))
    return None


def _has_value(answer: dict[str, Any]) -> bool:
    return answer.get("value") not in (None, "", [], {})


def _blank_option(answer: dict[str, Any]) -> bool:
    return (answer.get("status", answer.get("answer_status")) == "not_answered"
            and not _has_value(answer) and bool(NO_MARK.search(" ".join(_observations(answer)))))


def exclusion_reasons(record: dict[str, Any]) -> dict[int, str]:
    """Conservative legacy cleanup. Uncertain classifications remain in answers."""
    answers = record["answers"]
    excluded = {i: "document_context" for i, a in enumerate(answers)
                if a.get("field_role") == "document_context"}
    if record.get("template_mode") != "none" or record.get("data_category", "general") != "general":
        return excluded

    by_id: dict[tuple[Any, str], list[dict[str, Any]]] = defaultdict(list)
    table_groups: dict[tuple[Any, str, str], list[int]] = defaultdict(list)
    for i, answer in enumerate(answers):
        qid = str(answer["question_id"])
        label = str(answer.get("label_japanese") or "").strip()
        observation = " ".join(_observations(answer))
        value = answer.get("value")
        label_is_pagination = bool(re.fullmatch(r"[^\d]{1,30}\d+/\d+", label)) and value == label
        id_is_context = bool(re.search(r"_(?:title|footer|(?:sub)?section_header)(?:_\d+)?$", qid))
        explicit_page_title = bool(re.search(r"page title|printed title", observation, re.I))
        label_is_context = label in CONTEXT_LABELS or label_is_pagination or (
            label == "かゆみスケール" and value == label)
        if (label_is_context or id_is_context or explicit_page_title) and PRINTED_CONTEXT.search(observation) \
                and not RESPONSE_MARK.search(observation):
            excluded[i] = "document_context"
        by_id[(_page_number(answer), qid)].append(answer)
        match = re.fullmatch(r"(.+_table_\d+)_row_\d+", qid)
        if match and SINGLE_CHOICE.search(label):
            table_groups[(_page_number(answer), match.group(1), label)].append(i)

    # Option records only disappear when the selected parent answer exists.
    for i, answer in enumerate(answers):
        match = re.fullmatch(r"(.+)_opt\d+", str(answer["question_id"]))
        if match and _blank_option(answer):
            parents = by_id.get((_page_number(answer), match.group(1)), [])
            if len(parents) == 1 and parents[0].get("status") == "answered" and _has_value(parents[0]):
                excluded[i] = "unselected_option"
    for indices in table_groups.values():
        selected = [i for i in indices if answers[i].get("status") == "answered" and _has_value(answers[i])]
        if len(selected) == 1:
            for i in indices:
                if _blank_option(answers[i]):
                    excluded[i] = "unselected_option"

    # Older general-form extraction sometimes emitted each education option as a field.
    anchor: dict[str, Any] | None = None
    for i, answer in enumerate(answers):
        observation = " ".join(_observations(answer))
        option_only = observation.startswith("選択肢") and _blank_option(answer)
        if option_only:
            if anchor is not None and _page_number(anchor) == _page_number(answer) \
                    and re.search(r"学歴|single.?choice|(?:1|１|一)つ", str(anchor.get("label_japanese", "")), re.I):
                excluded[i] = "unselected_option"
        else:
            anchor = answer if answer.get("status") == "answered" and _has_value(answer) and i not in excluded else None
    return excluded


def _path_reference(path: Path, output_root: Path) -> str:
    try:
        return path.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def build_database_exports(
    record: dict[str, Any], *, raw_path: Path, output_root: Path,
    logical_pages: list[dict[str, Any]] | None = None, raw_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(record.get("answers"), list):
        raise ValueError("The raw JSON must contain an answers list.")
    for answer in record["answers"]:
        if not isinstance(answer, dict) or not str(answer.get("question_id") or "").strip():
            raise ValueError("Every answer must have a question_id; no fields were silently discarded.")
    record_id = str(record.get("person_id") or raw_path.stem)
    exclusions = exclusion_reasons(record)
    evidence_path = output_root / "evidence" / raw_path.name
    provenance = {
        "raw_json": _path_reference(raw_path, output_root),
        "raw_json_sha256": raw_sha256,
        "source_pdf": record.get("source_pdf"),
        "processing_metadata": deepcopy(record.get("processing_metadata") or {}),
    }
    main: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "record_id": record_id,
        "data_category": record.get("data_category", "questionnaire"),
        "template_id": record.get("template_id"),
        "evidence_file": _path_reference(evidence_path, output_root), "answers": [],
    }
    sidecar: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "export_revision": EXPORT_REVISION,
        "record_id": record_id, "provenance": provenance,
        "pages": [], "entries": [], "excluded_fields": [],
        "export_summary": {},
    }
    pages = {str(p.get("logical_page")): deepcopy(p) for p in record.get("freeform_pages", []) if isinstance(p, dict)}
    for page in logical_pages or []:
        if not isinstance(page, dict):
            continue
        page_key = str(page.get("logical_page"))
        pages.setdefault(page_key, {}).update(deepcopy(page))
    for page in pages.values():
        if page.get("image_path"):
            page["image_path"] = _path_reference(Path(page["image_path"]), output_root)
        page.pop("answer_count", None)
        # Explanatory page_notes remain available verbatim in the raw JSON.
        page.pop("page_notes", None)
        sidecar["pages"].append(page)
    review_by_key: dict[tuple[str, Any], list[dict[str, Any]]] = defaultdict(list)
    for item in record.get("review_items", []):
        if isinstance(item, dict):
            review_by_key[(str(item.get("question_id")), item.get("subquestion_id"))].append(item)

    for i, answer in enumerate(record["answers"]):
        evidence_id = f"ev_{i + 1:04d}"
        qid = str(answer["question_id"])
        status = answer.get("status", answer.get("answer_status", "unclear"))
        review_codes = []
        if status not in STATUSES:
            review_codes.append("unknown_status")
        if status in {"unclear", "conflict"}:
            review_codes.append(status)
        if status == "answered" and not _has_value(answer):
            review_codes.append("answered_without_value")
        value = answer.get("value")
        if isinstance(value, str) and value.strip() in {"○", "〇", "✓", "✔", "☑"}:
            review_codes.append("selection_symbol_value")
        needs_review = bool(answer.get("needs_review") or review_codes)
        entry: dict[str, Any] = {
            "evidence_id": evidence_id, "source_answer_index": i,
            "question_id": qid, "logical_page": _page_number(answer),
            "raw_visible_answer": deepcopy(answer.get("raw_visible_answer")),
            "observations": _observations(answer),
            "model_confidence": answer.get("confidence"),
            "value_source": answer.get("value_source"),
        }
        if answer.get("subquestion_id") is not None:
            entry["subquestion_id"] = answer["subquestion_id"]
        if review_codes:
            entry["review_codes"] = review_codes
        if answer.get("rule_trace"):
            entry["rule_trace"] = deepcopy(answer["rule_trace"])
        for key in ("source_bbox", "bbox_units", "ocr_result", "check_result", "check_review", "validation",
                    "check_llm_flags", "check_llm_corrections"):
            if answer.get(key) not in (None, "", [], {}):
                entry[key] = deepcopy(answer[key])
        reviews = review_by_key.get((qid, answer.get("subquestion_id")), [])
        detailed_reviews = [deepcopy(r) for r in reviews if r.get("reason") not in {
            "Marked for review.", "No visible answer was extracted.", "Low confidence or unclear answer.",
        }]
        if detailed_reviews:
            entry["review_details"] = detailed_reviews
        sidecar["entries"].append(entry)
        if i in exclusions:
            sidecar["excluded_fields"].append({
                "question_id": qid, "label_japanese": answer.get("label_japanese"),
                "status": status, "value": deepcopy(value), "evidence_id": evidence_id,
                "reason_code": exclusions[i],
            })
            continue
        item = {
            "question_id": qid, "label_japanese": answer.get("label_japanese") or qid,
            "status": status, "value": deepcopy(value),
            "needs_review": needs_review, "evidence_id": evidence_id,
        }
        if answer.get("subquestion_id") is not None:
            item["subquestion_id"] = answer["subquestion_id"]
        for key in DOMAIN_FIELDS:
            if answer.get(key) not in (None, ""):
                item[key] = deepcopy(answer[key])
        if answer.get("data_category") and answer["data_category"] != main["data_category"]:
            item["data_category"] = answer["data_category"]
        main["answers"].append(item)
    sidecar["export_summary"] = {
        "source_answer_count": len(record["answers"]),
        "database_answer_count": len(main["answers"]),
        "excluded_counts": dict(Counter(exclusions.values())),
        "status_counts": dict(Counter(str(a["status"]) for a in main["answers"])),
        "needs_review_count": sum(a["needs_review"] for a in main["answers"]),
    }
    return main, sidecar


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", encoding="utf-8", delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and Path(temporary).exists():
            Path(temporary).unlink()


def write_database_exports(raw_path: Path, output_root: Path | None = None) -> dict[str, Any]:
    raw_path = Path(raw_path).resolve()
    output_root = Path(output_root).resolve() if output_root else raw_path.parent.parent
    database_path = output_root / "database" / raw_path.name
    evidence_path = output_root / "evidence" / raw_path.name
    if raw_path in {database_path, evidence_path}:
        raise ValueError("Export destinations must not overwrite the raw JSON.")
    raw_bytes = raw_path.read_bytes()
    record = json.loads(raw_bytes)
    # Locate page images alongside the input, even when exporting to a new directory.
    mapping_path = raw_path.parent.parent / "artifacts" / raw_path.stem / "logical_pages.json"
    logical_pages = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.exists() else []
    main, sidecar = build_database_exports(
        record, raw_path=raw_path, output_root=output_root, logical_pages=logical_pages,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
    # Write evidence first so a newly published database file always has a sidecar.
    _atomic_json(evidence_path, sidecar)
    _atomic_json(database_path, main)
    return {"database_json": str(database_path), "evidence_json": str(evidence_path),
            **sidecar["export_summary"]}
