from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.base_llm.pipeline import BaseLLMPipeline
from src.base_llm.database_export import write_database_exports


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Base LLM questionnaire extraction pipeline.")
    parser.add_argument("--scheme", default="anket/scheme.pdf", help="Path to the blank questionnaire PDF.")
    parser.add_argument("--input-dir", default="anket/split_pdfs", help="Directory containing split person PDFs.")
    parser.add_argument("--output-dir", default="outputs/base_llm", help="Output directory.")
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override. Otherwise uses the model configured for the selected provider.",
    )
    parser.add_argument("--dpi", type=int, default=130, help="PDF render DPI.")
    parser.add_argument("--max-image-width", type=int, default=1400, help="Maximum rendered logical page width.")
    parser.add_argument("--limit", type=int, default=1, help="Number of person PDFs to process. Use 0 for scheme only.")
    parser.add_argument("--force-scheme", action="store_true", help="Rebuild survey_spec.json from scheme PDF.")
    parser.add_argument("--force-pages", action="store_true", help="Rebuild person page extraction JSON files.")
    parser.add_argument("--llm-consolidate", action="store_true", help="Use LLM for final person-level consolidation.")
    parser.add_argument("--enable-ocr", action="store_true", help="Run OCR only for Base LLM requested handwritten bboxes.")
    parser.add_argument(
        "--ocr-model-path",
        default="models/PaddleOCR-VL-1.5",
        help="Path to the local PaddleOCR-VL model directory.",
    )
    parser.add_argument("--ocr-device", default="auto", help="OCR device: auto, cuda, cuda:0, or cpu.")
    parser.add_argument("--ocr-padding", type=int, default=8, help="Pixel padding added around OCR bboxes.")
    parser.add_argument("--ocr-max-new-tokens", type=int, default=256, help="Maximum OCR generation tokens per crop.")
    parser.add_argument("--force-ocr", action="store_true", help="Re-run OCR even when page JSON already has OCR results.")
    parser.add_argument("--enable-check-llm", action="store_true", help="Run a visual Check LLM pass to flag suspicious answers.")
    parser.add_argument(
        "--check-model",
        default=None,
        help="Optional Check LLM model override. Defaults to the selected provider's model.",
    )
    parser.add_argument(
        "--apply-check-corrections",
        action="store_true",
        help="Apply Check LLM suggested corrections to the final person JSON.",
    )
    parser.add_argument("--force-check", action="store_true", help="Re-run Check LLM even when check_report.json exists.")
    return parser.parse_args()


def sorted_person_pdfs(input_dir: Path) -> list[Path]:
    def key(path: Path) -> tuple[str, int]:
        stem = path.stem
        if "_part_" in stem:
            prefix, number = stem.rsplit("_part_", 1)
            try:
                return prefix, int(number)
            except ValueError:
                pass
        return stem, 0

    return sorted((p for p in input_dir.glob("*.pdf") if not p.name.startswith("._")), key=key)


def main() -> None:
    args = parse_args()
    project_root = PROJECT_ROOT
    pipeline = BaseLLMPipeline(
        project_root=project_root,
        model=args.model,
        output_dir=project_root / args.output_dir,
        dpi=args.dpi,
        max_image_width=args.max_image_width,
        llm_consolidate=args.llm_consolidate,
        enable_ocr=args.enable_ocr,
        ocr_model_path=project_root / args.ocr_model_path,
        ocr_device=args.ocr_device,
        ocr_padding=args.ocr_padding,
        ocr_max_new_tokens=args.ocr_max_new_tokens,
        force_pages=args.force_pages,
        force_ocr=args.force_ocr,
        enable_check_llm=args.enable_check_llm,
        check_model=args.check_model,
        apply_check_corrections=args.apply_check_corrections,
        force_check=args.force_check,
    )
    scheme_pdf = project_root / args.scheme
    survey_spec = pipeline.build_survey_spec(scheme_pdf, force=args.force_scheme)

    pdfs = sorted_person_pdfs(project_root / args.input_dir)
    if args.limit <= 0:
        pdfs = []
    else:
        pdfs = pdfs[: args.limit]
    for pdf_path in pdfs:
        print(f"Processing {pdf_path}")
        final_record = pipeline.process_person_pdf(pdf_path, survey_spec)
        raw_path = pipeline.output_dir / "persons" / f"{final_record.get('person_id', pdf_path.stem)}.json"
        export_result = write_database_exports(raw_path, pipeline.output_dir)
        print(f"Database JSON: {export_result['database_json']}")
        print(f"Evidence JSON: {export_result['evidence_json']}")
        print(f"Finished {final_record.get('person_id', pdf_path.stem)}")


if __name__ == "__main__":
    main()
