import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import core
from app.main import app


class HtmlImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = core.DB_PATH
        core.DB_PATH = Path(self.temp.name) / 'html.sqlite3'
        core.init_db()
        core.add_product('demo_box', '演示盒')
        self.client = TestClient(app)

    def tearDown(self):
        core.DB_PATH = self.previous_db
        self.temp.cleanup()

    def test_upload_html_extracts_faq_and_ignores_page_code(self):
        page = ('''<!doctype html><html><head><title>忽略页面标题</title>
            <style>.x{content:"999 个"}</style></head><body>
            <nav>无关导航</nav><main><h1>演示盒参数</h1>
            <p>Q: 演示盒最多支持多少个标签？</p>
            <p>A: 演示盒 1.0 最多支持 <strong>20</strong> 个标签。</p>
            <script>const secret = '999 个标签';</script>
            </main><footer>无关页脚</footer></body></html>''').encode()
        uploaded = self.client.post('/api/documents', data={
            'product_id': 'demo_box', 'version': '1.0', 'category': '参数'},
            files={'file': ('manual.html', page, 'text/html')})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertGreaterEqual(uploaded.json()['faqs'], 1)
        with core.connect() as db:
            text = '\n'.join(row['text'] for row in db.execute('SELECT text FROM chunks'))
        self.assertIn('20 个标签', text)
        self.assertNotIn('999', text)
        self.assertNotIn('无关导航', text)
        answered = self.client.post('/api/ask', json={
            'product_id': 'demo_box', 'version': '1.0',
            'question': '演示盒最多支持多少个标签？'}).json()
        self.assertEqual(answered['status'], 'answered')
        self.assertIn('20 个标签', answered['answer'])

    def test_htm_with_declared_gbk_encoding(self):
        page = ('<html><head><meta charset="gbk"></head><body>'
                '<h2>产品说明</h2><p>演示盒支持离线记录。</p></body></html>').encode('gbk')
        parsed = core.parse_document('guide.HTM', page)
        self.assertIn('## 产品说明', parsed)
        self.assertIn('演示盒支持离线记录。', parsed)


if __name__ == '__main__':
    unittest.main()
