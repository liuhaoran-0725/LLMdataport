# Template-specific prompt overrides

Custom PDF templates can override the default extraction prompts by adding files under:

```text
runtime_skills/template_overrides/<template_id>/
```

Supported override filenames match the default prompt files in `runtime_skills/base_extractor/`:

```text
build_survey_page_spec.md
extract_page_answers.md
extract_page_cs30.md
extract_page_tug.md
extract_page_4stage.md
consolidate_person_answers.md
check_page_blind_read.md
check_page_compare.md
```

Missing files automatically fall back to the default `runtime_skills/base_extractor/` prompt.
