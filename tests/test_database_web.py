from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import serve_frontend_demo as web
from src.base_llm.database_export import write_database_exports


class DatabaseWebTests(unittest.TestCase):
    def run_job(self, folder, fail_export=False):
        root = Path(folder)
        inputs = root/'input'
        inputs.mkdir()
        (inputs/'test.pdf').write_bytes(b'mocked PDF; not parsed')
        output = root/'output'
        job = {'person_id':'test', 'options':{'template_mode':'none'}}
        with ExitStack() as stack:
            for name, value in {'INPUT_PDF_ROOT':inputs, 'OUTPUT_ROOT':output,
                                'PERSONS_ROOT':output/'persons', 'JOBS':{'test-job':job}}.items():
                stack.enter_context(patch.object(web, name, value))
            for name in ['ensure_poppler_on_path','monitor_pipeline_job','refresh_job_progress','summarize_record']:
                stack.enter_context(patch.object(web, name))
            pipeline = stack.enter_context(patch('src.base_llm.pipeline.BaseLLMPipeline'))
            pipeline.return_value.process_person_pdf.return_value = {
                'person_id':'test', 'answers':[{'question_id':'q','status':'answered','value':False}]}
            if fail_export:
                stack.enter_context(patch('src.base_llm.database_export.write_database_exports',
                                          side_effect=OSError('export destination unavailable')))
            web.run_pipeline_job('test-job', 'test', job['options'])
        return job, output

    def test_completed_extraction_automatically_exports_both_files(self):
        with tempfile.TemporaryDirectory() as folder:
            job, output = self.run_job(folder)
            self.assertEqual(job['status'], 'finished')
            self.assertEqual(job['database_export_status'], 'finished')
            self.assertTrue((output/'database/test.json').exists())
            self.assertTrue((output/'evidence/test.json').exists())
            raw = json.loads((output/'persons/test.json').read_text())
            self.assertEqual(raw['template_id'], 'none')
            self.assertFalse(raw['answers'][0]['value'])

    def test_export_failure_preserves_raw_and_is_visible_in_completion_event(self):
        with tempfile.TemporaryDirectory() as folder:
            job, output = self.run_job(folder, fail_export=True)
            self.assertEqual(job['database_export_status'], 'failed')
            self.assertTrue((output/'persons/test.json').exists())
            self.assertIn('入庫用JSONの作成に失敗', job['events'][-1]['message'])

    def test_json_routes_return_corresponding_export_without_modifying_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            raw=root/'persons/test.json'
            raw.parent.mkdir()
            raw.write_text(json.dumps({'person_id':'test','answers':[{'question_id':'q','status':'answered','value':0}]}))
            write_database_exports(raw)
            original = raw.read_bytes()
            handler = web.DemoHandler.__new__(web.DemoHandler)
            replies=[]
            handler.send_json=lambda payload, *args: replies.append(payload)
            with patch.object(web, 'OUTPUT_ROOT', root), patch.object(web, 'PERSONS_ROOT', root/'persons'):
                handler.handle_person_get('/api/person/test/database.json')
                handler.handle_person_get('/api/person/test/evidence.json')
            self.assertEqual(replies[0]['answers'][0]['value'], 0)
            self.assertEqual(replies[1]['entries'][0]['question_id'], 'q')
            self.assertEqual(raw.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
