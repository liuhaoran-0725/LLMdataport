from __future__ import annotations

import argparse
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: pypdf. Install it with: python -m pip install pypdf"
    ) from exc


def split_pdf_by_person(
    input_pdf: Path,
    output_dir: Path,
    *,
    pages_per_person: int = 8,
    prefix: str | None = None,
    start_index: int = 1,
    overwrite: bool = False,
    allow_incomplete_last: bool = False,
) -> list[Path]:
    input_pdf = input_pdf.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()

    if pages_per_person <= 0:
        raise ValueError("--pages-per-person must be greater than 0")
    if start_index <= 0:
        raise ValueError("--start-index must be greater than 0")
    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")
    if input_pdf.suffix.lower() != ".pdf":
        raise ValueError(f"Input file must be a PDF: {input_pdf}")

    reader = PdfReader(str(input_pdf))
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise ValueError(f"Input PDF has no pages: {input_pdf}")

    remainder = total_pages % pages_per_person
    if remainder and not allow_incomplete_last:
        raise ValueError(
            f"{input_pdf.name} has {total_pages} pages, which is not divisible by "
            f"{pages_per_person}. Use --allow-incomplete-last to save the final "
            "short PDF anyway."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    file_prefix = prefix or input_pdf.stem
    created_files: list[Path] = []

    for start_page in range(0, total_pages, pages_per_person):
        end_page = min(start_page + pages_per_person, total_pages)
        person_index = start_index + start_page // pages_per_person
        output_pdf = output_dir / f"{file_prefix}_part_{person_index}.pdf"

        if output_pdf.exists() and not overwrite:
            raise FileExistsError(
                f"Output already exists: {output_pdf}. Use --overwrite to replace it."
            )

        writer = PdfWriter()
        for page_index in range(start_page, end_page):
            writer.add_page(reader.pages[page_index])

        temp_pdf = output_pdf.with_suffix(output_pdf.suffix + ".tmp")
        with temp_pdf.open("wb") as file_obj:
            writer.write(file_obj)
        temp_pdf.replace(output_pdf)

        created_files.append(output_pdf)
        print(
            f"Saved {output_pdf} "
            f"(source pages {start_page + 1}-{end_page}, {end_page - start_page} pages)"
        )

    print(f"Done. Created {len(created_files)} PDF file(s) from {total_pages} pages.")
    return created_files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split a PDF into one output PDF per person."
    )
    parser.add_argument("input_pdf", type=Path, help="Source PDF path.")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output folder. Defaults to <input PDF folder>/split_pdfs.",
    )
    parser.add_argument(
        "--pages-per-person",
        type=int,
        default=8,
        help="Number of source pages per person. Default: 8.",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Output filename prefix. Defaults to the input PDF stem.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="First person number used in output filenames. Default: 1.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing output PDFs with the same names.",
    )
    parser.add_argument(
        "--allow-incomplete-last",
        action="store_true",
        help="Save a final short PDF if the total page count is not divisible.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_pdf = args.input_pdf
    output_dir = args.output_dir or input_pdf.parent / "split_pdfs"
    split_pdf_by_person(
        input_pdf,
        output_dir,
        pages_per_person=args.pages_per_person,
        prefix=args.prefix,
        start_index=args.start_index,
        overwrite=args.overwrite,
        allow_incomplete_last=args.allow_incomplete_last,
    )


if __name__ == "__main__":
    main()
