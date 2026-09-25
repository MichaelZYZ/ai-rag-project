import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from app import core
from app.main import app


class ProductDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = core.DB_PATH
        core.DB_PATH = Path(self.temp.name) / 'discovery.sqlite3'
        core.init_db()
        for number in range(1, 8):
            product_id = 'demo%02d' % number
            core.add_product(product_id, '演示产品%d' % number)
            detail = '支持邮件提醒。' if number == 2 else '支持创建项目和查看进度。'
            core.add_document(product_id, '1.0', '功能', '说明', '%s.md' % product_id,
                              ('# 功能\n%s' % detail).encode())
        core.add_product('empty_product', '尚未导入的产品')
        self.client = TestClient(app)

    def tearDown(self):
        core.DB_PATH = self.previous_db
        self.temp.cleanup()

    def test_featured_six_are_ranked_by_consultations(self):
        core.ask('邮件提醒怎么用？', product_id='demo02', version='1.0')
        core.ask('是否支持邮件提醒？', product_id='demo02', version='1.0')
        core.ask('项目进度怎么看？', product_id='demo03', version='1.0')
        featured = self.client.get('/api/products/featured').json()
        self.assertEqual(len(featured), 6)
        self.assertEqual(featured[0]['id'], 'demo02')
        self.assertEqual(featured[0]['consultation_count'], 2)
        self.assertEqual(featured[1]['id'], 'demo03')
        self.assertNotIn('empty_product', [p['id'] for p in featured])

    def test_all_indexed_products_are_shown_before_first_consultation(self):
        featured = self.client.get('/api/products/featured').json()
        self.assertEqual(len(featured), 7)
        self.assertTrue(all(p['consultation_count'] == 0 for p in featured))
        all_products = self.client.get('/api/products/catalog').json()
        self.assertEqual(len(all_products), 7)

    def test_fuzzy_discovery_and_missing_product_prompt(self):
        found = self.client.get('/api/products/search', params={'q': '邮件提醒'}).json()
        self.assertEqual(found[0]['id'], 'demo02')
        response = self.client.post('/api/ask', json={'question': '有没有演示产品2的知识库？'}).json()
        self.assertEqual(response['status'], 'product_found')
        self.assertEqual(response['matched_products'][0]['id'], 'demo02')
        missing = self.client.post('/api/ask', json={'question': '有没有火星商城的知识库？'}).json()
        self.assertEqual(missing['status'], 'no_product')
        self.assertIn('人工客服', missing['answer'])
        self.assertEqual(missing['matched_products'], [])


if __name__ == '__main__':
    unittest.main()
