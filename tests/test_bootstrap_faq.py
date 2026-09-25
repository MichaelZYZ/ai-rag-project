import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import core
from app.main import app


class ManualFaqTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = core.DB_PATH
        core.DB_PATH = Path(self.temp.name) / 'startup.sqlite3'
        core.init_db()
        core.add_product('cloud_box', '云盒')
        core.add_product('task_pad', '任务板')

    def tearDown(self):
        core.DB_PATH = self.previous_db
        self.temp.cleanup()

    def test_training_builds_source_backed_faqs_once_per_product_and_version(self):
        core.add_document('cloud_box', '1.0', '介绍', '云盒说明', 'cloud-v1.md',
                          ('# 产品介绍\n云盒是一款离线笔记工具。\n'
                           '# 主要功能\n云盒支持离线查看已下载的文件。').encode())
        core.add_document('cloud_box', '2.0', '介绍', '云盒说明', 'cloud-v2.md',
                          ('# 主要功能\n云盒支持语音转写。').encode())
        core.add_document('task_pad', '1.0', '介绍', '任务板说明', 'tasks.md',
                          ('# 主要功能\n任务板支持团队分配任务。').encode())
        with core.connect() as db:
            db.execute('DELETE FROM faqs')  # Simulate documents indexed before startup FAQ generation.
        first = core.bootstrap_faqs()
        self.assertEqual(first['documents_scanned'], 3)
        self.assertGreaterEqual(first['faqs_added'], 3)
        self.assertEqual(core.bootstrap_faqs()['faqs_added'], 0)

        with core.connect() as db:
            rows = db.execute('''SELECT f.product_id,f.version,f.source,f.answer,t.text
                                 FROM faqs f JOIN document_texts t ON t.document_id=f.document_id''').fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(all(line in row['text'] for line in row['answer'].splitlines()))
        one = core.ask('云盒有哪些功能？', product_id='cloud_box', version='1.0')
        two = core.ask('云盒有哪些功能？', product_id='cloud_box', version='2.0')
        tasks = core.ask('任务板有哪些功能？', product_id='task_pad', version='1.0')
        self.assertIn('离线查看', one['answer'])
        self.assertNotIn('语音转写', one['answer'])
        self.assertIn('语音转写', two['answer'])
        self.assertIn('分配任务', tasks['answer'])
        self.assertEqual(one['sources'][0]['source'], 'cloud-v1.md')

    def test_service_start_does_not_train_until_button_request(self):
        result = core.add_document('cloud_box', '1.0', '介绍', '旧版说明', 'legacy.md',
                                   ('# 产品介绍\n云盒是一款离线笔记工具。\n'
                                    '# 主要功能\n云盒支持离线查看已下载的文件。').encode())
        with core.connect() as db:
            db.execute('DELETE FROM faqs WHERE document_id=?', (result['document_id'],))
            db.execute('DELETE FROM document_texts WHERE document_id=?', (result['document_id'],))
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/stats').json()['auto_faqs'], 0)
            self.assertEqual(client.get('/api/faqs', params={
                'product_id': 'cloud_box', 'version': '1.0'}).json(), [])
            trained = client.post('/api/faqs/train')
            self.assertEqual(trained.status_code, 200, trained.text)
            self.assertEqual(trained.json()['documents_scanned'], 1)
            self.assertGreaterEqual(trained.json()['faqs_added'], 1)
            stats = client.get('/api/stats').json()
            faqs = client.get('/api/faqs', params={
                'product_id': 'cloud_box', 'version': '1.0'}).json()
            self.assertEqual(client.post('/api/faqs/train').json()['faqs_added'], 0)
        self.assertGreaterEqual(stats['auto_faqs'], 1)
        self.assertTrue(any(f['question'] == '什么是云盒？' for f in faqs))
        self.assertTrue(any(f['question'] == '云盒有哪些功能？' for f in faqs))
        self.assertEqual(core.bootstrap_faqs()['faqs_added'], 0)


if __name__ == '__main__':
    unittest.main()
