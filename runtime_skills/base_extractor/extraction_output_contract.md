Runtime output contract (applies to every extraction category and custom template):
- Return structured source data only. Never include reasoning steps or justification in value, raw_visible_answer, evidence, or page_notes.
- Keep the respondent's actual narrative answers verbatim, even if long. Keep domain-specific measurement fields and units requested by the category prompt.
- Evidence is a short observable fact in Japanese, normally one sentence <=80 characters. Do not repeat the complete question, all unselected options, or a page summary.
- A single-choice question produces one selected answer (or one missing/unclear record). Do not emit each unselected printed option as a separate answer. Preserve distinct questions, write-in fields, and repeated measurement rows.
- Do not output standalone printed headings, instructions, page numbers, or footers as answers.
- Never guess missing values, infer a clinical conclusion, or infer skipped status during page extraction.
- Use source_bbox with bbox_units="image_pixels" only if the answer/mark can be located in the supplied image; otherwise omit the box. Do not invent a location.
