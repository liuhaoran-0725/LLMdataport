from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from src.base_llm.pipeline import BaseLLMPipeline, deterministic_freeform_consolidate, normalize_freeform_answer


class FakeClient:
    def chat_json(self, prompt, **kwargs):
        self.prompt = prompt
        return {'answers': [], 'ocr_requests': [{'request_id': 'should_be_removed'}]}


class ExtractionContractTests(unittest.TestCase):
    def test_runtime_contract_and_disabled_ocr_apply_to_both_paths(self):
        pipeline = BaseLLMPipeline.__new__(BaseLLMPipeline)
        pipeline.default_skill_dir = Path(__file__).resolve().parents[1]/'runtime_skills/base_extractor'
        pipeline.skill_dir = pipeline.default_skill_dir
        pipeline.enable_ocr = False
        pipeline.model = 'fake-model'
        pipeline.provider = 'fake-provider'
        pipeline.client = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'page.png'
            Image.new('RGB', (100, 100)).save(path)
            results = [pipeline.extract_page(image_path=path, logical_page=1, relevant_questions=[]),
                       pipeline.extract_page_freeform(image_path=path, logical_page=1)]
        for result in results:
            self.assertEqual(result['ocr_requests'], [])
            self.assertEqual(result['_extraction_provenance']['model'], 'fake-model')
            self.assertEqual(len(result['_extraction_provenance']['prompt_sha256']), 64)
        self.assertIn('Local OCR enabled: false', pipeline.client.prompt)
        self.assertIn('Never include reasoning steps', pipeline.client.prompt)

    def test_blank_does_not_create_automatic_review_and_bbox_survives(self):
        raw = {'question_id':'q', 'answer_status':'not_answered','value':None,
               'confidence':0, 'evidence':'記入欄は空白。', 'needs_review':False,
               'source_bbox':{'x1':1,'y1':2,'x2':3,'y2':4},'bbox_units':'image_pixels'}
        normalized = normalize_freeform_answer(raw, 1, 1)
        self.assertFalse(normalized['needs_review'])
        self.assertEqual(normalized['source_bbox'], raw['source_bbox'])
        consolidated = deterministic_freeform_consolidate(person_id='test', pdf_path=Path('test.pdf'),
            page_results=[{'logical_page':1,'answers':[raw]}], note='test')
        self.assertEqual(consolidated['review_items'], [])


if __name__ == '__main__':
    unittest.main()
