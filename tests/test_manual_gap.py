import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from app import core
from app.main import app


class ManualGapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = core.DB_PATH
        core.DB_PATH = Path(self.temp.name) / 'manual.sqlite3'
        core.init_db()
        core.add_product('demo_box', '演示盒')
        core.add_document('demo_box', '1.0', '说明', '说明', 'demo.md',
                          '演示盒支持创建文字记录。'.encode())
        self.client = TestClient(app)

    def tearDown(self):
        core.DB_PATH = self.previous_db
        self.temp.cleanup()

    def test_gap_can_be_completed_as_scoped_question_and_answer(self):
        first = self.client.post('/api/ask', json={
            'question': '演示盒支持语音转写吗？', 'product_id': 'demo_box', 'version': '1.0'}).json()
        self.assertEqual(first['status'], 'no_answer')
        gaps = self.client.get('/api/stats').json()['top_unanswered']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['product_id'], 'demo_box')
        self.assertEqual(gaps[0]['version'], '1.0')
        wrong_version = self.client.post('/api/gaps/%s/answer' % gaps[0]['id'], json={
            'product_id': 'demo_box', 'version': '2.0',
            'question': gaps[0]['question'], 'answer': '错误版本的答案。'})
        self.assertEqual(wrong_version.status_code, 400)
        saved = self.client.post('/api/gaps/%s/answer' % gaps[0]['id'], json={
            'product_id': 'demo_box', 'version': '1.0',
            'question': '演示盒支持语音转写吗？',
            'answer': '演示盒 1.0 不支持语音转写。'})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['resolved_count'], 1)
        self.assertEqual(self.client.get('/api/stats').json()['top_unanswered'], [])
        self.assertEqual(self.client.get('/api/stats').json()['open_gap_count'], 0)
        answered = self.client.post('/api/ask', json={
            'question': '演示盒支持语音转写吗？', 'product_id': 'demo_box', 'version': '1.0'}).json()
        self.assertEqual(answered['status'], 'answered')
        self.assertEqual(answered['answer'], '演示盒 1.0 不支持语音转写。')
        self.assertEqual(answered['sources'][0]['source'], '人工补充')
        self.assertEqual(self.client.post('/api/gaps/%s/answer' % gaps[0]['id'], json={
            'product_id': 'demo_box', 'version': '1.0',
            'question': '演示盒支持语音转写吗？', 'answer': '重复'}).status_code, 404)

    def test_manual_faq_can_be_added_without_existing_gap(self):
        self.assertEqual(self.client.get('/api/stats').json()['open_gap_count'], 0)
        saved = self.client.post('/api/faqs/manual', json={
            'product_id': 'demo_box', 'version': '1.0',
            'question': '演示盒支持离线使用吗？',
            'answer': '演示盒 1.0 支持离线使用。'})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['resolved_count'], 0)
        answered = self.client.post('/api/ask', json={
            'question': '演示盒支持离线使用吗？',
            'product_id': 'demo_box', 'version': '1.0'}).json()
        self.assertEqual(answered['status'], 'answered')
        self.assertEqual(answered['answer'], '演示盒 1.0 支持离线使用。')
        self.assertEqual(answered['sources'][0]['source'], '人工补充')

    def test_homepage_always_shows_manual_entry(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('+ 补充知识缺口', page.text)
        self.assertIn('+ 手动补充问答', page.text)
        self.assertEqual(page.headers['cache-control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
