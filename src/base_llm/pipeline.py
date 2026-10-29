from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .llm_client_factory import create_llm_client
from .llm_settings import load_llm_settings
from .json_utils import dump_json, load_json
from .ocr_tools import PaddleOCRVLRegionReader, apply_page_ocr, should_apply_ocr_result
from .pdf_tools import extract_text, render_pdf, require_poppler, split_logical_pages

EXTRACTION_CONTRACT_REVISION = "2026-10-05"


class BaseLLMPipeline:
    def __init__(
        self,
        *,
        project_root: Path,
        model: str | None = None,
        output_dir: Path,
        dpi: int = 130,
        max_image_width: int = 1400,
        llm_consolidate: bool = False,
        enable_ocr: bool = False,
        ocr_model_path: Path | None = None,
        ocr_device: str = "auto",
        ocr_padding: int = 8,
        ocr_max_new_tokens: int = 256,
        force_pages: bool = False,
        force_ocr: bool = False,
        enable_check_llm: bool = False,
        check_model: str | None = None,
        apply_check_corrections: bool = False,
        force_check: bool = False,
        skill_dir: Path | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.project_root = project_root
        self.llm_settings = load_llm_settings()
        self.provider = self.llm_settings.provider
        self.model = model or self.llm_settings.model
        default_check_model = "gemma4:31b" if self.provider == "ollama" else self.model
        self.check_model = check_model or (default_check_model if enable_check_llm else self.model)
        self.output_dir = output_dir
        self.dpi = dpi
        self.max_image_width = max_image_width
        self.llm_consolidate = llm_consolidate
        self.enable_ocr = enable_ocr
        self.enable_check_llm = enable_check_llm
        self.ocr_model_path = ocr_model_path or project_root / "models" / "PaddleOCR-VL-1.5"
        self.ocr_device = ocr_device
        self.ocr_padding = ocr_padding
        self.ocr_max_new_tokens = ocr_max_new_tokens
        self.force_pages = force_pages
        self.force_ocr = force_ocr
        self.apply_check_corrections = apply_check_corrections
        self.force_check = force_check
        self.client = create_llm_client(model=self.model, settings=self.llm_settings)
        self.check_client = (
            self.client
            if self.check_model == self.model
            else create_llm_client(model=self.check_model, settings=self.llm_settings)
        )
        self.default_skill_dir = project_root / "runtime_skills" / "base_extractor"
        self.skill_dir = skill_dir or self.default_skill_dir
        self._ocr_reader: PaddleOCRVLRegionReader | None = None
        self.progress_callback = progress_callback

    def report_progress(self, phase: str, event: str, **details: Any) -> None:
        payload = {"phase": phase, "event": event, **details}
        callback = getattr(self, "progress_callback", None)
        if callback is not None:
            callback(payload)
        timing_path = getattr(self, "_timing_path", None)
        if timing_path is not None:
            self._timing_events.append({"recorded_at": time.time(), **payload})
            dump_json(str(timing_path), {
                "provider": self.provider,
                "model": self.model,
                "events": self._timing_events,
            })

    def read_skill_prompt(self, filename: str) -> str:
        custom_path = self.skill_dir / filename
        if custom_path.exists():
            return custom_path.read_text(encoding="utf-8")
        return (self.default_skill_dir / filename).read_text(encoding="utf-8")

    def extraction_contract(self) -> str:
        contract = self.read_skill_prompt("extraction_output_contract.md")
        ocr_instruction = (
            "Local OCR enabled: true. OCR requests may be made for handwritten regions when needed."
            if self.enable_ocr else
            "Local OCR enabled: false. Read handwriting directly; ocr_requests MUST be []. "
            "If a field cannot be read, use null/unclear and flag it; no later OCR pass will fill it."
        )
        return f"{contract}\n{ocr_instruction}"

    def annotate_extraction(self, result: dict[str, Any], prompt: str) -> None:
        if not self.enable_ocr:
            result["ocr_requests"] = []
        result["_extraction_provenance"] = {
            "provider": self.provider,
            "model": self.model,
            "prompt_contract_revision": EXTRACTION_CONTRACT_REVISION,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "ocr_enabled": self.enable_ocr,
        }

    def build_survey_spec(
        self,
        scheme_pdf: Path,
        *,
        force: bool = False,
        spec_path: Path | None = None,
        artifacts_dir: Path | None = None,
    ) -> dict[str, Any]:
        require_poppler()
        artifacts_dir = artifacts_dir or self.output_dir / "artifacts" / "scheme"
        spec_path = spec_path or self.output_dir / "survey_spec.json"
        if spec_path.exists() and not force:
            return load_json(str(spec_path))

        scheme_text = extract_text(scheme_pdf, artifacts_dir / "scheme.txt")
        prompt_template = self.read_skill_prompt("build_survey_page_spec.md")
        page_specs: list[dict[str, Any]] = []
        for page_number, page_text in enumerate(split_pdf_text_pages(scheme_text), start=1):
            if not page_text.strip():
                continue
            print(f"Building survey spec page {page_number}...", flush=True)
            page_spec_path = artifacts_dir / f"scheme_page_{page_number:03d}_spec.json"
            if page_spec_path.exists() and not force:
                page_spec = load_json(str(page_spec_path))
            else:
                prompt = (
                    f"{prompt_template}\n\n"
                    f"Source page number: {page_number}\n\n"
                    "Blank questionnaire page text begins below.\n"
                    "----- PAGE TEXT BEGIN -----\n"
                    f"{page_text}\n"
                    "----- PAGE TEXT END -----\n"
                )
                page_spec = self.client.chat_json(
                    prompt,
                    num_ctx=8192,
                    num_predict=6144,
                    raw_output_path=artifacts_dir / f"scheme_page_{page_number:03d}_raw.txt",
                )
                dump_json(str(page_spec_path), page_spec)
            page_specs.append(page_spec)
        survey_spec = merge_page_specs(page_specs)
        dump_json(str(spec_path), survey_spec)
        return survey_spec

    def process_person_pdf(self, pdf_path: Path, survey_spec: dict[str, Any]) -> dict[str, Any]:
        require_poppler()
        freeform_mode = is_freeform_survey_spec(survey_spec)
        freeform_category = freeform_data_category(survey_spec)
        person_id = pdf_path.stem
        person_dir = self.output_dir / "artifacts" / person_id
        person_dir.mkdir(parents=True, exist_ok=True)
        self._timing_path = person_dir / "timings.json"
        self._timing_events: list[dict[str, Any]] = []
        self.report_progress("rendering", "phase_started")
        rendered_dir = person_dir / "rendered"
        logical_dir = person_dir / "logical_pages"
        rendered_pages = render_pdf(pdf_path, rendered_dir, person_id, self.dpi)
        logical_pages = split_logical_pages(
            rendered_pages,
            logical_dir,
            max_width=self.max_image_width,
        )
        dump_json(str(person_dir / "logical_pages.json"), logical_pages)
        self.report_progress("extraction", "phase_started", total_pages=len(logical_pages))

        page_results: list[dict[str, Any]] = []
        reused_pages: set[int] = set()
        for page in logical_pages:
            logical_page = int(page["logical_page"])
            page_json_path = person_dir / f"page_{logical_page:03d}.json"
            cached = page_json_path.exists() and not self.force_pages
            succeeded = True
            self.report_progress("extraction", "page_started", logical_page=logical_page)
            page_started = time.monotonic()
            if cached:
                reused_pages.add(logical_page)
                print(f"Reusing {person_id} logical page {logical_page}...", flush=True)
                result = load_json(str(page_json_path))
            else:
                print(f"Extracting {person_id} logical page {logical_page}...", flush=True)
                relevant_questions = questions_for_page(survey_spec, logical_page)
                try:
                    if freeform_mode:
                        result = self.extract_page_freeform(
                            image_path=Path(str(page["image_path"])),
                            logical_page=logical_page,
                            data_category=freeform_category,
                            raw_output_path=person_dir / f"page_{logical_page:03d}_raw.txt",
                        )
                    else:
                        result = self.extract_page(
                            image_path=Path(str(page["image_path"])),
                            logical_page=logical_page,
                            relevant_questions=relevant_questions,
                            raw_output_path=person_dir / f"page_{logical_page:03d}_raw.txt",
                        )
                except Exception as exc:
                    succeeded = False
                    result = {
                        "logical_page": logical_page,
                        "page_kind": "unknown",
                        "answers": [],
                        "ocr_requests": [],
                        "page_notes": [f"Page extraction failed: {exc}"],
                    }
            result["_physical_page"] = page["physical_page"]
            result["_side"] = page["side"]
            result["_image_path"] = page["image_path"]
            if not freeform_mode:
                remap_page_answer_ids(survey_spec, result)
            self.apply_ocr_if_requested(
                person_dir=person_dir,
                page_result=result,
                image_path=Path(str(page["image_path"])),
            )
            validate_applied_ocr_answers(result)
            if not freeform_mode:
                validate_choice_answers(survey_spec, result)
            page_results.append(result)
            dump_json(str(page_json_path), result)
            self.report_progress("extraction", "page_finished", logical_page=logical_page,
                                 duration_seconds=time.monotonic() - page_started,
                                 cached=cached, success=succeeded)

        raw_path = person_dir / "page_extractions.json"
        dump_json(str(raw_path), page_results)
        print(f"Consolidating {person_id}...", flush=True)
        self.report_progress("finalizing", "phase_started")
        if freeform_mode:
            final_record = deterministic_freeform_consolidate(
                person_id=person_id,
                pdf_path=pdf_path,
                page_results=page_results,
                note="Template-free extraction was used.",
                data_category=freeform_category,
                category_label=freeform_category_label(survey_spec),
            )
        elif self.llm_consolidate:
            try:
                final_record = self.consolidate_person(
                    person_id=person_id,
                    pdf_path=pdf_path,
                    survey_spec=survey_spec,
                    page_results=page_results,
                    raw_output_path=person_dir / "consolidate_raw.txt",
                )
            except Exception as exc:
                error_path = person_dir / "consolidate_error.txt"
                error_path.write_text(str(exc), encoding="utf-8")
                final_record = deterministic_consolidate(
                    person_id=person_id,
                    pdf_path=pdf_path,
                    survey_spec=survey_spec,
                    page_results=page_results,
                    error=str(exc),
                )
        else:
            final_record = deterministic_consolidate(
                person_id=person_id,
                pdf_path=pdf_path,
                survey_spec=survey_spec,
                page_results=page_results,
                error="LLM consolidation disabled; deterministic consolidation was used.",
            )
        if self.enable_check_llm:
            try:
                self.report_progress("checking", "phase_started")
                check_report = self.check_person_record(
                    person_id=person_id,
                    pdf_path=pdf_path,
                    person_dir=person_dir,
                    survey_spec=survey_spec,
                    page_results=page_results,
                    final_record=final_record,
                )
                attach_check_report(
                    final_record,
                    check_report,
                    apply_corrections=self.apply_check_corrections,
                )
            except Exception as exc:
                error_path = person_dir / "check_error.txt"
                error_path.write_text(str(exc), encoding="utf-8")
                final_record.setdefault("processing_notes", []).append(f"Check LLM failed: {exc}")
        self.report_progress("finalizing", "phase_started")
        final_record["processing_metadata"] = {
            "pipeline_revision": EXTRACTION_CONTRACT_REVISION,
            "ocr_enabled": self.enable_ocr,
            "timing_file": str(self._timing_path),
            "page_extractions": [
                {
                    "logical_page": page.get("logical_page"),
                    "reused": page.get("logical_page") in reused_pages,
                    "provenance_available": bool(page.get("_extraction_provenance")),
                    **(page.get("_extraction_provenance") or {}),
                }
                for page in page_results
            ],
        }
        final_path = self.output_dir / "persons" / f"{person_id}.json"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(str(final_path), final_record)
        return final_record

    def extract_page(
        self,
        *,
        image_path: Path,
        logical_page: int,
        relevant_questions: list[dict[str, Any]],
        raw_output_path: Path | None = None,
    ) -> dict[str, Any]:
        prompt_template = self.read_skill_prompt("extract_page_answers.md")
        with Image.open(image_path) as image:
            image_width, image_height = image.size
        prompt = (
            f"{prompt_template}\n\n"
            f"{self.extraction_contract()}\n\n"
            f"Logical page number: {logical_page}\n\n"
            f"Rendered image size: width={image_width}px, height={image_height}px\n\n"
            "Relevant survey questions for this page:\n"
            f"{json.dumps(relevant_questions, ensure_ascii=False, indent=2)}\n"
        )
        result = self.client.chat_json(
            prompt,
            images=[image_path],
            num_ctx=8192,
            num_predict=6144,
            raw_output_path=raw_output_path,
        )
        result.setdefault("logical_page", logical_page)
        result.setdefault("ocr_requests", [])
        self.annotate_extraction(result, prompt)
        return result

    def extract_page_freeform(
        self,
        *,
        image_path: Path,
        logical_page: int,
        data_category: str = "general",
        raw_output_path: Path | None = None,
    ) -> dict[str, Any]:
        prompt_template = self.read_skill_prompt(freeform_prompt_name(data_category))
        with Image.open(image_path) as image:
            image_width, image_height = image.size
        prompt = (
            f"{prompt_template}\n\n"
            f"{self.extraction_contract()}\n\n"
            f"Logical page number: {logical_page}\n\n"
            f"Rendered image size: width={image_width}px, height={image_height}px\n\n"
            f"Data category: {data_category}\n"
        )
        result = self.client.chat_json(
            prompt,
            images=[image_path],
            num_ctx=32768 if data_category in {"physical_function", "cs30", "tug", "four_stage"} else 8192,
            num_predict=32768 if data_category in {"physical_function", "cs30", "tug", "four_stage"} else 6144,
            raw_output_path=raw_output_path,
        )
        result.setdefault("logical_page", logical_page)
        result.setdefault("data_category", data_category)
        result.setdefault("answers", [])
        result.setdefault("ocr_requests", [])
        self.annotate_extraction(result, prompt)
        return result

    def apply_ocr_if_requested(
        self,
        *,
        person_dir: Path,
        page_result: dict[str, Any],
        image_path: Path,
    ) -> None:
        if not self.enable_ocr:
            return
        ocr_requests = page_result.get("ocr_requests") or []
        if not ocr_requests:
            page_result.setdefault("ocr_results", [])
            return
        logical_page = int(page_result.get("logical_page") or 0)
        print(
            f"Running OCR for logical page {logical_page} on {len(ocr_requests)} bbox request(s)...",
            flush=True,
        )
        apply_page_ocr(
            page_result=page_result,
            image_path=image_path,
            output_dir=person_dir / "ocr_crops" / f"page_{logical_page:03d}",
            reader=self.ocr_reader(),
            padding=self.ocr_padding,
            force=self.force_ocr or self.force_pages,
        )

    def ocr_reader(self) -> PaddleOCRVLRegionReader:
        if self._ocr_reader is None:
            self._ocr_reader = PaddleOCRVLRegionReader(
                model_path=self.ocr_model_path,
                device=self.ocr_device,
                max_new_tokens=self.ocr_max_new_tokens,
            )
        return self._ocr_reader

    def consolidate_person(
        self,
        *,
        person_id: str,
        pdf_path: Path,
        survey_spec: dict[str, Any],
        page_results: list[dict[str, Any]],
        raw_output_path: Path | None = None,
    ) -> dict[str, Any]:
        prompt_template = self.read_skill_prompt("consolidate_person_answers.md")
        payload = {
            "person_id": person_id,
            "source_pdf": str(pdf_path),
            "survey_spec": survey_spec,
            "page_extractions": page_results,
        }
        prompt = (
            f"{prompt_template}\n\n"
            "Input payload:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        )
        return self.client.chat_json(
            prompt,
            num_ctx=32768,
            num_predict=8192,
            raw_output_path=raw_output_path,
        )

    def check_person_record(
        self,
        *,
        person_id: str,
        pdf_path: Path,
        person_dir: Path,
        survey_spec: dict[str, Any],
        page_results: list[dict[str, Any]],
        final_record: dict[str, Any],
    ) -> dict[str, Any]:
        check_path = person_dir / "check_report.json"
        if check_path.exists() and not self.force_check:
            for page_result in page_results:
                self.report_progress("checking", "page_finished",
                                     logical_page=page_result.get("logical_page"),
                                     duration_seconds=0, cached=True)
            return load_json(str(check_path))

        page_checks: list[dict[str, Any]] = []
        for page_result in page_results:
            logical_page = int(page_result.get("logical_page") or 0)
            if logical_page <= 0:
                continue
            print(f"Checking {person_id} logical page {logical_page} with {self.check_model}...", flush=True)
            cached = (person_dir / f"check_page_{logical_page:03d}.json").exists() and not self.force_check
            self.report_progress("checking", "page_started", logical_page=logical_page)
            page_started = time.monotonic()
            page_check = self.check_page_record(
                person_id=person_id, person_dir=person_dir, survey_spec=survey_spec, page_result=page_result,
            )
            page_checks.append(page_check)
            self.report_progress("checking", "page_finished", logical_page=logical_page,
                                 duration_seconds=time.monotonic() - page_started, cached=cached,
                                 success=page_check.get("status") != "check_failed")

        check_report = build_person_check_report(
            person_id=person_id,
            pdf_path=pdf_path,
            check_model=self.check_model,
            page_checks=page_checks,
        )
        dump_json(str(check_path), check_report)
        return check_report

    def check_page_record(
        self,
        *,
        person_id: str,
        person_dir: Path,
        survey_spec: dict[str, Any],
        page_result: dict[str, Any],
    ) -> dict[str, Any]:
        logical_page = int(page_result.get("logical_page") or 0)
        page_check_path = person_dir / f"check_page_{logical_page:03d}.json"
        if page_check_path.exists() and not self.force_check:
            return load_json(str(page_check_path))

        image_path = Path(str(page_result.get("_image_path") or ""))
        if not image_path.exists():
            page_check = {
                "logical_page": logical_page,
                "check_model": self.check_model,
                "image_path": str(image_path),
                "status": "check_failed",
                "review_items": [
                    {
                        "logical_page": logical_page,
                        "question_id": "__page__",
                        "subquestion_id": None,
                        "issue_type": "other",
                        "reason": f"Check image is missing: {image_path}",
                        "question_for_human": "Verify whether this page image was rendered correctly.",
                        "suggested_action": "inspect_image",
                        "severity": "high",
                    }
                ],
                "corrections": [],
                "processing_notes": ["Visual check could not run because the logical page image was missing."],
            }
            dump_json(str(page_check_path), page_check)
            return page_check

        if is_freeform_survey_spec(survey_spec):
            relevant_questions = freeform_questions_for_page_check(page_result)
        else:
            relevant_questions = questions_for_page(survey_spec, logical_page)
        blind_read = self.check_page_blind_read(
            logical_page=logical_page,
            image_path=image_path,
            relevant_questions=relevant_questions,
            raw_output_path=person_dir / f"check_page_{logical_page:03d}_blind_raw.txt",
        )
        comparison = self.compare_page_check(
            person_id=person_id,
            logical_page=logical_page,
            image_path=image_path,
            relevant_questions=relevant_questions,
            page_result=page_result,
            blind_read=blind_read,
            raw_output_path=person_dir / f"check_page_{logical_page:03d}_compare_raw.txt",
        )
        page_check = {
            "logical_page": logical_page,
            "check_model": self.check_model,
            "image_path": str(image_path),
            "status": comparison.get("page_status") or comparison.get("overall_status") or "checked",
            "blind_read": blind_read,
            "comparison": comparison,
            "review_items": comparison.get("review_items") or [],
            "corrections": comparison.get("corrections") or [],
            "processing_notes": comparison.get("processing_notes") or [],
        }
        dump_json(str(page_check_path), page_check)
        return page_check

    def check_page_blind_read(
        self,
        *,
        logical_page: int,
        image_path: Path,
        relevant_questions: list[dict[str, Any]],
        raw_output_path: Path,
    ) -> dict[str, Any]:
        prompt_template = self.read_skill_prompt("check_page_blind_read.md")
        with Image.open(image_path) as image:
            image_width, image_height = image.size
        prompt = (
            f"{prompt_template}\n\n"
            f"Logical page number: {logical_page}\n\n"
            f"Rendered image size: width={image_width}px, height={image_height}px\n\n"
            "Relevant survey questions for this page:\n"
            f"{json.dumps(relevant_questions, ensure_ascii=False, indent=2)}\n"
        )
        result = self.check_client.chat_json(
            prompt,
            images=[image_path],
            num_ctx=32768,
            num_predict=8192,
            raw_output_path=raw_output_path,
        )
        result.setdefault("logical_page", logical_page)
        result.setdefault("answers", [])
        result.setdefault("review_items", [])
        return result

    def compare_page_check(
        self,
        *,
        person_id: str,
        logical_page: int,
        image_path: Path,
        relevant_questions: list[dict[str, Any]],
        page_result: dict[str, Any],
        blind_read: dict[str, Any],
        raw_output_path: Path,
    ) -> dict[str, Any]:
        prompt_template = self.read_skill_prompt("check_page_compare.md")
        payload = {
            "person_id": person_id,
            "logical_page": logical_page,
            "relevant_questions": relevant_questions,
            "check_blind_read": blind_read,
            "base_page_extraction": summarize_page_for_check(page_result),
        }
        prompt = (
            f"{prompt_template}\n\n"
            "Input payload:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        )
        result = self.check_client.chat_json(
            prompt,
            images=[image_path],
            num_ctx=32768,
            num_predict=8192,
            raw_output_path=raw_output_path,
        )
        result.setdefault("logical_page", logical_page)
        result.setdefault("review_items", [])
        result.setdefault("corrections", [])
        return result


def summarize_pages_for_check(page_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [summarize_page_for_check(page_result) for page_result in page_results]


def summarize_page_for_check(page_result: dict[str, Any]) -> dict[str, Any]:
    page_keys = [
        "logical_page",
        "page_kind",
        "answers",
        "ocr_requests",
        "ocr_results",
        "page_notes",
        "_physical_page",
        "_side",
        "_image_path",
    ]
    return {
        key: page_result[key]
        for key in page_keys
        if key in page_result
    }


def build_person_check_report(
    *,
    person_id: str,
    pdf_path: Path,
    check_model: str,
    page_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    review_items: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    processing_notes: list[str] = []
    for page_check in page_checks:
        logical_page = page_check.get("logical_page")
        for item in page_check.get("review_items") or []:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            copied.setdefault("logical_page", logical_page)
            copied.setdefault("source", "visual_check_llm")
            review_items.append(copied)
        for correction in page_check.get("corrections") or []:
            if not isinstance(correction, dict):
                continue
            copied = dict(correction)
            copied.setdefault("logical_page", logical_page)
            copied.setdefault("source", "visual_check_llm")
            corrections.append(copied)
        for note in page_check.get("processing_notes") or []:
            processing_notes.append(f"logical_page={logical_page}: {note}")

    if corrections:
        overall_status = "has_corrections"
    elif review_items:
        overall_status = "needs_review"
    else:
        overall_status = "pass"

    return {
        "check_model_role": "visual_check_llm",
        "check_model": check_model,
        "overall_status": overall_status,
        "person_id": person_id,
        "source_pdf": str(pdf_path),
        "summary": (
            f"Visual Check LLM reviewed {len(page_checks)} logical page(s), "
            f"flagged {len(review_items)} review item(s), and suggested {len(corrections)} correction(s)."
        ),
        "page_checks": page_checks,
        "review_items": dedupe_review_items(review_items),
        "corrections": dedupe_corrections(corrections),
        "processing_notes": processing_notes,
    }


def attach_check_report(
    final_record: dict[str, Any],
    check_report: dict[str, Any],
    *,
    apply_corrections: bool,
) -> None:
    final_record["check_llm"] = check_report
    applied_count = 0
    if apply_corrections:
        applied_count = apply_check_corrections_to_answers(final_record, check_report)
    marked_count = mark_check_reviews(
        final_record,
        check_report,
        skip_applied_clear_corrections=apply_corrections,
    )
    final_record.setdefault("processing_notes", []).append(
        f"Check LLM completed; marked {marked_count} answer(s) for review; "
        f"applied {applied_count} correction(s)."
    )


def mark_check_reviews(
    final_record: dict[str, Any],
    check_report: dict[str, Any],
    *,
    skip_applied_clear_corrections: bool,
) -> int:
    answer_index = build_final_answer_index(final_record)
    review_items = final_record.setdefault("review_items", [])
    marked_count = 0

    for item in check_report.get("review_items") or []:
        if not isinstance(item, dict):
            continue
        question_id = normalize_check_question_id(item.get("question_id"))
        if question_id is None:
            continue
        subquestion_id = normalize_page_optional_id(item.get("subquestion_id"))
        logical_page = item.get("logical_page")
        reason = str(item.get("reason") or item.get("question_for_human") or "Check LLM marked this answer.")
        severity = normalize_check_severity(item.get("severity"))
        answer = answer_index.get((question_id, subquestion_id)) or answer_index.get((question_id, None))
        if answer is not None:
            answer["needs_review"] = True
            flag = {
                "reason": reason,
                "severity": severity,
                "issue_type": item.get("issue_type"),
                "suggested_action": item.get("suggested_action"),
                "source": item.get("source") or "visual_check_llm",
            }
            if logical_page is not None:
                flag["logical_page"] = logical_page
            question_for_human = item.get("question_for_human")
            if question_for_human:
                flag["question_for_human"] = question_for_human
            answer.setdefault("check_llm_flags", []).append(flag)
            marked_count += 1
        review_item = {
            "question_id": question_id,
            "reason": f"Check LLM: {reason}",
            "severity": severity,
            "source": item.get("source") or "visual_check_llm",
        }
        if logical_page is not None:
            review_item["logical_page"] = logical_page
        if item.get("issue_type"):
            review_item["issue_type"] = item.get("issue_type")
        if item.get("suggested_action"):
            review_item["suggested_action"] = item.get("suggested_action")
        if item.get("question_for_human"):
            review_item["question_for_human"] = item.get("question_for_human")
        if subquestion_id is not None:
            review_item["subquestion_id"] = subquestion_id
        review_items.append(review_item)

    for correction in check_report.get("corrections") or []:
        if not isinstance(correction, dict):
            continue
        if skip_applied_clear_corrections and correction.get("needs_review_after_correction") is False:
            continue
        question_id = normalize_check_question_id(correction.get("question_id"))
        if question_id is None:
            continue
        subquestion_id = normalize_page_optional_id(correction.get("subquestion_id"))
        logical_page = correction.get("logical_page")
        reason = str(correction.get("reason") or "Check LLM suggested a correction.")
        answer = answer_index.get((question_id, subquestion_id)) or answer_index.get((question_id, None))
        if answer is not None:
            answer["needs_review"] = True
            flag = {
                "reason": reason,
                "severity": "high",
                "issue_type": "correction_suggested",
                "suggested_action": "verify_value",
                "source": correction.get("source") or "visual_check_llm",
                "corrected_status": correction.get("corrected_status"),
                "corrected_value": correction.get("corrected_value"),
            }
            if logical_page is not None:
                flag["logical_page"] = logical_page
            answer.setdefault("check_llm_flags", []).append(flag)
            marked_count += 1
        review_item = {
            "question_id": question_id,
            "reason": f"Check LLM suggested correction: {reason}",
            "severity": "high",
            "source": correction.get("source") or "visual_check_llm",
            "issue_type": "correction_suggested",
            "suggested_action": "verify_value",
            "corrected_status": correction.get("corrected_status"),
            "corrected_value": correction.get("corrected_value"),
        }
        if logical_page is not None:
            review_item["logical_page"] = logical_page
        if subquestion_id is not None:
            review_item["subquestion_id"] = subquestion_id
        review_items.append(review_item)

    final_record["review_items"] = dedupe_review_items(review_items)
    return marked_count


def apply_check_corrections_to_answers(final_record: dict[str, Any], check_report: dict[str, Any]) -> int:
    answer_index = build_final_answer_index(final_record)
    applied_count = 0
    for correction in check_report.get("corrections") or []:
        if not isinstance(correction, dict):
            continue
        question_id = normalize_check_question_id(correction.get("question_id"))
        if question_id is None:
            continue
        subquestion_id = normalize_page_optional_id(correction.get("subquestion_id"))
        answer = answer_index.get((question_id, subquestion_id)) or answer_index.get((question_id, None))
        if answer is None:
            continue

        before = {
            "status": answer.get("status"),
            "value": answer.get("value"),
            "confidence": answer.get("confidence"),
            "value_source": answer.get("value_source"),
            "needs_review": answer.get("needs_review"),
        }
        if "corrected_status" in correction:
            answer["status"] = correction.get("corrected_status")
        elif "status" in correction:
            answer["status"] = correction.get("status")
        if "corrected_value" in correction:
            answer["value"] = correction.get("corrected_value")
            answer["value_source"] = "check_llm"
        elif "value" in correction:
            answer["value"] = correction.get("value")
            answer["value_source"] = "check_llm"
        if "corrected_confidence" in correction:
            answer["confidence"] = correction.get("corrected_confidence")
        elif "confidence" in correction:
            answer["confidence"] = correction.get("confidence")
        answer["needs_review"] = bool(correction.get("needs_review_after_correction", True))

        reason = str(correction.get("reason") or "Check LLM correction was applied.")
        append_final_answer_evidence(answer, f"Check LLM correction: {reason}")
        answer.setdefault("check_llm_corrections", []).append(
            {
                "before": before,
                "after": {
                    "status": answer.get("status"),
                    "value": answer.get("value"),
                    "confidence": answer.get("confidence"),
                    "value_source": answer.get("value_source"),
                    "needs_review": answer.get("needs_review"),
                },
                "reason": reason,
            }
        )
        applied_count += 1
    return applied_count


def build_final_answer_index(final_record: dict[str, Any]) -> dict[tuple[str, str | None], dict[str, Any]]:
    answer_index: dict[tuple[str, str | None], dict[str, Any]] = {}
    for answer in final_record.get("answers", []):
        if not isinstance(answer, dict):
            continue
        question_id = normalize_check_question_id(answer.get("question_id"))
        if question_id is None:
            continue
        subquestion_id = normalize_page_optional_id(answer.get("subquestion_id"))
        answer_index[(question_id, subquestion_id)] = answer
    return answer_index


def normalize_check_question_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_check_severity(value: Any) -> str:
    severity = str(value or "medium").strip().lower()
    if severity not in {"low", "medium", "high"}:
        return "medium"
    return severity


def dedupe_review_items(review_items: list[Any]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str, str, str]] = set()
    for item in review_items:
        if not isinstance(item, dict):
            continue
        question_id = normalize_check_question_id(item.get("question_id"))
        if question_id is None:
            continue
        subquestion_id = normalize_page_optional_id(item.get("subquestion_id"))
        logical_page = str(item.get("logical_page") or "")
        reason = str(item.get("reason") or "")
        source = str(item.get("source") or "")
        key = (question_id, subquestion_id, logical_page, reason, source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def dedupe_corrections(corrections: list[Any]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str, str]] = set()
    for correction in corrections:
        if not isinstance(correction, dict):
            continue
        question_id = normalize_check_question_id(correction.get("question_id"))
        if question_id is None:
            continue
        subquestion_id = normalize_page_optional_id(correction.get("subquestion_id"))
        logical_page = str(correction.get("logical_page") or "")
        reason = str(correction.get("reason") or "")
        key = (question_id, subquestion_id, logical_page, reason)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(correction)
    return deduped


def append_final_answer_evidence(answer: dict[str, Any], evidence: str) -> None:
    current = answer.get("evidence")
    if isinstance(current, list):
        current.append(evidence)
    elif isinstance(current, str) and current.strip():
        answer["evidence"] = [current, evidence]
    else:
        answer["evidence"] = [evidence]


def is_freeform_survey_spec(survey_spec: dict[str, Any]) -> bool:
    mode = str(survey_spec.get("_template_mode") or survey_spec.get("template_mode") or "").strip().lower()
    return mode in {"none", "freeform", "template_free"} or survey_spec.get("_freeform") is True


FREEFORM_CATEGORY_LABELS = {
    "general": "Template-free PDF extraction",
    "cs30": "CS-30",
    "tug": "TUG",
    "four_stage": "4STAGE",
    "physical_function": "身体機能検査データ",
}

FREEFORM_CATEGORY_PROMPTS = {
    "general": "extract_page_freeform.md",
    "cs30": "extract_page_cs30.md",
    "tug": "extract_page_tug.md",
    "four_stage": "extract_page_4stage.md",
    "physical_function": "extract_page_physical_function.md",
}


def freeform_data_category(survey_spec: dict[str, Any]) -> str:
    raw = str(
        survey_spec.get("_data_category")
        or survey_spec.get("data_category")
        or survey_spec.get("category")
        or ""
    ).strip().lower()
    aliases = {
        "clinical": "physical_function",
        "body_function": "physical_function",
        "physical": "physical_function",
        "physical-function": "physical_function",
        "physical_function": "physical_function",
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
    return aliases.get(raw, raw if raw in FREEFORM_CATEGORY_PROMPTS else "general")


def freeform_category_label(survey_spec: dict[str, Any]) -> str:
    explicit = str(survey_spec.get("_data_category_label") or survey_spec.get("data_category_label") or "").strip()
    if explicit:
        return explicit
    return FREEFORM_CATEGORY_LABELS.get(freeform_data_category(survey_spec), FREEFORM_CATEGORY_LABELS["general"])


def freeform_prompt_name(data_category: str) -> str:
    return FREEFORM_CATEGORY_PROMPTS.get(data_category, FREEFORM_CATEGORY_PROMPTS["general"])


def questions_for_page(survey_spec: dict[str, Any], logical_page: int) -> list[dict[str, Any]]:
    questions = survey_spec.get("questions", [])
    selected: list[dict[str, Any]] = []
    for question in questions:
        source_page = question.get("source_page")
        if source_page is None:
            selected.append(question)
            continue
        try:
            if int(source_page) == logical_page:
                selected.append(question)
        except (TypeError, ValueError):
            selected.append(question)
    if selected:
        return selected
    return questions


def freeform_questions_for_page_check(page_result: dict[str, Any]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    logical_page = page_result.get("logical_page")
    for answer in page_result.get("answers") or []:
        if not isinstance(answer, dict):
            continue
        question_id = str(answer.get("question_id") or "").strip()
        if not question_id:
            continue
        questions.append(
            {
                "question_id": question_id,
                "source_page": logical_page,
                "label_japanese": answer.get("label_japanese") or answer.get("label") or question_id,
                "question_type": "freeform_extracted_field",
                "options": [],
            }
        )
    return questions


def split_pdf_text_pages(text: str) -> list[str]:
    return text.split("\f")


def merge_page_specs(page_specs: list[dict[str, Any]]) -> dict[str, Any]:
    title = None
    questions: list[dict[str, Any]] = []
    skip_rules: list[dict[str, Any]] = []
    for page_spec in page_specs:
        if not title and page_spec.get("survey_title"):
            title = page_spec.get("survey_title")
        questions.extend(page_spec.get("questions", []))
        skip_rules.extend(page_spec.get("skip_rules", []))
    questions = normalize_question_ids(questions)
    return {
        "survey_title": title or "Questionnaire",
        "language": "ja",
        "source_summary": "Survey specification generated page-by-page from the blank questionnaire PDF text.",
        "questions": questions,
        "global_skip_rules": skip_rules,
        "_page_spec_count": len(page_specs),
    }


def normalize_question_ids(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for question in questions:
        copied = dict(question)
        original_id = str(copied.get("question_id") or f"question_{len(normalized) + 1}")
        source_page = copied.get("source_page", "unknown")
        question_id = original_id
        if question_id in seen:
            question_id = f"p{source_page}_{original_id}"
            copied["_original_question_id"] = original_id
            copied["question_id"] = question_id
        seen.add(question_id)
        normalized.append(copied)
    return normalized


def remap_page_answer_ids(survey_spec: dict[str, Any], page_result: dict[str, Any]) -> None:
    logical_page = page_result.get("logical_page")
    if logical_page is None:
        return
    id_map: dict[str, str] = {}
    valid_ids: set[str] = set()
    for question in survey_spec.get("questions", []):
        try:
            same_page = int(question.get("source_page")) == int(logical_page)
        except (TypeError, ValueError):
            same_page = False
        if not same_page:
            continue
        question_id = str(question.get("question_id"))
        original_id = question.get("_original_question_id")
        valid_ids.add(question_id)
        if original_id:
            id_map[str(original_id)] = question_id

    for answer in page_result.get("answers", []):
        current_id = str(answer.get("question_id"))
        if current_id not in valid_ids and current_id in id_map:
            answer["_original_question_id"] = current_id
            answer["question_id"] = id_map[current_id]

    for request in page_result.get("ocr_requests", []):
        if not isinstance(request, dict):
            continue
        current_id = str(request.get("question_id"))
        if current_id not in valid_ids and current_id in id_map:
            request["_original_question_id"] = current_id
            request["question_id"] = id_map[current_id]


CHOICE_ANSWER_TYPES = {
    "single_choice",
    "multiple_choice",
    "checkbox",
    "table_single_choice",
    "table_multiple_choice",
}


def validate_applied_ocr_answers(page_result: dict[str, Any]) -> None:
    answers = page_result.get("answers")
    if not isinstance(answers, list):
        return

    for answer in answers:
        if not isinstance(answer, dict):
            continue
        ocr_result = answer.get("ocr_result")
        if not isinstance(ocr_result, dict) or ocr_result.get("applied") is not True:
            continue
        text = str(ocr_result.get("recognized_text") or answer.get("ocr_text") or "").strip()
        accepted, reason = should_apply_ocr_result(ocr_result, text)
        if accepted:
            continue
        ocr_result["applied"] = False
        ocr_result["apply_reason"] = reason
        if "_base_llm_value" in answer:
            base_value = answer.get("_base_llm_value")
            answer["value"] = base_value
            answer["raw_visible_answer"] = base_value
            answer["value_source"] = "base_llm"
        answer["needs_review"] = True
        append_answer_evidence(
            answer,
            f"Rejected OCR value {text!r}: {reason}.",
        )
    sync_rejected_ocr_results(page_result, answers)


def validate_choice_answers(survey_spec: dict[str, Any], page_result: dict[str, Any]) -> None:
    spec_index = build_answer_spec_index(survey_spec)
    answers = page_result.get("answers")
    if not isinstance(answers, list):
        return

    for answer in answers:
        if not isinstance(answer, dict):
            continue
        if answer.get("answer_status") != "answered":
            continue
        question_id = str(answer.get("question_id"))
        subquestion_id = normalize_page_optional_id(answer.get("subquestion_id"))
        answer_spec = spec_index.get((question_id, subquestion_id)) or spec_index.get((question_id, None))
        if not answer_spec:
            continue
        answer_type = str(answer_spec.get("answer_type") or "")
        options = [str(option) for option in answer_spec.get("options") or []]
        if answer_type not in CHOICE_ANSWER_TYPES or not options:
            continue

        value = answer.get("value")
        if isinstance(value, list):
            valid_values = [item for item in value if str(item) in options]
            if len(valid_values) == len(value) and valid_values:
                continue
            if valid_values:
                answer["value"] = valid_values
                answer["raw_visible_answer"] = valid_values
                answer["needs_review"] = True
                append_answer_evidence(
                    answer,
                    f"Removed invalid choice values not present in survey options: {value!r}.",
                )
                continue
            recover_or_mark_unclear(answer, options, value)
            continue

        if value is not None and str(value) in options:
            continue
        recover_or_mark_unclear(answer, options, value)
    sync_rejected_ocr_results(page_result, answers)


def build_answer_spec_index(survey_spec: dict[str, Any]) -> dict[tuple[str, str | None], dict[str, Any]]:
    spec_index: dict[tuple[str, str | None], dict[str, Any]] = {}
    for question in survey_spec.get("questions", []):
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("question_id"))
        spec_index[(question_id, None)] = {
            "answer_type": question.get("answer_type"),
            "options": question.get("options") or [],
        }
        for subquestion in question.get("subquestions") or []:
            if not isinstance(subquestion, dict):
                continue
            subquestion_id = normalize_page_optional_id(subquestion.get("subquestion_id"))
            if subquestion_id is None:
                continue
            spec_index[(question_id, subquestion_id)] = {
                "answer_type": subquestion.get("answer_type") or question.get("answer_type"),
                "options": subquestion.get("options") or question.get("options") or [],
            }
    return spec_index


def recover_or_mark_unclear(answer: dict[str, Any], options: list[str], invalid_value: Any) -> None:
    base_value = answer.get("_base_llm_value")
    if isinstance(base_value, list):
        recovered_values = [item for item in base_value if str(item) in options]
        if recovered_values:
            answer["value"] = recovered_values
            answer["raw_visible_answer"] = recovered_values
            answer["value_source"] = "base_llm"
            answer["needs_review"] = True
            mark_ocr_result_rejected(answer, invalid_value)
            append_answer_evidence(
                answer,
                f"Rejected invalid OCR/choice value {invalid_value!r}; restored Base LLM option value {recovered_values!r}.",
            )
            return
    elif base_value is not None and str(base_value) in options:
        answer["value"] = str(base_value)
        answer["raw_visible_answer"] = str(base_value)
        answer["value_source"] = "base_llm"
        answer["needs_review"] = True
        mark_ocr_result_rejected(answer, invalid_value)
        append_answer_evidence(
            answer,
            f"Rejected invalid OCR/choice value {invalid_value!r}; restored Base LLM option value {base_value!r}.",
        )
        return

    answer["answer_status"] = "unclear"
    answer["value"] = None
    answer["needs_review"] = True
    if "confidence" in answer:
        try:
            answer["confidence"] = min(float(answer.get("confidence")), 0.4)
        except (TypeError, ValueError):
            answer["confidence"] = 0.4
    else:
        answer["confidence"] = 0.4
    mark_ocr_result_rejected(answer, invalid_value)
    append_answer_evidence(
        answer,
        f"Answer value {invalid_value!r} is not one of the allowed survey options: {options!r}.",
    )


def mark_ocr_result_rejected(answer: dict[str, Any], invalid_value: Any) -> None:
    ocr_result = answer.get("ocr_result")
    if isinstance(ocr_result, dict) and ocr_result.get("applied") is True:
        ocr_result["applied"] = False
        ocr_result["apply_reason"] = f"Rejected because {invalid_value!r} is not an allowed choice option"


def sync_rejected_ocr_results(page_result: dict[str, Any], answers: list[Any]) -> None:
    rejected_by_request_id: dict[str, str | None] = {}
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        ocr_result = answer.get("ocr_result")
        if not isinstance(ocr_result, dict) or ocr_result.get("applied") is not False:
            continue
        request_id = ocr_result.get("request_id")
        if request_id:
            rejected_by_request_id[str(request_id)] = ocr_result.get("apply_reason")

    if not rejected_by_request_id:
        return
    for result in page_result.get("ocr_results") or []:
        if not isinstance(result, dict):
            continue
        request_id = result.get("request_id")
        if request_id is None or str(request_id) not in rejected_by_request_id:
            continue
        result["applied"] = False
        result["apply_reason"] = rejected_by_request_id[str(request_id)]


def append_answer_evidence(answer: dict[str, Any], evidence: str) -> None:
    current = answer.get("evidence")
    if isinstance(current, str) and current.strip():
        answer["evidence"] = f"{current.rstrip()} {evidence}"
    else:
        answer["evidence"] = evidence


def normalize_page_optional_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def deterministic_consolidate(
    *,
    person_id: str,
    pdf_path: Path,
    survey_spec: dict[str, Any],
    page_results: list[dict[str, Any]],
    error: str,
) -> dict[str, Any]:
    extracted: dict[tuple[str, str | None], dict[str, Any]] = {}
    for page_result in page_results:
        logical_page = page_result.get("logical_page")
        for answer in page_result.get("answers", []):
            question_id = str(answer.get("question_id"))
            subquestion_id = answer.get("subquestion_id")
            if subquestion_id is not None:
                subquestion_id = str(subquestion_id)
            key = (question_id, subquestion_id)
            current = extracted.get(key)
            if current is None or answer_rank(answer) >= answer_rank(current):
                copied = dict(answer)
                copied["_logical_page"] = logical_page
                extracted[key] = copied

    answers: list[dict[str, Any]] = []
    for question in survey_spec.get("questions", []):
        subquestions = question.get("subquestions") or []
        if subquestions:
            question_id = str(question.get("question_id"))
            if (question_id, None) in extracted:
                answers.append(build_answer_record(question, None, extracted))
            for subquestion in subquestions:
                answers.append(build_answer_record(question, subquestion, extracted))
        else:
            answers.append(build_answer_record(question, None, extracted))

    apply_skip_rules(answers, survey_spec.get("global_skip_rules", []))
    review_items = [
        {
            "question_id": item["question_id"],
            "reason": review_reason(item),
            "severity": "medium" if item["status"] in {"unclear", "not_answered"} else "low",
        }
        for item in answers
        if item.get("needs_review") or item["status"] in {"unclear", "conflict"}
    ]

    return {
        "person_id": person_id,
        "source_pdf": str(pdf_path),
        "survey_title": survey_spec.get("survey_title"),
        "answers": answers,
        "ocr_summary": collect_ocr_summary(page_results),
        "review_items": review_items,
        "processing_notes": [error],
        "skip_rules": survey_spec.get("global_skip_rules", []),
    }


def deterministic_freeform_consolidate(
    *,
    person_id: str,
    pdf_path: Path,
    page_results: list[dict[str, Any]],
    note: str,
    data_category: str = "general",
    category_label: str | None = None,
) -> dict[str, Any]:
    answers: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    category_label = category_label or FREEFORM_CATEGORY_LABELS.get(data_category, FREEFORM_CATEGORY_LABELS["general"])
    for page_result in page_results:
        logical_page = page_result.get("logical_page")
        page_answers = answer_dicts_from_page(page_result)
        page_summaries.append(
            {
                "logical_page": logical_page,
                "physical_page": page_result.get("_physical_page"),
                "side": page_result.get("_side"),
                "page_kind": page_result.get("page_kind"),
                "answer_count": len(page_answers),
                "page_notes": page_result.get("page_notes") or [],
            }
        )
        for index, answer in enumerate(page_answers, start=1):
            answers.append(normalize_freeform_answer(answer, logical_page, index, data_category=data_category))

    review_items = [
        {
            "question_id": item["question_id"],
            "subquestion_id": item.get("subquestion_id"),
            "reason": review_reason(item),
            "severity": "medium" if item["status"] in {"unclear", "not_answered"} else "low",
            "logical_page": item.get("logical_page"),
        }
        for item in answers
        if item.get("needs_review") or item["status"] in {"unclear", "conflict"}
    ]

    return {
        "person_id": person_id,
        "source_pdf": str(pdf_path),
        "survey_title": category_label,
        "template_mode": "none",
        "data_category": data_category,
        "data_category_label": category_label,
        "answers": answers,
        "freeform_pages": page_summaries,
        "ocr_summary": collect_ocr_summary(page_results),
        "review_items": dedupe_review_items(review_items),
        "processing_notes": [note],
        "skip_rules": [],
    }


def answer_dicts_from_page(page_result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_answers = page_result.get("answers")
    if not isinstance(raw_answers, list):
        return []
    return [answer for answer in raw_answers if isinstance(answer, dict)]


def normalize_freeform_answer(answer: dict[str, Any], logical_page: Any, index: int, *, data_category: str = "general") -> dict[str, Any]:
    try:
        page_number = int(logical_page)
    except (TypeError, ValueError):
        page_number = 0
    fallback_id = f"page_{page_number:03d}_field_{index:03d}" if page_number > 0 else f"field_{index:03d}"
    question_id = str(answer.get("question_id") or fallback_id).strip() or fallback_id
    subquestion_id = normalize_page_optional_id(answer.get("subquestion_id"))
    status = str(answer.get("answer_status") or answer.get("status") or "").strip()
    value = answer.get("value")
    if status not in {"answered", "not_answered", "unclear", "skipped", "conflict"}:
        status = "answered" if value is not None and value != "" else "unclear"
    try:
        confidence = float(answer.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5 if status == "answered" else 0.0
    evidence = answer.get("evidence")
    if isinstance(evidence, list):
        evidence_list = [str(item) for item in evidence if str(item).strip()]
    elif evidence is None:
        evidence_list = []
    else:
        evidence_list = [str(evidence)]
    if logical_page is not None:
        evidence_list.append(f"logical_page={logical_page}")
    needs_review = bool(answer.get("needs_review") or status in {"unclear", "conflict"}
                        or (status == "answered" and confidence < 0.6))
    ocr_result = answer.get("ocr_result")
    value_source = answer.get("value_source") or ("ocr" if isinstance(ocr_result, dict) and ocr_result.get("applied") else "base_llm")
    record = {
        "question_id": question_id,
        "subquestion_id": subquestion_id,
        "label_japanese": answer.get("label_japanese") or answer.get("label") or question_id,
        "data_category": answer.get("data_category") or data_category,
        "status": status,
        "value": value,
        "raw_visible_answer": answer.get("raw_visible_answer"),
        "value_source": value_source,
        "confidence": confidence,
        "evidence": evidence_list,
        "rule_trace": [],
        "needs_review": needs_review,
        "logical_page": logical_page,
    }
    for key in (
        "participant_id",
        "participant_name",
        "normalized_key",
        "measurement_group",
        "test_name",
        "test_component",
        "unit",
        "category",
        "source_row",
        "row_number",
        "source_table",
        "row_label",
        "column_label",
        "field_role",
        "source_bbox",
        "bbox_units",
    ):
        if answer.get(key) not in (None, ""):
            record[key] = answer.get(key)
    if ocr_result:
        record["ocr_result"] = ocr_result
    return record


def build_answer_record(
    question: dict[str, Any],
    subquestion: dict[str, Any] | None,
    extracted: dict[tuple[str, str | None], dict[str, Any]],
) -> dict[str, Any]:
    question_id = str(question.get("question_id"))
    subquestion_id = str(subquestion.get("subquestion_id")) if subquestion else None
    extracted_answer = extracted.get((question_id, subquestion_id)) or extracted.get((question_id, None))
    label_japanese = subquestion.get("label_japanese") if subquestion else question.get("label_japanese")
    if extracted_answer:
        status = extracted_answer.get("answer_status", "unclear")
        value = extracted_answer.get("value")
        confidence = extracted_answer.get("confidence", 0.0)
        raw_evidence = extracted_answer.get("evidence")
        evidence = ([str(item) for item in raw_evidence] if isinstance(raw_evidence, list)
                    else [str(raw_evidence)] if raw_evidence else [])
        value_source = extracted_answer.get("value_source") or "base_llm"
        ocr_result = extracted_answer.get("ocr_result")
        needs_review = bool(
            extracted_answer.get("needs_review")
            or status in {"unclear", "conflict"}
        )
        if extracted_answer.get("_logical_page"):
            evidence.append(f"logical_page={extracted_answer.get('_logical_page')}")
    else:
        status = "not_answered"
        value = None
        confidence = 0.0
        evidence = []
        value_source = None
        ocr_result = None
        needs_review = True
    record = {
        "question_id": question_id,
        "subquestion_id": subquestion_id,
        "label_japanese": label_japanese,
        "status": status,
        "value": value,
        "value_source": value_source,
        "confidence": confidence,
        "evidence": evidence,
        "rule_trace": [],
        "needs_review": needs_review,
    }
    if ocr_result:
        record["ocr_result"] = ocr_result
    if extracted_answer:
        record["raw_visible_answer"] = extracted_answer.get("raw_visible_answer")
        record["logical_page"] = extracted_answer.get("_logical_page")
        for key in ("field_role", "source_bbox", "bbox_units"):
            if extracted_answer.get(key) not in (None, ""):
                record[key] = extracted_answer[key]
    return record


def review_reason(answer: dict[str, Any]) -> str:
    ocr_result = answer.get("ocr_result")
    if isinstance(ocr_result, dict) and ocr_result.get("applied") is False:
        reason = ocr_result.get("apply_reason") or "OCR result was not applied."
        return f"OCR result requires review: {reason}"
    if answer["status"] == "not_answered":
        return "No visible answer was extracted."
    if answer["status"] == "unclear":
        return "Low confidence or unclear answer."
    if answer["status"] == "conflict":
        return "Visible answer conflicts with skip rule."
    return "Marked for review."


def collect_ocr_summary(page_results: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for page_result in page_results:
        logical_page = page_result.get("logical_page")
        for result in page_result.get("ocr_results") or []:
            if not isinstance(result, dict):
                continue
            results.append(
                {
                    "logical_page": logical_page,
                    "request_id": result.get("request_id"),
                    "question_id": result.get("question_id"),
                    "subquestion_id": result.get("subquestion_id"),
                    "status": result.get("status"),
                    "source": result.get("source") or "ocr",
                    "engine": result.get("ocr_engine"),
                    "backend": result.get("ocr_backend"),
                    "recognized_text": result.get("recognized_text"),
                    "applied": result.get("applied"),
                    "apply_reason": result.get("apply_reason"),
                    "bbox": result.get("bbox"),
                    "crop_path": result.get("crop_path"),
                    "content_type": result.get("content_type"),
                    "rough_read": result.get("rough_read"),
                    "error": result.get("error"),
                }
            )
    return {
        "used": any(item.get("status") == "ok" for item in results),
        "request_count": len(results),
        "ok_count": sum(1 for item in results if item.get("status") == "ok"),
        "applied_count": sum(1 for item in results if item.get("applied") is True),
        "rejected_count": sum(1 for item in results if item.get("applied") is False),
        "results": results,
    }


def answer_rank(answer: dict[str, Any]) -> int:
    status = answer.get("answer_status")
    if status == "answered":
        return 3
    if status == "unclear":
        return 2
    if status == "not_answered":
        return 1
    return 0


def apply_skip_rules(answers: list[dict[str, Any]], skip_rules: list[dict[str, Any]]) -> None:
    value_by_question: dict[str, Any] = {}
    for answer in answers:
        if answer.get("subquestion_id") is None:
            value_by_question[str(answer.get("question_id"))] = answer.get("value")

    for rule in repair_known_rule_ids(skip_rules, value_by_question):
        if rule.get("action") != "skip":
            continue
        condition = str(rule.get("condition", ""))
        if not evaluate_condition(condition, value_by_question):
            continue
        rule_text = f"{rule.get('rule_id', 'skip_rule')}: {condition} -> skip {rule.get('target_question_ids', [])}"
        targets = {str(item) for item in rule.get("target_question_ids", [])}
        for answer in answers:
            if str(answer.get("question_id")) not in targets:
                continue
            answer.setdefault("rule_trace", []).append(rule_text)
            if answer.get("status") == "answered":
                answer["status"] = "conflict"
                answer["needs_review"] = True
                answer.setdefault("evidence", []).append("Visible answer conflicts with skip rule.")
            else:
                answer["status"] = "skipped"
                answer["needs_review"] = False


def repair_known_rule_ids(skip_rules: list[dict[str, Any]], value_by_question: dict[str, Any]) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    for rule in skip_rules:
        copied = dict(rule)
        condition = str(copied.get("condition", ""))
        targets = {str(item) for item in copied.get("target_question_ids", [])}
        if "Q15" in targets and "Q5" in condition and "問５" in value_by_question:
            copied["condition"] = condition.replace("Q5", "問５")
        repaired.append(copied)
    return repaired


def evaluate_condition(condition: str, value_by_question: dict[str, Any]) -> bool:
    parts = [part.strip() for part in condition.split("AND")]
    if not parts:
        return False
    return all(evaluate_equality(part, value_by_question) for part in parts)


def evaluate_equality(expr: str, value_by_question: dict[str, Any]) -> bool:
    if "==" not in expr:
        return False
    left, right = expr.split("==", 1)
    question_id = left.strip().strip('"').strip("'")
    expected = right.strip().strip('"').strip("'")
    actual = value_by_question.get(question_id)
    if actual is None:
        return False
    return str(actual).strip() == expected
