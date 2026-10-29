from __future__ import annotations

import csv
import cgi
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.base_llm.llm_settings import load_llm_settings
from src.base_llm.job_progress import estimate_remaining, record_progress

FRONTEND_ROOT = PROJECT_ROOT / "frontend-html"
OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "base_llm"
PERSONS_ROOT = OUTPUT_ROOT / "persons"
ARTIFACTS_ROOT = OUTPUT_ROOT / "artifacts"
INPUT_PDF_ROOT = PROJECT_ROOT / "anket" / "split_pdfs"
TEMPLATE_PDF_ROOT = PROJECT_ROOT / "anket" / "templates"
TEMPLATE_OUTPUT_ROOT = OUTPUT_ROOT / "templates"
TEMPLATE_SKILL_ROOT = PROJECT_ROOT / "runtime_skills" / "template_overrides"
DEFAULT_TEMPLATE_ID = "default"
DEFAULT_TEMPLATE_NAME = "Default questionnaire template"
DEFAULT_LLM_SETTINGS = load_llm_settings()
DEFAULT_MODEL = DEFAULT_LLM_SETTINGS.model
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
UNSAFE_UPLOAD_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
JOBS: dict[str, dict[str, Any]] = {}
JOB_QUEUE: list[str] = []
JOB_LOCK = threading.RLock()
JOB_WORKER_THREAD: threading.Thread | None = None
DATA_CATEGORY_META = {
    "questionnaire": {"label": "調査票データ", "survey_title": "LLM questionnaire extraction"},
    "general": {
        "label": "自由読み取りデータ",
        "survey_title": "Template-free PDF extraction",
        "prompt_file": "extract_page_freeform.md",
    },
    "cs30": {
        "label": "CS-30",
        "survey_title": "CS-30 身体機能検査",
        "prompt_file": "extract_page_cs30.md",
    },
    "tug": {
        "label": "TUG",
        "survey_title": "TUG 身体機能検査",
        "prompt_file": "extract_page_tug.md",
    },
    "four_stage": {
        "label": "4STAGE",
        "survey_title": "4STAGE 身体機能検査",
        "prompt_file": "extract_page_4stage.md",
    },
    # Legacy category kept so older physical-function JSON records remain readable.
    "physical_function": {
        "label": "身体機能検査データ",
        "survey_title": "身体機能検査データ",
        "prompt_file": "extract_page_physical_function.md",
    },
}
PHYSICAL_DATA_CATEGORIES = frozenset({"physical_function", "cs30", "tug", "four_stage"})


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def answer_dicts(raw_answers: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_answers, list):
        return []
    return [answer for answer in raw_answers if isinstance(answer, dict)]


def ensure_poppler_on_path() -> None:
    candidates = [
        os.environ.get("POPPLER_BIN"),
        str(
            Path.home()
            / "AppData"
            / "Local"
            / "Microsoft"
            / "WinGet"
            / "Packages"
            / "oschwartz10612.Poppler_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "poppler-25.07.0"
            / "Library"
            / "bin"
        ),
    ]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for candidate in candidates:
        if candidate and Path(candidate).exists() and candidate not in parts:
            parts.insert(0, candidate)
    os.environ["PATH"] = os.pathsep.join(parts)


def validate_person_id(person_id: str) -> str:
    decoded = unquote(person_id)
    if not SAFE_ID_RE.match(decoded):
        raise ValueError("Invalid person id.")
    return decoded


def validate_template_id(template_id: str) -> str:
    decoded = unquote(template_id)
    if not SAFE_ID_RE.match(decoded):
        raise ValueError("Invalid template id.")
    return decoded


def normalize_data_category(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "": "general",
        "none": "general",
        "freeform": "general",
        "clinical": "physical_function",
        "physical": "physical_function",
        "physical-function": "physical_function",
        "physical_function": "physical_function",
        "body_function": "physical_function",
        "functional": "physical_function",
        "身体機能": "physical_function",
        "身体機能検査": "physical_function",
        "cs-30": "cs30",
        "cs30": "cs30",
        "tug": "tug",
        "4stage": "four_stage",
        "4-stage": "four_stage",
        "four_stage": "four_stage",
        "four-stage": "four_stage",
        "4ステージ": "four_stage",
        "4 ステージ": "four_stage",
    }
    category = aliases.get(raw, raw)
    return category if category in DATA_CATEGORY_META else "general"


def data_category_label(category: str) -> str:
    return DATA_CATEGORY_META.get(category, DATA_CATEGORY_META["general"])["label"]


def data_category_survey_title(category: str) -> str:
    return DATA_CATEGORY_META.get(category, DATA_CATEGORY_META["general"])["survey_title"]


def is_physical_data_category(category: str) -> bool:
    return category in PHYSICAL_DATA_CATEGORIES


def normalize_template_id(value: str | None, fallback: str | None = None) -> str:
    raw = (value or fallback or "").strip()
    safe = UNSAFE_UPLOAD_ID_RE.sub("_", raw).strip("._-").lower()
    safe = safe[:80].strip("._-")
    if not safe:
        safe = f"template_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    if safe == DEFAULT_TEMPLATE_ID:
        safe = f"{safe}_{uuid.uuid4().hex[:6]}"
    return validate_template_id(safe)


def template_pdf_path(template_id: str) -> Path:
    template_id = validate_template_id(template_id)
    if template_id == DEFAULT_TEMPLATE_ID:
        return PROJECT_ROOT / "anket" / "scheme.pdf"
    return TEMPLATE_PDF_ROOT / f"{template_id}.pdf"


def template_output_dir(template_id: str) -> Path:
    template_id = validate_template_id(template_id)
    if template_id == DEFAULT_TEMPLATE_ID:
        return OUTPUT_ROOT
    return TEMPLATE_OUTPUT_ROOT / template_id


def template_spec_path(template_id: str) -> Path:
    template_id = validate_template_id(template_id)
    if template_id == DEFAULT_TEMPLATE_ID:
        return OUTPUT_ROOT / "survey_spec.json"
    return template_output_dir(template_id) / "survey_spec.json"


def template_artifacts_dir(template_id: str) -> Path:
    template_id = validate_template_id(template_id)
    if template_id == DEFAULT_TEMPLATE_ID:
        return ARTIFACTS_ROOT / "scheme"
    return template_output_dir(template_id) / "artifacts" / "scheme"


def template_skill_dir(template_id: str) -> Path:
    template_id = validate_template_id(template_id)
    if template_id == DEFAULT_TEMPLATE_ID:
        return PROJECT_ROOT / "runtime_skills" / "base_extractor"
    return TEMPLATE_SKILL_ROOT / template_id


def template_meta_path(template_id: str) -> Path:
    return template_output_dir(template_id) / "template.json"


def load_template_meta(template_id: str) -> dict[str, Any]:
    template_id = validate_template_id(template_id)
    if template_id == DEFAULT_TEMPLATE_ID:
        return {
            "template_id": DEFAULT_TEMPLATE_ID,
            "template_name": DEFAULT_TEMPLATE_NAME,
            "source_pdf": str(template_pdf_path(DEFAULT_TEMPLATE_ID)),
            "is_default": True,
        }
    meta_path = template_meta_path(template_id)
    if meta_path.exists():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["template_id"] = template_id
                return payload
        except Exception:
            pass
    return {
        "template_id": template_id,
        "template_name": template_id,
        "source_pdf": str(template_pdf_path(template_id)),
        "is_default": False,
    }


def summarize_template(template_id: str) -> dict[str, Any]:
    template_id = validate_template_id(template_id)
    meta = load_template_meta(template_id)
    pdf_path = template_pdf_path(template_id)
    spec_path = template_spec_path(template_id)
    summary = {
        "template_id": template_id,
        "template_name": str(meta.get("template_name") or template_id),
        "is_default": template_id == DEFAULT_TEMPLATE_ID,
        "source_pdf": str(pdf_path),
        "source_pdf_exists": pdf_path.exists(),
        "survey_spec": str(spec_path),
        "survey_spec_exists": spec_path.exists(),
        "prompt_override_dir": str(template_skill_dir(template_id)),
        "prompt_override_exists": template_skill_dir(template_id).exists(),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "uploaded_filename": meta.get("uploaded_filename"),
    }
    if spec_path.exists():
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            summary["survey_title"] = spec.get("survey_title")
            questions = spec.get("questions")
            if isinstance(questions, list):
                summary["question_count"] = len(questions)
        except Exception as exc:
            summary["spec_error"] = str(exc)
    return summary


def no_template_summary(data_category: str = "general") -> dict[str, Any]:
    data_category = normalize_data_category(data_category)
    category_meta = DATA_CATEGORY_META.get(data_category, DATA_CATEGORY_META["general"])
    return {
        "template_id": "none",
        "template_name": f"No template / {data_category_label(data_category)}",
        "is_default": False,
        "source_pdf": None,
        "source_pdf_exists": False,
        "survey_spec": None,
        "survey_spec_exists": False,
        "prompt_override_dir": str(PROJECT_ROOT / "runtime_skills" / "base_extractor"),
        "prompt_override_exists": True,
        "survey_title": data_category_survey_title(data_category),
        "data_category": data_category,
        "data_category_label": data_category_label(data_category),
        "prompt_file": category_meta.get("prompt_file"),
        "question_count": None,
    }


def list_templates() -> list[dict[str, Any]]:
    template_ids = {DEFAULT_TEMPLATE_ID}
    if TEMPLATE_PDF_ROOT.exists():
        template_ids.update(path.stem for path in TEMPLATE_PDF_ROOT.glob("*.pdf"))
    if TEMPLATE_OUTPUT_ROOT.exists():
        template_ids.update(path.name for path in TEMPLATE_OUTPUT_ROOT.iterdir() if path.is_dir())
    summaries = [summarize_template(template_id) for template_id in template_ids]
    return sorted((item for item in summaries if item["source_pdf_exists"]),
                  key=lambda item: (not item["is_default"], item["template_name"]))


def default_template_mode(options: dict[str, Any]) -> str:
    if str(options.get("template_id") or "").strip().lower() == "none":
        return "none"
    selected = str(options.get("template_mode") or "").strip().lower()
    if selected:
        if selected not in {"existing", "new", "none"}:
            raise ValueError("テンプレート方式は existing / new / none から選択してください。")
        return selected
    if options.get("template_id") or template_pdf_path(DEFAULT_TEMPLATE_ID).is_file():
        return "existing"
    return "none"


def save_template_meta(template_id: str, *, template_name: str, uploaded_filename: str, pdf_path: Path) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    meta = load_template_meta(template_id)
    created_at = meta.get("created_at") or now
    payload = {
        "template_id": template_id,
        "template_name": template_name.strip() or template_id,
        "source_pdf": str(pdf_path),
        "prompt_override_dir": str(template_skill_dir(template_id)),
        "uploaded_filename": uploaded_filename,
        "created_at": created_at,
        "updated_at": now,
        "is_default": False,
    }
    path = template_meta_path(template_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def option_bool(options: dict[str, Any], key: str, default: bool = False) -> bool:
    value = options.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def option_int(options: dict[str, Any], key: str, default: int) -> int:
    value = options.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_upload_person_id(filename: str, requested_id: str | None = None) -> str:
    raw = (requested_id or "").strip() or Path(filename or "").stem
    safe = UNSAFE_UPLOAD_ID_RE.sub("_", raw).strip("._-")
    safe = safe[:90].strip("._-")
    if not safe:
        safe = f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    return validate_person_id(safe)


def unique_upload_target(person_id: str) -> tuple[str, Path]:
    INPUT_PDF_ROOT.mkdir(parents=True, exist_ok=True)
    candidate = validate_person_id(person_id)
    path = INPUT_PDF_ROOT / f"{candidate}.pdf"
    if not path.exists():
        return candidate, path

    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    for index in range(1, 100):
        next_id = validate_person_id(f"{candidate}_{suffix}_{index}")
        next_path = INPUT_PDF_ROOT / f"{next_id}.pdf"
        if not next_path.exists():
            return next_id, next_path
    next_id = validate_person_id(f"{candidate}_{uuid.uuid4().hex[:10]}")
    return next_id, INPUT_PDF_ROOT / f"{next_id}.pdf"


def update_job(
    job: dict[str, Any],
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: int | None = None,
    message: str | None = None,
) -> None:
    if status is not None:
        job["status"] = status
    if stage is not None:
        job["stage"] = stage
    if progress is not None:
        job["progress"] = max(0, min(100, int(progress)))
    if message:
        events = job.setdefault("events", [])
        events.append(
            {
                "time": time.time(),
                "stage": job.get("stage"),
                "message": message,
            }
        )
        del events[:-80]


def refresh_job_progress(job: dict[str, Any], person_id: str, options: dict[str, Any]) -> None:
    with JOB_LOCK:
        _refresh_job_progress(job, person_id, options)


def _refresh_job_progress(job: dict[str, Any], person_id: str, options: dict[str, Any]) -> None:
    if job.get("status") not in {"queued", "running"}:
        return
    if job.get("status") == "queued":
        job.update({"eta_seconds": None, "eta_min_seconds": None, "eta_max_seconds": None,
                    "eta_state": "queued", "elapsed_seconds": 0, "progress_detail": "処理待機中"})
        return
    now = time.time()
    started_at = float(job.get("started_at") or now)
    job["elapsed_seconds"] = int(max(0.0, now - started_at))
    timing = dict(job.get("runtime_timing") or {})
    phase = timing.get("phase", "preparing")
    total_pages = int(timing.get("total_pages") or 0)
    enable_check = option_bool(options, "enable_check_llm")
    job.update(estimate_remaining(timing, total_pages=total_pages, enable_check=enable_check, now=now))
    if phase not in {"extraction", "checking", "finalizing"} or not total_pages:
        job["progress_detail"] = "PDFを画像に変換中" if phase == "rendering" else "ページ構成を確認中"
        if phase == "rendering":
            update_job(job, stage="pdf_rendering", progress=18)
        return
    extraction_done = min(total_pages, len(timing.get("extraction_pages") or []))
    check_done = min(total_pages, len(timing.get("checking_pages") or [])) if enable_check else 0
    total_units = total_pages * (3 if enable_check else 1)
    done_units = extraction_done + check_done * 2
    progress = min(92, 18 + round(done_units / total_units * 74))
    current_page = timing.get("current_page")
    if phase == "checking":
        stage = "check_llm"
        detail = (f"再確認 {current_page}/{total_pages}ページを処理中（{check_done}ページ完了）"
                  if current_page is not None else f"再確認 {check_done}/{total_pages}ページ完了")
    elif phase == "finalizing":
        stage = "finalizing"
        detail = "結果を統合・保存中"
    else:
        stage = "llm_extracting"
        detail = (f"抽出 {current_page}/{total_pages}ページを処理中（{extraction_done}ページ完了）"
                  if current_page is not None else f"抽出 {extraction_done}/{total_pages}ページ完了")
    message = None
    if stage != job.get("stage"):
        message = {"llm_extracting": "MiniMaxでページ画像を解析しています。",
                   "check_llm": "チェックLLMでページ画像を再確認しています。",
                   "finalizing": "抽出結果を統合・保存しています。"}.get(stage)
    update_job(job, stage=stage, progress=max(int(job.get("progress") or 0), progress), message=message)
    job.update({"total_pages": total_pages, "pages_done": extraction_done, "check_pages_done": check_done,
                "total_units": total_units, "done_units": done_units, "progress_detail": detail})


def on_pipeline_progress(job: dict[str, Any], event: dict[str, Any]) -> None:
    # File mtimes and raw output presence are not timing or partial-page completion signals.
    with JOB_LOCK:
        record_progress(job, event)
        _refresh_job_progress(job, str(job.get("person_id") or ""), dict(job.get("options") or {}))


def monitor_pipeline_job(job_id: str, person_id: str, options: dict[str, Any], stop_event: threading.Event) -> None:
    while not stop_event.wait(2.0):
        job = JOBS.get(job_id)
        if not job:
            return
        refresh_job_progress(job, person_id, options)


def load_person_record(person_id: str) -> dict[str, Any]:
    person_id = validate_person_id(person_id)
    path = PERSONS_ROOT / f"{person_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No LLM result found for {person_id}.")
    return json.loads(path.read_text(encoding="utf-8"))


def answer_by_id(answers: list[dict[str, Any]], question_id: str) -> dict[str, Any] | None:
    for answer in answers:
        if isinstance(answer, dict) and str(answer.get("question_id")) == question_id:
            return answer
    return None


def answers_by_id(answers: list[dict[str, Any]], question_id: str) -> list[dict[str, Any]]:
    return [
        answer
        for answer in answers
        if isinstance(answer, dict) and str(answer.get("question_id")) == question_id
    ]


def answer_value(answers: list[dict[str, Any]], question_id: str) -> Any:
    answer = answer_by_id(answers, question_id)
    return answer.get("value") if answer else None


def record_data_category(record: dict[str, Any]) -> str:
    raw = str(record.get("data_category") or "").strip().lower()
    if raw in DATA_CATEGORY_META:
        return raw
    normalized = normalize_data_category(raw)
    if normalized in PHYSICAL_DATA_CATEGORIES:
        return normalized
    if str(record.get("template_mode") or "").strip().lower() == "none":
        return "general"
    return "questionnaire"


def answer_search_text(answer: dict[str, Any]) -> str:
    parts = [
        answer.get("question_id"),
        answer.get("normalized_key"),
        answer.get("measurement_group"),
        answer.get("test_name"),
        answer.get("test_component"),
        answer.get("label_japanese"),
        answer.get("unit"),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def answer_display_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def answer_participant_ids(answers: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for answer in answers:
        raw = answer.get("participant_id")
        if raw in (None, "") and str(answer.get("normalized_key") or answer.get("question_id") or "") == "participant.id":
            raw = answer.get("value")
        text = str(raw or "").strip()
        if not text or text in {"-", "None", "null", "ID未検出"}:
            continue
        if text not in seen:
            seen.add(text)
            ids.append(text)
    return ids


def first_physical_value(answers: list[dict[str, Any]], preferred_keys: list[str], fallback_terms: list[str]) -> Any:
    by_key = {
        str(answer.get("normalized_key") or answer.get("question_id") or "").lower(): answer
        for answer in answers
        if isinstance(answer, dict)
    }
    for key in preferred_keys:
        answer = by_key.get(key.lower())
        if answer and answer.get("value") not in (None, ""):
            return answer.get("value")
    for answer in answers:
        if answer.get("value") in (None, ""):
            continue
        answer_key = str(answer.get("normalized_key") or answer.get("question_id") or "").lower()
        if answer_key.endswith(".row_record"):
            continue
        text = answer_search_text(answer)
        if all(term.lower() in text for term in fallback_terms):
            return answer.get("value")
    return None


def physical_function_values(answers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cs30": first_physical_value(
            answers,
            [
                "physical_function.cs30.count",
                "physical_function.cs30.count_reps",
                "cs30.count",
            ],
            ["cs30"],
        ),
        "stage4": first_physical_value(
            answers,
            [
                "physical_function.four_stage.stage_reached",
                "four_stage.stage_reached",
            ],
            ["stage"],
        ),
        "tug": first_physical_value(
            answers,
            [
                "physical_function.tug.best_seconds",
                "physical_function.tug.trial_1_seconds",
                "physical_function.tug.average_seconds",
                "tug.trial_1_seconds",
            ],
            ["tug"],
        ),
    }


def physical_function_summary(answers: list[dict[str, Any]]) -> str:
    row_records = [
        answer for answer in answers
        if str(answer.get("normalized_key") or answer.get("question_id") or "") == "physical_function.row_record"
        or str(answer.get("normalized_key") or answer.get("question_id") or "").endswith(".row_record")
    ]
    if row_records:
        participant_count = len(answer_participant_ids(row_records))
        return f"row records={len(row_records)}, onsite IDs={participant_count}"
    values = physical_function_values(answers)
    parts = []
    if values["cs30"] not in (None, ""):
        parts.append(f"CS-30={answer_display_value(values['cs30'])}")
    if values["stage4"] not in (None, ""):
        parts.append(f"4STAGE={answer_display_value(values['stage4'])}")
    if values["tug"] not in (None, ""):
        parts.append(f"TUG={answer_display_value(values['tug'])}")
    return ", ".join(parts)


def artifact_answers(file_key: str) -> list[dict[str, Any]]:
    person_dir = ARTIFACTS_ROOT / file_key
    answers: list[dict[str, Any]] = []
    page_extractions_path = person_dir / "page_extractions.json"
    page_payloads: list[Any] = []
    if page_extractions_path.exists():
        try:
            page_payloads = json.loads(page_extractions_path.read_text(encoding="utf-8"))
        except Exception:
            page_payloads = []
    else:
        for page_path in sorted(person_dir.glob("page_*.json"), key=lambda p: natural_key(p.name)):
            try:
                page_payloads.append(json.loads(page_path.read_text(encoding="utf-8")))
            except Exception:
                continue

    for page in page_payloads:
        if not isinstance(page, dict):
            continue
        logical_page = page.get("logical_page")
        for answer in answer_dicts(page.get("answers")):
            copied = dict(answer)
            if logical_page is not None and "logical_page" not in copied:
                copied["logical_page"] = logical_page
            if "status" not in copied and "answer_status" in copied:
                copied["status"] = copied.get("answer_status")
            answers.append(copied)
    return answers


def combined_answers(record: dict[str, Any], file_key: str) -> list[dict[str, Any]]:
    answers = answer_dicts(record.get("answers"))
    existing = {
        (
            str(answer.get("question_id")),
            str(answer.get("subquestion_id")),
            str(answer.get("label_japanese") or answer.get("label") or ""),
        )
        for answer in answers
    }
    for answer in artifact_answers(file_key):
        key = (
            str(answer.get("question_id")),
            str(answer.get("subquestion_id")),
            str(answer.get("label_japanese") or answer.get("label") or ""),
        )
        if key not in existing:
            answers.append(answer)
            existing.add(key)
    return answers


def measurement_date(answers: list[dict[str, Any]]) -> str | None:
    value = answer_value(answers, "q3_date")
    if isinstance(value, str):
        match = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", value)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        year_match = re.search(r"(20\d{2})", value)
        if year_match:
            return year_match.group(1)
    return None


def normalize_gender(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "女性" in text or text == "女":
        return "女性"
    if "男性" in text or text == "男":
        return "男性"
    return text if text not in {"-", "None", "null"} else None


def extract_gender(answers: list[dict[str, Any]]) -> str:
    answer = answer_by_id(answers, "q_gender")
    gender = normalize_gender(answer.get("value") if answer else None)
    return gender or "未抽出"


def int_from_answer_value(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d{1,4}", str(value))
    if not match:
        return None
    return int(match.group(0))


def extract_birth_date(answers: list[dict[str, Any]]) -> tuple[int | None, int | None, int | None]:
    year: int | None = None
    month: int | None = None
    day: int | None = None
    for answer in answers_by_id(answers, "q_birth_date"):
        subquestion_id = str(answer.get("subquestion_id") or "").lower()
        label = str(answer.get("label_japanese") or answer.get("label") or "")
        value = int_from_answer_value(answer.get("value"))
        if value is None:
            continue
        if "year" in subquestion_id or "西暦" in label or ("年" in label and value > 1900):
            year = value
        elif "month" in subquestion_id or "月" in label:
            month = value
        elif "day" in subquestion_id or "日" in label:
            day = value
    return year, month, day


def calculate_age(answers: list[dict[str, Any]], measured_at: str | None) -> str:
    year, month, day = extract_birth_date(answers)
    if not year:
        return "未抽出"
    if not measured_at:
        return str(max(0, datetime.now().year - year))
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", measured_at)
    if not match:
        return str(max(0, datetime.now().year - year))
    measured_year, measured_month, measured_day = (int(part) for part in match.groups())
    age = measured_year - year
    if month and day and (measured_month, measured_day) < (month, day):
        age -= 1
    return str(max(0, age))


def answer_text_parts(answer: dict[str, Any] | None) -> list[str]:
    if not answer:
        return []
    parts: list[str] = []
    for key in ("value", "raw_visible_answer", "_base_llm_value"):
        value = answer.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    for evidence in answer.get("evidence") or []:
        if evidence not in (None, ""):
            parts.append(str(evidence))
    ocr_result = answer.get("ocr_result")
    if isinstance(ocr_result, dict):
        for key in ("rough_read", "recognized_text"):
            value = ocr_result.get(key)
            if value not in (None, ""):
                parts.append(str(value))
    return parts


def extract_participant_identity(record: dict[str, Any], file_key: str) -> dict[str, str | None]:
    answers = answer_dicts(record.get("answers"))
    if is_physical_data_category(record_data_category(record)):
        participant_ids = answer_participant_ids(answers)
        return {
            "record_key": file_key,
            "participant_id": participant_ids[0] if len(participant_ids) == 1 else None,
            "display_id": file_key,
            "name": f"身体機能検査ファイル（{len(participant_ids)} ID）" if participant_ids else "身体機能検査ファイル",
        }

    admin_answer = answer_by_id(answers, "q4_admin_name")
    source_text = " ".join(answer_text_parts(admin_answer))

    participant_id: str | None = None
    id_match = re.search(r"ID\s*[:：]?\s*([A-Za-z0-9-]{6,})", source_text, flags=re.IGNORECASE)
    if id_match:
        participant_id = id_match.group(1)
    else:
        numeric_match = re.search(r"(?<!\d)(20\d{7,})(?!\d)", source_text)
        if numeric_match:
            participant_id = numeric_match.group(1)

    extracted_name: str | None = None
    identity_sources: list[str] = []
    if admin_answer:
        ocr_result = admin_answer.get("ocr_result")
        if isinstance(ocr_result, dict) and ocr_result.get("rough_read"):
            identity_sources.append(str(ocr_result["rough_read"]))
        if admin_answer.get("_base_llm_value"):
            identity_sources.append(str(admin_answer["_base_llm_value"]))
        for evidence in admin_answer.get("evidence") or []:
            text = str(evidence)
            quoted = re.findall(r"[\"'「『]([^\"'」』]*?ID\s*[:：]?\s*[A-Za-z0-9-]{6,})[\"'」』]", text, flags=re.IGNORECASE)
            identity_sources.extend(quoted)
    if participant_id:
        for identity_text in identity_sources:
            if participant_id not in identity_text:
                continue
            before_id = re.split(r"\bID\b", identity_text, maxsplit=1, flags=re.IGNORECASE)[0]
            before_id = re.sub(r"[/|]+", " ", before_id)
            before_id = re.sub(r"\s+", " ", before_id).strip(" '\"「『:：,，。")
            if before_id and len(before_id) <= 60 and not re.search(r"承諾|Printed|field|text|OCR|bbox", before_id, re.IGNORECASE):
                extracted_name = before_id
                break

    return {
        "record_key": file_key,
        "participant_id": participant_id,
        "display_id": participant_id or "ID未検出",
        "name": extracted_name or participant_id or "ID未検出",
    }


def fiscal_year_from_date(date_value: str | None) -> str:
    if date_value:
        match = re.match(r"(20\d{2})", date_value)
        if match:
            return match.group(1)
    return "LLM"


def source_pdf_for_record(person_id: str, record: dict[str, Any]) -> Path:
    raw_path = record.get("source_pdf")
    if raw_path:
        path = Path(str(raw_path))
        if path.exists():
            return path
    return INPUT_PDF_ROOT / f"{person_id}.pdf"


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def count_ocr_results(person_id: str, record: dict[str, Any] | None = None) -> int:
    if record:
        summary = record.get("ocr_summary")
        if isinstance(summary, dict):
            if isinstance(summary.get("request_count"), int):
                return int(summary["request_count"])
            results = summary.get("results")
            if isinstance(results, list):
                return len(results)
    count = 0
    person_dir = ARTIFACTS_ROOT / person_id
    if not person_dir.exists():
        return count
    for page_path in person_dir.glob("page_*.json"):
        try:
            page = json.loads(page_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        count += len(page.get("ocr_results") or [])
    return count


def logical_page_images(person_id: str, limit: int = 6) -> list[dict[str, Any]]:
    logical_dir = ARTIFACTS_ROOT / person_id / "logical_pages"
    images: list[dict[str, Any]] = []
    if not logical_dir.exists():
        return images
    for image_path in sorted(logical_dir.glob("*.jpg"), key=lambda p: natural_key(p.name))[:limit]:
        rel = image_path.relative_to(PROJECT_ROOT).as_posix()
        images.append(
            {
                "title": image_path.stem,
                "url": f"/api/file?path={quote(rel, safe='')}",
            }
        )
    return images


def summarize_record(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    person_id = str(record.get("person_id") or path.stem)
    answers = combined_answers(record, person_id)
    identity = extract_participant_identity(record, person_id)
    date_value = measurement_date(answers)
    year = fiscal_year_from_date(date_value)
    answered_count = sum(1 for answer in answers if answer.get("status") == "answered" and answer.get("value") is not None)
    review_count = sum(1 for answer in answers if answer.get("needs_review"))
    source_pdf = source_pdf_for_record(person_id, record)
    data_category = record_data_category(record)
    row_participant_ids = answer_participant_ids(answers) if is_physical_data_category(data_category) else []
    return {
        "id": identity["display_id"],
        "record_key": person_id,
        "participant_id": identity["participant_id"],
        "name": identity["name"],
        "gender": extract_gender(answers),
        "age": calculate_age(answers, date_value),
        "measurement_date": date_value,
        "year": year,
        "survey_title": record.get("survey_title") or "LLM questionnaire extraction",
        "data_category": data_category,
        "data_category_label": record.get("data_category_label") or data_category_label(data_category),
        "physical_function_summary": physical_function_summary(answers) if is_physical_data_category(data_category) else "",
        "physical_participant_count": len(row_participant_ids),
        "physical_participant_ids": row_participant_ids[:20],
        "template_id": record.get("template_id") or DEFAULT_TEMPLATE_ID,
        "template_name": record.get("template_name") or DEFAULT_TEMPLATE_NAME,
        "answers_count": len(answers),
        "answered_count": answered_count,
        "review_count": review_count,
        "ocr_count": count_ocr_results(person_id, record),
        "source_pdf_exists": source_pdf.exists(),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def list_person_summaries() -> list[dict[str, Any]]:
    if not PERSONS_ROOT.exists():
        return []
    rows = []
    for path in sorted(PERSONS_ROOT.glob("*.json"), key=lambda p: natural_key(p.stem)):
        try:
            rows.append(summarize_record(path))
        except Exception as exc:
            rows.append({"id": path.stem, "error": str(exc)})
    return rows


def to_frontend_person(record: dict[str, Any]) -> dict[str, Any]:
    person_id = str(record.get("person_id"))
    answers = combined_answers(record, person_id)
    identity = extract_participant_identity(record, person_id)
    date_value = measurement_date(answers)
    year = fiscal_year_from_date(date_value)
    answered_count = sum(1 for answer in answers if answer.get("status") == "answered" and answer.get("value") is not None)
    review_count = sum(1 for answer in answers if answer.get("needs_review"))
    ocr_count = count_ocr_results(person_id, record)
    source_pdf = source_pdf_for_record(person_id, record)
    status = f"{answered_count}/{len(answers)} answered, {review_count} review"
    data_category = record_data_category(record)
    row_participant_ids = answer_participant_ids(answers) if is_physical_data_category(data_category) else []
    clinical_tests: list[dict[str, Any]] = []
    if is_physical_data_category(data_category):
        physical_values = physical_function_values(answers)
        clinical_tests.append(
            {
                "year": year,
                "cs30": answer_display_value(physical_values["cs30"]),
                "stage4": answer_display_value(physical_values["stage4"]),
                "tug": answer_display_value(physical_values["tug"]),
                "record_key": person_id,
            }
        )

    questionnaire = {
        "year": year,
        "file_name": source_pdf.name if source_pdf.exists() else f"{person_id}.pdf",
        "pdf_url": f"/api/pdf/{quote(person_id)}" if source_pdf.exists() else "",
        "csv_url": f"/api/person/{quote(person_id)}/answers.csv",
        "json_url": f"/api/person/{quote(person_id)}/result.json",
        "database_json_url": f"/api/person/{quote(person_id)}/database.json"
            if (OUTPUT_ROOT / "database" / f"{person_id}.json").exists() else "",
        "evidence_json_url": f"/api/person/{quote(person_id)}/evidence.json"
            if (OUTPUT_ROOT / "evidence" / f"{person_id}.json").exists() else "",
        "record_key": person_id,
        "status": status,
        "answers_count": len(answers),
        "answered_count": answered_count,
        "review_count": review_count,
        "ocr_count": ocr_count,
        "measurement_date": date_value or "-",
    }

    return {
        "id": identity["display_id"],
        "record_key": person_id,
        "participant_id": identity["participant_id"],
        "basic_info": {
            "name": identity["name"],
            "gender": extract_gender(answers),
            "age": calculate_age(answers, date_value),
            "group": record.get("survey_title") or "LLM extraction",
        },
        "questionnaires": [questionnaire],
        "imu": [],
        "plantar": [],
        "clinical_tests": clinical_tests,
        "llm_result": {
            "person_id": identity["display_id"],
            "record_key": person_id,
            "participant_id": identity["participant_id"],
            "survey_title": record.get("survey_title"),
            "data_category": data_category,
            "data_category_label": record.get("data_category_label") or data_category_label(data_category),
            "physical_participant_count": len(row_participant_ids),
            "physical_participant_ids": row_participant_ids[:100],
            "template_id": record.get("template_id") or DEFAULT_TEMPLATE_ID,
            "template_name": record.get("template_name") or DEFAULT_TEMPLATE_NAME,
            "template_source_pdf": record.get("template_source_pdf"),
            "source_pdf": str(source_pdf),
            "measurement_date": date_value,
            "answers": answers,
            "review_items": record.get("review_items") or [],
            "processing_notes": record.get("processing_notes") or [],
            "skip_rules": record.get("skip_rules") or [],
            "ocr_summary": record.get("ocr_summary") or {},
            "logical_page_images": logical_page_images(person_id),
            "summary": {
                "answers_count": len(answers),
                "answered_count": answered_count,
                "review_count": review_count,
                "ocr_count": ocr_count,
            },
        },
    }


def answers_csv(record: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "participant_id",
            "record_key",
            "data_category",
            "source_row",
            "question_id",
            "subquestion_id",
            "normalized_key",
            "measurement_group",
            "unit",
            "label_japanese",
            "status",
            "value",
            "confidence",
            "needs_review",
            "value_source",
            "evidence",
        ]
    )
    person_id = str(record.get("person_id") or "")
    identity = extract_participant_identity(record, person_id)
    category = record_data_category(record)
    for answer in answer_dicts(record.get("answers")):
        answer_participant_id = answer.get("participant_id") or (
            answer.get("value") if str(answer.get("normalized_key") or answer.get("question_id") or "") == "participant.id" else None
        )
        writer.writerow(
            [
                answer_participant_id or identity["display_id"],
                person_id,
                answer.get("data_category") or category,
                answer.get("source_row") or answer.get("row_number") or answer.get("row_label"),
                answer.get("question_id"),
                answer.get("subquestion_id"),
                answer.get("normalized_key"),
                answer.get("measurement_group"),
                answer.get("unit"),
                answer.get("label_japanese"),
                answer.get("status"),
                answer.get("value"),
                answer.get("confidence"),
                answer.get("needs_review"),
                answer.get("value_source"),
                " | ".join(str(item) for item in (answer.get("evidence") or [])),
            ]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def run_pipeline_job(job_id: str, person_id: str, options: dict[str, Any]) -> None:
    job = JOBS[job_id]
    job["queue_position"] = 0
    update_job(job, status="running", stage="preparing", progress=5, message="解析ジョブを開始しました。")
    job["started_at"] = time.time()
    stop_monitor = threading.Event()
    monitor_thread: threading.Thread | None = None
    try:
        ensure_poppler_on_path()
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from src.base_llm.pipeline import BaseLLMPipeline
        from src.base_llm.database_export import write_database_exports

        pdf_path = INPUT_PDF_ROOT / f"{person_id}.pdf"
        if not pdf_path.exists():
            raise FileNotFoundError(f"Input PDF not found: {pdf_path}")
        update_job(job, stage="loading", progress=10, message=f"入力PDFを確認しました: {pdf_path.name}")
        template_mode = default_template_mode(options)
        freeform_mode = template_mode == "none" or str(options.get("template_id") or "").strip().lower() == "none"
        data_category = normalize_data_category(options.get("data_category")) if freeform_mode else "questionnaire"
        template_id = "none" if freeform_mode else validate_template_id(str(options.get("template_id") or DEFAULT_TEMPLATE_ID))
        template_summary = no_template_summary(data_category) if freeform_mode else summarize_template(template_id)
        scheme_pdf = None if freeform_mode else template_pdf_path(template_id)
        if scheme_pdf is not None and not scheme_pdf.exists():
            raise FileNotFoundError(f"Template PDF not found: {scheme_pdf}")
        job["template"] = template_summary
        job["data_category"] = data_category
        job["data_category_label"] = data_category_label(data_category)
        enable_check_llm = option_bool(options, "enable_check_llm")
        check_model = str(options.get("check_model")) if enable_check_llm and options.get("check_model") else None
        pipeline = BaseLLMPipeline(
            project_root=PROJECT_ROOT,
            model=str(options.get("model") or DEFAULT_MODEL),
            output_dir=OUTPUT_ROOT,
            dpi=option_int(options, "dpi", 130),
            max_image_width=option_int(options, "max_image_width", 1400),
            redact_first_page_name=option_bool(options, "redact_first_page_name"),
            llm_consolidate=option_bool(options, "llm_consolidate"),
            enable_ocr=option_bool(options, "enable_ocr"),
            ocr_device=str(options.get("ocr_device") or "auto"),
            ocr_padding=option_int(options, "ocr_padding", 8),
            ocr_max_new_tokens=option_int(options, "ocr_max_new_tokens", 256),
            force_pages=option_bool(options, "force_pages"),
            force_ocr=option_bool(options, "force_ocr"),
            enable_check_llm=enable_check_llm,
            check_model=check_model,
            apply_check_corrections=option_bool(options, "apply_check_corrections"),
            force_check=option_bool(options, "force_check"),
            skill_dir=template_skill_dir(template_id) if not freeform_mode else PROJECT_ROOT / "runtime_skills" / "base_extractor",
            progress_callback=lambda event: on_pipeline_progress(job, event),
        )
        if freeform_mode:
            update_job(job, stage="survey_spec", progress=18, message=f"テンプレートなしで{data_category_label(data_category)}を読み取る準備をしています。")
            survey_spec = {
                "_template_mode": "none",
                "_freeform": True,
                "_data_category": data_category,
                "_data_category_label": data_category_label(data_category),
                "data_category": data_category,
                "data_category_label": data_category_label(data_category),
                "survey_title": data_category_survey_title(data_category),
                "questions": [],
                "global_skip_rules": [],
            }
            job["template"] = no_template_summary(data_category)
        else:
            update_job(job, stage="survey_spec", progress=18, message=f"テンプレート定義を読み込んでいます: {template_summary['template_name']}")
            survey_spec = pipeline.build_survey_spec(
                scheme_pdf,
                force=option_bool(options, "force_scheme"),
                spec_path=template_spec_path(template_id),
                artifacts_dir=template_artifacts_dir(template_id),
            )
            job["template"] = summarize_template(template_id)
        update_job(job, stage="llm_extracting", progress=18, message="PDFをページ画像に変換し、MiniMaxで抽出しています。")
        monitor_thread = threading.Thread(
            target=monitor_pipeline_job,
            args=(job_id, person_id, options, stop_monitor),
            daemon=True,
        )
        monitor_thread.start()
        final_record = pipeline.process_person_pdf(pdf_path, survey_spec)
        final_record["redact_first_page_name"] = option_bool(options, "redact_first_page_name")
        final_record["template_id"] = template_id
        final_record["template_name"] = str(job["template"].get("template_name") or template_id)
        final_record["template_mode"] = "none" if freeform_mode else "existing"
        final_record["data_category"] = data_category
        final_record["data_category_label"] = data_category_label(data_category)
        final_record["template_source_pdf"] = str(scheme_pdf) if scheme_pdf is not None else None
        final_record["template_survey_spec"] = None if freeform_mode else str(template_spec_path(template_id))
        refresh_job_progress(job, person_id, options)
        update_job(job, stage="finalizing", progress=92, message="解析結果JSONと閲覧用データを更新しています。")
        final_path = PERSONS_ROOT / f"{final_record.get('person_id', person_id)}.json"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text(json.dumps(final_record, ensure_ascii=False, indent=2), encoding="utf-8")
        completion_message = "解析が完了しました。入庫用JSONと証拠JSONも保存しました。"
        try:
            export_result = write_database_exports(final_path, OUTPUT_ROOT)
            job.update(export_result)
            job["database_export_status"] = "finished"
        except Exception as export_error:
            job["database_export_status"] = "failed"
            job["database_export_error"] = str(export_error)
            completion_message = f"読み取りJSONは保存済みですが、入庫用JSONの作成に失敗しました: {export_error}"
        update_job(job, status="finished", stage="finished", progress=100, message=completion_message)
        job["eta_seconds"] = 0
        job["progress_detail"] = "完了"
        job["finished_at"] = time.time()
        job["elapsed_seconds"] = int(max(0, job["finished_at"] - job["started_at"]))
        job["eta_min_seconds"] = 0
        job["eta_max_seconds"] = 0
        job["eta_state"] = "finished"
        job["person"] = summarize_record(final_path)
        job["result_json"] = str(final_path)
    except Exception as exc:
        update_job(job, status="failed", stage="failed", message=f"解析に失敗しました: {exc}")
        job["finished_at"] = time.time()
        job["eta_seconds"] = None
        job["eta_min_seconds"] = None
        job["eta_max_seconds"] = None
        job["eta_state"] = "failed"
        job["elapsed_seconds"] = int(max(0, job["finished_at"] - job["started_at"]))
        job["error"] = str(exc)
        job["traceback"] = traceback.format_exc()
    finally:
        stop_monitor.set()
        if monitor_thread:
            monitor_thread.join(timeout=1.0)


def refresh_queue_positions_locked() -> None:
    for index, queued_job_id in enumerate(JOB_QUEUE, start=1):
        queued_job = JOBS.get(queued_job_id)
        if queued_job and queued_job.get("status") == "queued":
            queued_job["queue_position"] = index


def pipeline_job_worker() -> None:
    global JOB_WORKER_THREAD
    while True:
        with JOB_LOCK:
            if not JOB_QUEUE:
                JOB_WORKER_THREAD = None
                return
            job_id = JOB_QUEUE.pop(0)
            refresh_queue_positions_locked()

        job = JOBS.get(job_id)
        if not job:
            continue
        run_pipeline_job(job_id, str(job.get("person_id") or ""), dict(job.get("options") or {}))


def start_job_worker_locked() -> None:
    global JOB_WORKER_THREAD
    if JOB_WORKER_THREAD is None or not JOB_WORKER_THREAD.is_alive():
        JOB_WORKER_THREAD = threading.Thread(target=pipeline_job_worker, daemon=True)
        JOB_WORKER_THREAD.start()


def enqueue_pipeline_job(
    person_id: str,
    options: dict[str, Any],
    *,
    source: str,
    uploaded_filename: str | None = None,
    input_pdf: Path | None = None,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    job: dict[str, Any] = {
        "job_id": job_id,
        "person_id": validate_person_id(person_id),
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "created_at": time.time(),
        "source": source,
        "options": options,
        "events": [],
    }
    if uploaded_filename:
        job["uploaded_filename"] = uploaded_filename
    if input_pdf:
        job["input_pdf"] = str(input_pdf)
    template_mode = default_template_mode(options)
    template_id = "none" if template_mode == "none" else str(options.get("template_id") or DEFAULT_TEMPLATE_ID)
    data_category = normalize_data_category(options.get("data_category")) if template_id == "none" else "questionnaire"
    job["data_category"] = data_category
    job["data_category_label"] = data_category_label(data_category)
    try:
        job["template"] = no_template_summary(data_category) if template_id == "none" else summarize_template(template_id)
    except Exception:
        job["template"] = {"template_id": template_id}
    update_job(job, message="解析ジョブをキューに登録しました。")
    with JOB_LOCK:
        JOBS[job_id] = job
        JOB_QUEUE.append(job_id)
        refresh_queue_positions_locked()
        start_job_worker_locked()
    return job


class DemoHandler(SimpleHTTPRequestHandler):
    server_version = "LLMDemoServer/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(FRONTEND_ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        try:
            if route == "/api/status":
                self.send_json(
                    {
                        "ok": True,
                        "project_root": str(PROJECT_ROOT),
                        "frontend_root": str(FRONTEND_ROOT),
                        "persons_count": len(list_person_summaries()),
                        "input_pdf_count": len(list(INPUT_PDF_ROOT.glob("*.pdf"))) if INPUT_PDF_ROOT.exists() else 0,
                        "template_count": len(list_templates()),
                        "active_job_count": sum(job.get("status") in {"queued", "running"} for job in list(JOBS.values())),
                        "progress_version": "2026-10-06",
                        "output_dir": str(OUTPUT_ROOT),
                    }
                )
                return
            if route == "/api/templates":
                self.send_json({"templates": list_templates()})
                return
            if route == "/api/persons":
                self.send_json({"persons": list_person_summaries()})
                return
            if route.startswith("/api/person/"):
                self.handle_person_get(route)
                return
            if route.startswith("/api/pdf/"):
                person_id = validate_person_id(route.rsplit("/", 1)[-1])
                record = load_person_record(person_id)
                self.send_file(source_pdf_for_record(person_id, record), "application/pdf")
                return
            if route == "/api/file":
                query = parse_qs(parsed.query)
                rel = query.get("path", [""])[0]
                self.send_artifact_file(rel)
                return
            if route.startswith("/api/jobs/"):
                job_id = route.rsplit("/", 1)[-1]
                job = JOBS.get(job_id)
                if not job:
                    self.send_json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
                    return
                if job.get("status") == "queued":
                    with JOB_LOCK:
                        refresh_queue_positions_locked()
                else:
                    refresh_job_progress(job, str(job.get("person_id") or ""), dict(job.get("options") or {}))
                self.send_json(job)
                return
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self.send_json({"error": str(exc), "traceback": traceback.format_exc()}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        try:
            if route == "/api/upload":
                filename, pdf_bytes, requested_id, options, template_upload = self.read_pdf_upload()
                template_mode = default_template_mode(options)
                template_id = validate_template_id(str(options.get("template_id") or DEFAULT_TEMPLATE_ID))
                if template_mode == "none":
                    data_category = normalize_data_category(options.get("data_category"))
                    options = {
                        **options,
                        "template_mode": "none",
                        "template_id": "none",
                        "data_category": data_category,
                        "data_category_label": data_category_label(data_category),
                    }
                elif template_mode == "new":
                    if template_upload is None:
                        raise ValueError("新しいテンプレートPDFを選択してください。")
                    template_filename, template_bytes = template_upload
                    template_name = str(options.get("template_name") or Path(template_filename).stem).strip()
                    template_id = normalize_template_id(str(options.get("new_template_id") or template_name), Path(template_filename).stem)
                    template_path = template_pdf_path(template_id)
                    template_path.parent.mkdir(parents=True, exist_ok=True)
                    template_path.write_bytes(template_bytes)
                    save_template_meta(
                        template_id,
                        template_name=template_name or template_id,
                        uploaded_filename=template_filename,
                        pdf_path=template_path,
                    )
                    options = {
                        **options,
                        "template_mode": "existing",
                        "template_id": template_id,
                        "template_name": template_name or template_id,
                        "force_scheme": True,
                    }
                else:
                    if not template_pdf_path(template_id).exists():
                        raise ValueError(f"選択した空白テンプレートPDFがありません: {template_id}。「テンプレートなし」で直接読み取るか、空白テンプレートPDFを登録してください。")
                    options = {
                        **options,
                        "template_mode": "existing",
                        "template_id": template_id,
                    }
                person_id = normalize_upload_person_id(filename, requested_id)
                person_id, pdf_path = unique_upload_target(person_id)
                pdf_path.write_bytes(pdf_bytes)
                options = {
                    "force_pages": True,
                    **options,
                }
                options["redact_first_page_name"] = option_bool(options, "redact_first_page_name")
                options["enable_ocr"] = False
                options["force_ocr"] = False
                job = enqueue_pipeline_job(
                    person_id,
                    options,
                    source="upload",
                    uploaded_filename=filename,
                    input_pdf=pdf_path,
                )
                self.send_json(job, HTTPStatus.ACCEPTED)
                return
            if route.startswith("/api/person/") and route.endswith("/run"):
                person_id = validate_person_id(route.split("/")[3])
                options = self.read_json_body()
                pdf_path = INPUT_PDF_ROOT / f"{person_id}.pdf"
                if not pdf_path.exists():
                    self.send_json({"error": f"Input PDF not found for {person_id}."}, HTTPStatus.NOT_FOUND)
                    return
                record = {}
                if "template_id" not in options or "redact_first_page_name" not in options:
                    try:
                        record = load_person_record(person_id)
                    except Exception:
                        record = {}
                if "template_id" not in options:
                    options["template_id"] = record.get("template_id") or (DEFAULT_TEMPLATE_ID if default_template_mode({}) == "existing" else "none")
                    options["template_mode"] = record.get("template_mode") or ("none" if options["template_id"] == "none" else "existing")
                    if options["template_mode"] == "none" and "data_category" not in options:
                        options["data_category"] = record.get("data_category") or "general"
                if "redact_first_page_name" not in options:
                    options["redact_first_page_name"] = bool(record.get("redact_first_page_name", False))
                options["enable_ocr"] = False
                options["force_ocr"] = False
                job = enqueue_pipeline_job(person_id, options, source="rerun", input_pdf=pdf_path)
                self.send_json(job, HTTPStatus.ACCEPTED)
                return
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self.send_json({"error": str(exc), "traceback": traceback.format_exc()}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def read_pdf_upload(self) -> tuple[str, bytes, str | None, dict[str, Any], tuple[str, bytes] | None]:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            raise ValueError("PDFファイルが送信されていません。")
        if content_length > MAX_UPLOAD_BYTES:
            raise ValueError("PDFファイルが大きすぎます。250MB以下にしてください。")
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("multipart/form-dataでPDFを送信してください。")

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(content_length),
            },
            keep_blank_values=True,
        )
        filename, pdf_bytes = self.read_pdf_form_field(
            form,
            ("pdf", "file"),
            missing_message="PDFファイルを選択してください。",
        )
        template_upload = self.read_optional_pdf_form_field(form, ("template_pdf", "template", "scheme_pdf"))

        requested_id = form.getfirst("participant_id")
        options_raw = form.getfirst("options", "{}") or "{}"
        if isinstance(options_raw, bytes):
            options_raw = options_raw.decode("utf-8")
        try:
            options = json.loads(str(options_raw))
        except json.JSONDecodeError as exc:
            raise ValueError("解析オプションのJSONが不正です。") from exc
        if not isinstance(options, dict):
            raise ValueError("解析オプションはJSONオブジェクトで指定してください。")
        return filename, pdf_bytes, str(requested_id).strip() if requested_id else None, options, template_upload

    def read_pdf_form_field(
        self,
        form: cgi.FieldStorage,
        names: tuple[str, ...],
        *,
        missing_message: str,
    ) -> tuple[str, bytes]:
        upload_field = None
        for name in names:
            if name in form:
                upload_field = form[name]
                break
        if isinstance(upload_field, list):
            upload_field = upload_field[0] if upload_field else None
        if upload_field is None or not getattr(upload_field, "filename", None):
            raise ValueError(missing_message)

        filename = Path(str(upload_field.filename)).name
        if not filename.lower().endswith(".pdf"):
            raise ValueError("PDFファイルだけアップロードできます。")
        pdf_bytes = upload_field.file.read()
        if not pdf_bytes:
            raise ValueError("PDFファイルが空です。")
        if not pdf_bytes.lstrip().startswith(b"%PDF"):
            raise ValueError("PDFとして読み取れないファイルです。")
        return filename, pdf_bytes

    def read_optional_pdf_form_field(
        self,
        form: cgi.FieldStorage,
        names: tuple[str, ...],
    ) -> tuple[str, bytes] | None:
        for name in names:
            if name in form:
                return self.read_pdf_form_field(
                    form,
                    (name,),
                    missing_message="テンプレートPDFを選択してください。",
                )
        return None

    def handle_person_get(self, route: str) -> None:
        parts = route.strip("/").split("/")
        if len(parts) < 3:
            self.send_json({"error": "Missing person id."}, HTTPStatus.BAD_REQUEST)
            return
        person_id = validate_person_id(parts[2])
        record = load_person_record(person_id)
        if len(parts) == 3:
            self.send_json(to_frontend_person(record))
            return
        if len(parts) == 4 and parts[3] == "answers.csv":
            body = answers_csv(record)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'inline; filename="{person_id}_answers.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if len(parts) == 4 and parts[3] == "result.json":
            self.send_json(record)
            return
        if len(parts) == 4 and parts[3] in {"database.json", "evidence.json"}:
            export_dir = "database" if parts[3] == "database.json" else "evidence"
            export_path = OUTPUT_ROOT / export_dir / f"{person_id}.json"
            self.send_json(json.loads(export_path.read_text(encoding="utf-8")))
            return
        self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        if not path_is_under(path, PROJECT_ROOT):
            raise ValueError("Refusing to serve files outside the project.")
        body = path.read_bytes()
        guessed_type = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_artifact_file(self, rel_path: str) -> None:
        if not rel_path:
            raise ValueError("Missing file path.")
        path = (PROJECT_ROOT / rel_path).resolve()
        if not path_is_under(path, ARTIFACTS_ROOT):
            raise ValueError("Only generated artifact files can be served.")
        self.send_file(path)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Serve the HTML demo frontend with LLM result APIs.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not FRONTEND_ROOT.exists():
        raise SystemExit(f"Frontend folder not found: {FRONTEND_ROOT}")
    ensure_poppler_on_path()
    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    print(f"Serving LLM demo frontend at http://{args.host}:{args.port}/")
    print(f"Project root: {PROJECT_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
