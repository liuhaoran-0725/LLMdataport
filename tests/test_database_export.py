from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.base_llm.database_export import build_database_exports, write_database_exports


def answer(qid, label="質問", value=None, status="not_answered", evidence="記入欄は空白。", **extra):
    return {"question_id": qid, "label_japanese": label, "value": value,
            "status": status, "logical_page": 1, "evidence": [evidence, "logical_page=1"], **extra}


class ExportTests(unittest.TestCase):
    def export(self, answers, **extra):
        record = {"person_id": "test", "template_mode": "none", "data_category": "general",
                  "answers": answers, **extra}
        before = deepcopy(record)
        main, side = build_database_exports(record, raw_path=Path('/tmp/output/persons/test.json'),
                                             output_root=Path('/tmp/output'))
        self.assertEqual(record, before)
        for retained in main['answers']:
            evidence = next(e for e in side['entries'] if e['evidence_id'] == retained['evidence_id'])
            original = answers[evidence['source_answer_index']]
            self.assertEqual(retained['value'], original['value'])
            self.assertEqual(retained['status'], original['status'])
        return main, side

    def test_blanks_false_zero_and_explicit_skipped_are_preserved(self):
        main, _ = self.export([answer('blank'), answer('no', value=False, status='answered'),
                               answer('zero', value=0, status='answered'), answer('skip', status='skipped')])
        self.assertEqual([a['value'] for a in main['answers']], [None, False, 0, None])
        self.assertEqual(main['answers'][0]['status'], 'not_answered')

    def test_only_unselected_options_with_selected_parent_are_removed(self):
        main, side = self.export([answer('q', value='はい', status='answered'),
                                  answer('q_opt2', evidence='No circle/checkmark visible.'),
                                  answer('missing_opt1', evidence='No circle/checkmark visible.'),
                                  answer('q_other', label='その他の記入欄')])
        self.assertEqual([a['question_id'] for a in main['answers']], ['q', 'missing_opt1', 'q_other'])
        self.assertEqual(side['excluded_fields'][0]['reason_code'], 'unselected_option')

    def test_single_choice_table_and_repeated_measurement_rows(self):
        label = '職業（当てはまるもの１つ）'
        main, _ = self.export([answer('page_001_table_001_row_001', label, '職業A', 'answered'),
                               answer('page_001_table_001_row_002', label, evidence='○印なし'),
                               answer('page_001_table_002_row_001', '握力', 23.4, 'answered', unit='kg'),
                               answer('page_001_table_002_row_002', '握力')])
        self.assertEqual(len(main['answers']), 3)
        self.assertEqual(main['answers'][1]['unit'], 'kg')

    def test_ambiguous_multiple_selections_are_not_filtered(self):
        label = '職業（１つに○）'
        main, _ = self.export([answer('page_001_table_001_row_001', label, 'A', 'answered'),
                               answer('page_001_table_001_row_002', label, 'B', 'answered'),
                               answer('page_001_table_001_row_003', label, evidence='○印なし')])
        self.assertEqual(len(main['answers']), 3)

    def test_printed_context_removed_but_handwriting_consent_and_narrative_kept(self):
        text = '長い記述回答。' * 30
        main, side = self.export([answer('page_001_title', 'タイトル', '質問票', 'answered', 'Printed title'),
                                  answer('page_001_footer', 'フッター', '記入内容', 'answered', 'Handwritten footer note'),
                                  answer('consent', '同意', True, 'answered', '同意欄にチェックあり'),
                                  answer('narrative', '自由記述', text, 'answered', '手書きの記入あり')])
        self.assertEqual([a['question_id'] for a in main['answers']], ['page_001_footer', 'consent', 'narrative'])
        self.assertEqual(main['answers'][-1]['value'], text)
        self.assertEqual(len(side['entries']), 4)

    def test_cleanup_is_restricted_to_general_freeform_legacy_records(self):
        rows = [answer('page_001_title', 'タイトル', '測定名', 'answered', 'Printed title')]
        main, _ = self.export(rows, data_category='physical_function')
        self.assertEqual(len(main['answers']), 1)
        main, _ = self.export(rows, template_mode='existing')
        self.assertEqual(len(main['answers']), 1)

    def test_unclear_mark_is_flagged_without_recoding(self):
        main, side = self.export([answer('mark', value='○', status='answered'),
                                  answer('unclear', value='2～3', status='unclear')])
        self.assertTrue(all(a['needs_review'] for a in main['answers']))
        self.assertIn('selection_symbol_value', side['entries'][0]['review_codes'])

    def test_locations_raw_words_rules_and_corrections_stay_linked(self):
        bbox = {'x1': 5, 'y1': 6, 'x2': 20, 'y2': 30}
        main, side = self.export([answer('a', value=3, status='answered', raw_visible_answer='３',
                                        confidence=.9, source_bbox=bbox, bbox_units='image_pixels',
                                        rule_trace=['rule_1'], check_llm_corrections=[{'before': {'value': 4}}])],
                                 freeform_pages=[{'logical_page':1,'physical_page':2,'side':'left','page_notes':['long']}])
        self.assertNotIn('raw_visible_answer', main['answers'][0])
        entry = side['entries'][0]
        self.assertEqual(entry['raw_visible_answer'], '３')
        self.assertEqual(entry['source_bbox'], bbox)
        self.assertEqual(entry['rule_trace'], ['rule_1'])
        self.assertIn('check_llm_corrections', entry)
        self.assertNotIn('logical_page=1', entry['observations'])
        self.assertEqual(side['pages'][0]['physical_page'], 2)

    def test_duplicate_question_ids_have_distinct_evidence(self):
        main, side = self.export([answer('duplicate', value=1, status='answered'),
                                  answer('duplicate', value=2, status='answered')])
        self.assertEqual(len(main['answers']), 2)
        self.assertEqual(len({e['evidence_id'] for e in side['entries']}), 2)

    def test_no_location_is_fabricated_and_raw_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root/'persons/test.json'
            raw.parent.mkdir()
            raw.write_text(json.dumps({'person_id':'test','answers':[answer('q')]}), encoding='utf-8')
            original = raw.read_bytes()
            result = write_database_exports(raw)
            self.assertEqual(raw.read_bytes(), original)
            side = json.loads(Path(result['evidence_json']).read_text())
            self.assertEqual(side['provenance']['raw_json_sha256'], hashlib.sha256(original).hexdigest())
            self.assertNotIn('source_bbox', side['entries'][0])
            self.assertEqual(side['pages'], [])

    def test_missing_question_id_fails_without_silent_loss(self):
        with self.assertRaises(ValueError):
            self.export([{'value': 1}])


if __name__ == '__main__':
    unittest.main()
