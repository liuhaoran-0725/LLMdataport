"""Export existing extraction JSON without an API call or OCR."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from src.base_llm.database_export import write_database_exports


def main() -> None:
    parser = argparse.ArgumentParser(description="Create database JSON and evidence from an existing persons JSON.")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--output-dir", type=Path, help="Default: the parent of the persons directory.")
    args = parser.parse_args()
    result = write_database_exports(args.json_path, args.output_dir)
    for key in ("database_json", "evidence_json", "source_answer_count", "database_answer_count", "excluded_counts"):
        print(f"{key}: {result[key]}")


if __name__ == "__main__":
    main()
